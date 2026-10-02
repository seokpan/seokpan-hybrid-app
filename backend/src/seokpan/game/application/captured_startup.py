"""Opt-in captured startup coordinator; the composition roots stay disabled for now."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from seokpan.game.application.persistence import (
    GameParticipantRecord,
    GamePersistencePort,
    PersistenceRuleViolation,
    StartGameCommand,
)
from seokpan.game.application.service import (
    GameApplicationSnapshot,
    MillisecondClock,
    _stable_uuid4,
)
from seokpan.game.domain import Stone
from seokpan.identity.application import SessionActorType, SessionRecord
from seokpan.room.application.runtime import RoomRuntimePort
from seokpan.room.application.start_capture import CaptureRoomGameStart
from seokpan.room.application.start_intent import RoomGameStartIntent, StartIntentPlayer
from seokpan.room.domain import RoomRuleViolation, RoomStatus
from seokpan.vote.application.runtime import VoteRuntimePort
from seokpan.vote.application.start_initialization import (
    CapturedVoteInitializationPort,
    InitializeCapturedGame,
)

if TYPE_CHECKING:
    from seokpan.room.application.lobby import RoomApplicationService


class CapturedRoomRuntimePort(RoomRuntimePort, Protocol):
    async def get_start_intent(self, room_id: str, game_id: str) -> RoomGameStartIntent | None: ...


@dataclass(frozen=True, slots=True)
class CapturedStartOutcome:
    snapshot: GameApplicationSnapshot
    initialized_now: bool


class CapturedGameStartup:
    """New path behind explicit dependency injection, not a user-controlled flag.

    Reuse the existing Room service for trusted identity lookup. Provider acceptance
    remains the version/owner/roster authority. Final activation awaits closure,
    retention, legacy writer guards and real Provider gates (Issue #86).
    """

    def __init__(
        self,
        *,
        rooms: RoomApplicationService,
        runtime: CapturedRoomRuntimePort,
        games: GamePersistencePort,
        votes: VoteRuntimePort,
        initializer: CapturedVoteInitializationPort,
        clock: MillisecondClock,
    ) -> None:
        self._rooms = rooms
        self._runtime = runtime
        self._games = games
        self._votes = votes
        self._initializer = initializer
        self._clock = clock

    async def start_game(
        self,
        *,
        session: SessionRecord,
        room_id: str,
        request_id: str,
        expected_state_version: int,
    ) -> CapturedStartOutcome:
        if (
            not isinstance(request_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) is None
        ):
            raise RoomRuleViolation("INVALID_REQUEST_ID")
        if type(expected_state_version) is not int or not 1 <= expected_state_version < 2**53:
            raise RoomRuleViolation("INVALID_STATE_VERSION")
        participation = await self._rooms.resolve_participation(session.session_digest)
        if participation is None or participation.room_id != room_id:
            raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
        room = await self._runtime.get(room_id)
        if room is None:
            raise RoomRuleViolation("ROOM_NOT_FOUND")
        if room.owner_id != participation.participant_id:
            raise RoomRuleViolation("OWNER_REQUIRED")
        recovering = room.status is RoomStatus.PLAYING
        if room.status is RoomStatus.WAITING:
            if room.state_version != expected_state_version:
                raise RoomRuleViolation("STATE_VERSION_CONFLICT")
            players = []
            for player in room.participants:
                if not player.ready:
                    continue
                identity = await self._rooms.resolve_participant_identity(player.participant_id)
                if identity is None or identity.room_id != room_id:
                    raise RoomRuleViolation("PARTICIPANT_IDENTITY_NOT_FOUND")
                if identity.actor_type is SessionActorType.MEMBER:
                    players.append(
                        StartIntentPlayer(
                            participant_id=player.participant_id,
                            team=player.team.value,
                            member_id=identity.actor_id,
                        )
                    )
                else:
                    # Preserve the existing Guest-NNNN persistence attribution.
                    digest = hashlib.sha256(identity.actor_id.encode("utf-8")).hexdigest()
                    players.append(
                        StartIntentPlayer(
                            participant_id=player.participant_id,
                            team=player.team.value,
                            guest_label=f"Guest-{int(digest[:8], 16) % 10_000:04d}",
                        )
                    )
            game_id = _stable_uuid4(f"{room_id}\nstart\n{request_id}")
            await self._runtime.start_game(
                CaptureRoomGameStart(
                    room_id=room_id,
                    request_id=request_id,
                    actor_id=participation.participant_id,
                    game_id=game_id,
                    expected_state_version=expected_state_version,
                    players=tuple(players),
                )
            )
        elif room.status is RoomStatus.PLAYING and room.game_id is not None:
            game_id = room.game_id
        else:
            raise RoomRuleViolation("ROOM_NOT_WAITING")
        intent = await self._runtime.get_start_intent(room_id, game_id)
        if intent is None or intent.room_id != room_id or intent.game_id != game_id:
            # Never reconstruct the accepted roster from current Ready/identity.
            raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
        latest = await self._runtime.get(room_id)
        if latest is None or latest.status is not RoomStatus.PLAYING or latest.game_id != game_id:
            raise RoomRuleViolation("GAME_NOT_IN_CURRENT_ROOM")
        if latest.owner_id != participation.participant_id:
            raise RoomRuleViolation("OWNER_REQUIRED")
        original_retry = (
            request_id == intent.original_request_id
            and expected_state_version == intent.accepted_state_version - 1
        )
        if not original_retry and expected_state_version != latest.state_version:
            raise RoomRuleViolation("STATE_VERSION_CONFLICT")
        phase = await self._initializer.get_phase(intent)
        history = await self._games.load_game(game_id)
        active = await self._votes.get(room_id)
        command = self.persistence_command(intent)
        if history is None:
            # INITIALIZED + lost history is not a fresh startup. PENDING + an
            # already matching Runtime is also ambiguous, not safe to overwrite.
            if phase != "PENDING" or (active is not None and active.game_id == game_id):
                raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
            # Check before the first write; a later result check cannot undo
            # history creation when a result already exists or its read fails.
            if await self._games.load_result(game_id) is not None:
                raise PersistenceRuleViolation("GAME_RESULT_HISTORY_MISMATCH")
            try:
                await self._games.start_game(command)
            except PersistenceRuleViolation as error:
                if error.code != "GAME_START_CONFLICT":
                    raise
            history = await self._games.load_game(game_id)
        if history is None:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        actual = history.start

        def roster(
            values: tuple[GameParticipantRecord, ...],
        ) -> list[tuple[str, str, int | None, str | None]]:
            return sorted(
                [
                    (
                        item.participant_id,
                        item.team.value,
                        item.member_id,
                        item.guest_label,
                    )
                    for item in values
                ],
                key=lambda item: item[0],
            )

        if (
            actual.game_id != command.game_id
            or actual.room_id != command.room_id
            or actual.voting_time_seconds != command.voting_time_seconds
            or actual.started_at != command.started_at
            or roster(actual.participants) != roster(command.participants)
        ):
            raise PersistenceRuleViolation("GAME_START_CONFLICT")
        if await self._games.load_result(game_id) is not None:
            raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
        if history.moves and (active is None or active.game_id != game_id):
            raise RoomRuleViolation("GAME_START_RECOVERY_REQUIRED")
        result = await self._initializer.initialize(
            InitializeCapturedGame(
                intent=intent,
                request_id=request_id,
                actor_id=participation.participant_id,
                expected_room_version=latest.state_version,
            )
        )
        return CapturedStartOutcome(
            snapshot=GameApplicationSnapshot(
                latest,
                result.snapshot,
                participation.participant_id,
                self._clock.now_ms,
                replayed=recovering or result.replayed,
            ),
            initialized_now=not result.replayed,
        )

    @staticmethod
    def persistence_command(intent: RoomGameStartIntent) -> StartGameCommand:
        return StartGameCommand(
            game_id=intent.game_id,
            room_id=intent.room_id,
            voting_time_seconds=intent.vote_seconds,
            started_at=intent.started_at,
            participants=tuple(
                GameParticipantRecord(
                    participant_id=item.participant_id,
                    team=Stone(item.team),
                    member_id=None if item.member_id is None else int(item.member_id),
                    guest_label=item.guest_label,
                )
                for item in intent.players
            ),
        )
