"""Deterministic in-memory Vote runtime adapter."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from seokpan.clock import MillisecondClock
from seokpan.game.domain import Game, GameStatus
from seokpan.room.application import ROOM_REQUEST_DEDUPE_TTL_MS, RoomRuntimeSnapshot
from seokpan.room.domain import RoomStatus
from seokpan.vote.application import (
    RESOLVER_LEASE_MS,
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    FinalizeRuntimeGame,
    InitializeVoteRuntime,
    RemoveRuntimeVote,
    ResolverLease,
    VoteMutationResult,
    VoteRuntimeSnapshot,
)
from seokpan.vote.domain import TurnClosure, TurnStatus, VoteRuleViolation, VoteTally, VoteTurnGame


class _VoteCommand(Protocol):
    @property
    def room_id(self) -> str: ...

    @property
    def request_id(self) -> str: ...

    @property
    def game_id(self) -> str: ...

    @property
    def expected_state_version(self) -> int: ...


@dataclass(slots=True)
class _VoteState:
    game: VoteTurnGame
    state_version: int
    resolver: ResolverLease | None = None
    closure: TurnClosure | None = None
    valid_voter_count: int | None = None


@dataclass(frozen=True, slots=True)
class _CachedResult:
    fingerprint: str
    result: VoteMutationResult
    expires_at_ms: int


class InMemoryVoteRuntimeAdapter:
    """A Fake for contract tests; passing it is not Redis Provider evidence."""

    def __init__(
        self,
        clock: MillisecondClock,
        *,
        room_lookup: Callable[[str], Awaitable[RoomRuntimeSnapshot | None]] | None = None,
    ) -> None:
        self._clock = clock
        self._room_lookup = room_lookup
        self._states: dict[str, _VoteState] = {}
        self._requests: dict[tuple[str, str], _CachedResult] = {}
        self._captured_start_records: (
            tuple[Mapping[tuple[str, str], object], Mapping[tuple[str, str], object]] | None
        ) = None

    def bind_captured_start_records(
        self,
        intents: Mapping[tuple[str, str], object],
        phases: Mapping[tuple[str, str], object],
    ) -> None:
        """Bind the shared Memory stores, never a snapshot copied before capture."""
        current = self._captured_start_records
        if current is not None and (current[0] is not intents or current[1] is not phases):
            raise ValueError("CAPTURED_START_STORE_MISMATCH")
        self._captured_start_records = (intents, phases)

    def _require_legacy_initialization(self, command: InitializeVoteRuntime) -> None:
        records = self._captured_start_records
        key = (command.room_id, command.game_id)
        if records is not None and any(key in store for store in records):
            # Presence alone is sufficient: damaged or empty proof is not permission.
            raise VoteRuleViolation("CAPTURED_START_REQUIRED")

    async def initialize(self, command: InitializeVoteRuntime) -> VoteMutationResult:
        self._require_legacy_initialization(command)
        if self._room_lookup is not None:
            room = await self._room_lookup(command.room_id)
            if room is None:
                raise VoteRuleViolation("ROOM_NOT_FOUND")
            if (
                room.status is not RoomStatus.PLAYING
                or room.game_id != command.game_id
                or room.last_game_id != command.previous_game_id
                or room.last_game_turn_no != command.previous_turn_no
            ):
                raise VoteRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        # A captured start can appear while the optional Room lookup is suspended.
        self._require_legacy_initialization(command)
        replay = self._replay(command)
        if replay is not None:
            return replay
        previous = self._states.get(command.room_id)
        if previous is not None:
            if previous.game.game.status is GameStatus.ACTIVE:
                raise VoteRuleViolation("GAME_RUNTIME_ALREADY_EXISTS")
            if (
                previous.game.game_id != command.previous_game_id
                or previous.game.turn_no != command.previous_turn_no
            ):
                raise VoteRuleViolation("STALE_GAME")
        elif command.previous_game_id is not None:
            raise VoteRuleViolation("GAME_RUNTIME_NOT_FOUND")
        if command.expected_state_version != 1:
            raise VoteRuleViolation("STATE_VERSION_CONFLICT")
        state = _VoteState(
            game=VoteTurnGame(
                game_id=command.game_id,
                participants=command.participants,
                deadline_ms=command.deadline_ms,
                game=Game(),
            ),
            state_version=command.expected_state_version + 1,
        )
        self._states[command.room_id] = state
        return self._remember(command, VoteMutationResult(self._snapshot(command.room_id, state)))

    async def get(self, room_id: str) -> VoteRuntimeSnapshot | None:
        state = self._states.get(room_id)
        return None if state is None else self._snapshot(room_id, state)

    async def participant_connected(self, room_id: str, participant_id: str) -> None:
        state = self._states.get(room_id)
        if state is None or not any(
            item.participant_id == participant_id and item.role.value == "PLAYER"
            for item in state.game.participants
        ):
            return
        before = state.game.participants
        state.game.reconnect(participant_id=participant_id)
        if state.game.participants != before:
            state.state_version += 1

    async def participant_disconnected(self, room_id: str, participant_id: str) -> bool:
        state = self._states.get(room_id)
        if state is None or not any(
            item.participant_id == participant_id and item.role.value == "PLAYER"
            for item in state.game.participants
        ):
            return False
        before = (state.game.participants, state.game.votes)
        votes_before = state.game.votes
        state.game.disconnect(participant_id=participant_id, now_ms=self._clock.now_ms)
        if (state.game.participants, state.game.votes) != before:
            state.state_version += 1
        return state.game.votes != votes_before

    async def cast_vote(self, command: CastRuntimeVote) -> VoteMutationResult:
        replay = self._replay(command)
        if replay is not None:
            return replay
        state = self._require(command)
        before = state.game.votes
        state.game.cast_vote(
            game_id=command.game_id,
            turn_no=command.turn_no,
            participant_id=command.participant_id,
            coordinate=command.coordinate,
            now_ms=self._clock.now_ms,
        )
        if state.game.votes != before:
            state.state_version += 1
        return self._remember(command, VoteMutationResult(self._snapshot(command.room_id, state)))

    async def remove_vote(self, command: RemoveRuntimeVote) -> VoteMutationResult:
        replay = self._replay(command)
        if replay is not None:
            return replay
        state = self._require(command)
        before = state.game.votes
        state.game.remove_vote(
            game_id=command.game_id,
            turn_no=command.turn_no,
            participant_id=command.participant_id,
            now_ms=self._clock.now_ms,
        )
        if state.game.votes != before:
            state.state_version += 1
        return self._remember(command, VoteMutationResult(self._snapshot(command.room_id, state)))

    async def close_turn(self, command: CloseRuntimeTurn) -> VoteMutationResult:
        replay = self._replay(command)
        if replay is not None:
            return replay
        state = self._require(command)
        valid_voter_count = sum(
            item.connected and item.role.value == "PLAYER" and item.team is state.game.current_team
            for item in state.game.participants
        )
        closure = state.game.close_voting(
            game_id=command.game_id,
            turn_no=command.turn_no,
            now_ms=self._clock.now_ms,
            next_deadline_ms=command.next_deadline_ms,
        )
        state.closure = closure if closure.status is TurnStatus.RESOLVING else None
        state.valid_voter_count = valid_voter_count
        state.state_version += 1
        return self._remember(
            command,
            VoteMutationResult(
                self._snapshot(command.room_id, state, closure=closure),
                closure=closure,
                valid_voter_count=valid_voter_count,
            ),
        )

    async def acquire_resolver(self, command: AcquireRuntimeResolver) -> VoteMutationResult:
        replay = self._replay(command)
        if replay is not None:
            return replay
        state = self._require(command)
        if state.game.turn_status is not TurnStatus.RESOLVING:
            raise VoteRuleViolation("TURN_NOT_RESOLVING")
        current = state.resolver
        if (
            current is not None
            and current.expires_at_ms > self._clock.now_ms
            and current.resolution_id != command.resolution_id
        ):
            raise VoteRuleViolation("RESOLVER_LEASE_HELD")
        state.resolver = ResolverLease(
            resolution_id=command.resolution_id,
            expires_at_ms=self._clock.now_ms + RESOLVER_LEASE_MS,
        )
        return self._remember(command, VoteMutationResult(self._snapshot(command.room_id, state)))

    async def apply_resolution(self, command: ApplyRuntimeResolution) -> VoteMutationResult:
        replay = self._replay(command)
        if replay is not None:
            return replay
        state = self._require(command)
        resolver = state.resolver
        if resolver is None or resolver.resolution_id != command.resolution_id:
            raise VoteRuleViolation("RESOLVER_NOT_OWNER")
        if resolver.expires_at_ms <= self._clock.now_ms:
            raise VoteRuleViolation("RESOLVER_LEASE_EXPIRED")
        if command.resolution.result.value == "JOINT_LOSS":
            resolution = state.game.resolve_joint_loss(
                game_id=command.game_id,
                turn_no=command.turn_no,
            )
        else:
            resolution = state.game.resolve_move(
                game_id=command.game_id,
                turn_no=command.turn_no,
                selected_coordinate=command.resolution.selected_coordinate,
                next_deadline_ms=command.next_deadline_ms,
            )
        if resolution != command.resolution:
            raise VoteRuleViolation("RESOLUTION_MISMATCH")
        state.resolver = None
        state.closure = None
        state.state_version += 1
        return self._remember(
            command,
            VoteMutationResult(
                self._snapshot(command.room_id, state),
                resolution=resolution,
            ),
        )

    async def finalize_game(self, command: FinalizeRuntimeGame) -> VoteMutationResult:
        replay = self._replay(command)
        if replay is not None:
            return replay
        state = self._require(command)
        if state.game.turn_no != command.turn_no:
            raise VoteRuleViolation("STALE_TURN")
        state.game.finalize_external_result(
            end_reason=command.end_reason,
            winner=command.winner,
        )
        state.resolver = None
        state.closure = None
        state.valid_voter_count = 0
        state.state_version += 1
        return self._remember(
            command,
            VoteMutationResult(self._snapshot(command.room_id, state)),
        )

    async def discard_game(self, room_id: str, game_id: str) -> None:
        state = self._states.get(room_id)
        if state is None:
            return
        if state.game.game_id != game_id:
            raise VoteRuleViolation("STALE_GAME")
        self._states.pop(room_id, None)
        for key in tuple(self._requests):
            if key[0] == room_id:
                self._requests.pop(key, None)

    def _require(self, command: _VoteCommand) -> _VoteState:
        state = self._states.get(command.room_id)
        if state is None:
            raise VoteRuleViolation("GAME_RUNTIME_NOT_FOUND")
        if state.game.game_id != command.game_id:
            raise VoteRuleViolation("STALE_GAME")
        if state.state_version != command.expected_state_version:
            raise VoteRuleViolation("STATE_VERSION_CONFLICT")
        return state

    def _snapshot(
        self,
        room_id: str,
        state: _VoteState,
        *,
        closure: TurnClosure | None = None,
    ) -> VoteRuntimeSnapshot:
        votes = state.game.votes
        counts = Counter(item.coordinate for item in votes)
        active_closure = closure or state.closure
        tally = (
            active_closure.tally
            if active_closure is not None
            else tuple(
                VoteTally(coordinate=coordinate, count=count)
                for coordinate, count in sorted(
                    counts.items(), key=lambda item: (-item[1], item[0].canonical)
                )
            )
        )
        candidates = () if active_closure is None else active_closure.candidates
        return VoteRuntimeSnapshot(
            room_id=room_id,
            game_id=state.game.game_id,
            state_version=state.state_version,
            turn_no=state.game.turn_no,
            turn_status=state.game.turn_status,
            current_team=state.game.current_team,
            deadline_ms=state.game.deadline_ms,
            consecutive_passes=state.game.consecutive_passes,
            move_no=state.game.game.move_no,
            game_status=state.game.game.status,
            end_reason=state.game.game.end_reason,
            participants=state.game.participants,
            votes=votes,
            tally=tally,
            candidates=candidates,
            occupied_cells=state.game.game.occupied_cells,
            last_move=state.game.game.moves[-1] if state.game.game.moves else None,
            resolver=state.resolver,
            valid_voter_count=state.valid_voter_count,
        )

    def _replay(self, command: _VoteCommand) -> VoteMutationResult | None:
        self._purge_expired_requests()
        cached = self._requests.get((command.room_id, command.request_id))
        if cached is None:
            return None
        if cached.fingerprint != self._fingerprint(command):
            raise VoteRuleViolation("REQUEST_ID_CONFLICT")
        result = cached.result
        current = self._states.get(command.room_id)
        if current is None or current.game.game_id != result.snapshot.game_id:
            raise VoteRuleViolation("STALE_GAME")
        return VoteMutationResult(
            snapshot=result.snapshot,
            replayed=True,
            closure=result.closure,
            resolution=result.resolution,
            valid_voter_count=result.valid_voter_count,
        )

    def _remember(self, command: _VoteCommand, result: VoteMutationResult) -> VoteMutationResult:
        self._requests[(command.room_id, command.request_id)] = _CachedResult(
            fingerprint=self._fingerprint(command),
            result=result,
            expires_at_ms=self._clock.now_ms + ROOM_REQUEST_DEDUPE_TTL_MS,
        )
        return result

    def _purge_expired_requests(self) -> None:
        expired = [
            key
            for key, value in self._requests.items()
            if value.expires_at_ms <= self._clock.now_ms
        ]
        for key in expired:
            self._requests.pop(key, None)

    @staticmethod
    def _fingerprint(command: _VoteCommand) -> str:
        return hashlib.sha256(repr(command).encode()).hexdigest()
