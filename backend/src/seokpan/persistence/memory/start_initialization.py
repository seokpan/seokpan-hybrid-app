"""Atomic within one event loop; reference adapter, not Redis durability evidence."""

from __future__ import annotations

import json

from seokpan.clock import MillisecondClock
from seokpan.game.domain import Game, GameStatus, Stone
from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter, _VoteState
from seokpan.room.application.start_intent import RoomGameStartIntent
from seokpan.room.domain import RoomStatus
from seokpan.vote.application.runtime import VoteMutationResult
from seokpan.vote.application.start_initialization import InitializeCapturedGame
from seokpan.vote.domain import ParticipantRole, Voter, VoteRuleViolation, VoteTurnGame


class InMemoryCapturedVoteInitializer:
    """Use the same Room and Vote instances as the rest of the application."""

    def __init__(
        self,
        *,
        rooms: InMemoryRoomRuntimeAdapter,
        votes: InMemoryVoteRuntimeAdapter,
        clock: MillisecondClock,
    ) -> None:
        votes.bind_captured_start_records(rooms._start_intents, rooms._start_phases)
        self._rooms = rooms
        self._votes = votes
        self._clock = clock

    async def get_phase(self, intent: RoomGameStartIntent) -> str | None:
        return self._rooms._start_phases.get((intent.room_id, intent.game_id))

    async def initialize(self, command: InitializeCapturedGame) -> VoteMutationResult:
        # No await between inspecting Room/intent, creating Runtime and writing
        # its witness. Using private storage is confined to this Memory adapter.
        intent = command.intent
        key = (intent.room_id, intent.game_id)
        state = self._rooms._rooms.get(intent.room_id)
        if state is None:
            raise VoteRuleViolation("ROOM_NOT_FOUND")
        room = self._rooms._snapshot(intent.room_id, state)
        if (
            room.status is not RoomStatus.PLAYING
            or room.game_id != intent.game_id
            or room.last_game_id != intent.previous_game_id
            or room.last_game_turn_no != intent.previous_turn_no
        ):
            raise VoteRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        if room.owner_id != command.actor_id:
            raise VoteRuleViolation("OWNER_REQUIRED")
        if room.state_version != command.expected_room_version:
            raise VoteRuleViolation("STATE_VERSION_CONFLICT")
        if self._rooms._start_intents.get(key) != intent:
            raise VoteRuleViolation("START_INTENT_INVALID")
        phase = self._rooms._start_phases.get(key)
        previous = self._votes._states.get(intent.room_id)
        if phase != "PENDING":
            try:
                witness = json.loads(phase) if isinstance(phase, str) else None
            except ValueError:
                witness = None
            if (
                not isinstance(witness, dict)
                or type(witness.get("schema_version")) is not int
                or witness.get("schema_version") != 1
                or witness.get("phase") != "INITIALIZED"
                or witness.get("game_id") != intent.game_id
                or witness.get("intent_fingerprint") != intent.fingerprint
                or type(witness.get("first_deadline_ms")) is not int
                or type(witness.get("initialized_at_ms")) is not int
                or witness["initialized_at_ms"] < intent.started_at_ms
                or witness["first_deadline_ms"] >= 2**53
                or witness["first_deadline_ms"]
                != witness["initialized_at_ms"] + intent.vote_seconds * 1000
                or previous is None
                or previous.game.game_id != intent.game_id
            ):
                raise VoteRuleViolation("GAME_START_RECOVERY_REQUIRED")
            return VoteMutationResult(
                self._votes._snapshot(intent.room_id, previous),
                replayed=True,
            )
        if previous is not None:
            if previous.game.game_id == intent.game_id:
                raise VoteRuleViolation("GAME_START_RECOVERY_REQUIRED")
            if (
                previous.game.game.status is GameStatus.ACTIVE
                or previous.game.game_id != intent.previous_game_id
                or previous.game.turn_no != intent.previous_turn_no
            ):
                raise VoteRuleViolation("STALE_GAME")
        elif intent.previous_game_id is not None:
            raise VoteRuleViolation("GAME_RUNTIME_NOT_FOUND")
        players = {item.participant_id: item for item in intent.players}
        present = {item.participant_id for item in room.participants}
        if not set(players) <= present:
            raise VoteRuleViolation("GAME_START_RECOVERY_REQUIRED")
        current = {item.participant_id: item for item in room.participants}
        participants = tuple(
            Voter(
                participant_id=item.participant_id,
                team=Stone(item.team),
                role=ParticipantRole.PLAYER,
                connected=current[item.participant_id].connected,
            )
            for item in intent.players
        )
        now = self._clock.now_ms
        deadline = now + intent.vote_seconds * 1000
        if type(now) is not int or now < intent.started_at_ms or deadline >= 2**53:
            raise VoteRuleViolation("INVALID_DEADLINE")
        new_state = _VoteState(
            game=VoteTurnGame(
                game_id=intent.game_id,
                participants=participants,
                deadline_ms=deadline,
                game=Game(),
            ),
            state_version=2,
        )
        result = VoteMutationResult(self._votes._snapshot(intent.room_id, new_state))
        witness_json = json.dumps(
            {
                "schema_version": 1,
                "phase": "INITIALIZED",
                "game_id": intent.game_id,
                "intent_fingerprint": intent.fingerprint,
                "initialized_at_ms": now,
                "first_deadline_ms": deadline,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._votes._states[intent.room_id] = new_state
        self._rooms._start_phases[key] = witness_json
        return result
