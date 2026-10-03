"""redis.asyncio-backed Vote, Turn, and resolver runtime adapter."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast

from redis.exceptions import RedisError

from seokpan.game.domain import AppliedMove, BoardCell, Coordinate, EndReason, GameStatus, Stone
from seokpan.persistence.redis.common import (
    LuaScriptRunner,
    RedisClient,
    RedisKeyspace,
    RedisProviderError,
    VersionedJsonCodec,
)
from seokpan.persistence.redis.start_capture_script import start_intent_key, start_phase_key
from seokpan.persistence.redis.vote_scripts import VOTE_DISCARD, VOTE_MUTATION, VOTE_READ
from seokpan.room.application import ROOM_REQUEST_DEDUPE_TTL_MS
from seokpan.vote.application import (
    RESOLVER_LEASE_MS,
    VOTE_RUNTIME_SCHEMA_VERSION,
    AcquireRuntimeDeparture,
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    DepartureFinalization,
    FinalizeRuntimeGame,
    InitializeVoteRuntime,
    RemoveRuntimeVote,
    ResolverLease,
    VoteMutationResult,
    VoteRuntimeSnapshot,
)
from seokpan.vote.domain import (
    ParticipantRole,
    TurnClosure,
    TurnResolution,
    TurnResultKind,
    TurnStatus,
    Vote,
    Voter,
    VoteRuleViolation,
    VoteTally,
)


class RedisVoteRuntimeAdapter:
    def __init__(self, client: RedisClient) -> None:
        self._client = client
        self._scripts = LuaScriptRunner(client)

    async def initialize(self, command: InitializeVoteRuntime) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "initialize",
            1,
            {
                "schema_version": VOTE_RUNTIME_SCHEMA_VERSION,
                "game_id": command.game_id,
                "participants": [self._voter_value(item) for item in command.participants],
                "deadline_ms": command.deadline_ms,
                "expected_state_version": command.expected_state_version,
                "previous_game_id": command.previous_game_id,
                "previous_turn_no": command.previous_turn_no,
            },
            previous_turn_no=command.previous_turn_no,
        )

    async def get(self, room_id: str) -> VoteRuntimeSnapshot | None:
        game_key = RedisKeyspace.room_game(room_id)
        for attempt in range(3):
            try:
                raw_game = await self._client.get(game_key)
            except RedisError as error:
                raise RedisProviderError() from error
            if raw_game is None:
                return None
            game = VersionedJsonCodec.decode(raw_game)
            if _integer(game, "schema_version") != VOTE_RUNTIME_SCHEMA_VERSION:
                raise RedisProviderError("VOTE_SCHEMA_VERSION_MISMATCH")
            turn_no = _integer(game, "turn_no")
            result = await self._scripts.execute(
                VOTE_READ,
                keys=self._read_keys(room_id, turn_no),
                args=(
                    VersionedJsonCodec.encode(
                        {
                            "room_id": room_id,
                            "game_id": _string(game, "game_id"),
                            "turn_no": turn_no,
                        }
                    ),
                ),
            )
            decoded = self._result(result)
            if (
                decoded.get("ok") is False
                and decoded.get("error") == "REDIS_SNAPSHOT_CHANGED"
                and attempt < 2
            ):
                continue
            self._raise_rejection(decoded)
            return self._optional_snapshot(decoded.get("snapshot"))
        raise RedisProviderError("REDIS_SNAPSHOT_CHANGED")

    async def discard_game(self, room_id: str, game_id: str) -> None:
        game_key = RedisKeyspace.room_game(room_id)
        for attempt in range(3):
            try:
                raw = await self._client.get(game_key)
            except RedisError as error:
                raise RedisProviderError() from error
            if raw is None:
                return
            game = VersionedJsonCodec.decode(raw)
            if _integer(game, "schema_version") != VOTE_RUNTIME_SCHEMA_VERSION:
                raise RedisProviderError("VOTE_SCHEMA_VERSION_MISMATCH")
            if _string(game, "game_id") != game_id:
                raise VoteRuleViolation("STALE_GAME")
            turn_no = _integer(game, "turn_no")
            result = await self._scripts.execute(
                VOTE_DISCARD,
                keys=self._discard_keys(room_id, turn_no),
                args=(game_id, turn_no),
            )
            decoded = self._result(result)
            if (
                decoded.get("ok") is False
                and decoded.get("error") == "REDIS_SNAPSHOT_CHANGED"
                and attempt < 2
            ):
                continue
            self._raise_rejection(decoded)
            return
        raise RedisProviderError("REDIS_SNAPSHOT_CHANGED")

    async def cast_vote(self, command: CastRuntimeVote) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "cast_vote",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "participant_id": command.participant_id,
                "coordinate": command.coordinate.canonical,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def remove_vote(self, command: RemoveRuntimeVote) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "remove_vote",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "participant_id": command.participant_id,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def close_turn(self, command: CloseRuntimeTurn) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "close_turn",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "expected_state_version": command.expected_state_version,
                "next_deadline_ms": command.next_deadline_ms,
            },
        )

    async def acquire_resolver(self, command: AcquireRuntimeResolver) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "acquire_resolver",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "resolution_id": command.resolution_id,
                "expected_state_version": command.expected_state_version,
            },
        )

    async def acquire_departure(self, command: AcquireRuntimeDeparture) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "acquire_departure",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "resolution_id": command.resolution_id,
                "expected_state_version": command.expected_state_version,
                "expected_room_state_version": command.expected_room_state_version,
                "end_reason": command.end_reason.value,
                "winner": command.winner.value,
            },
        )

    async def apply_resolution(self, command: ApplyRuntimeResolution) -> VoteMutationResult:
        resolution = command.resolution
        return await self._mutate(
            command.room_id,
            command.request_id,
            "apply_resolution",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "resolution_id": command.resolution_id,
                "resolution": self._resolution_value(resolution),
                "expected_state_version": command.expected_state_version,
                "persistence_confirmed": command.persistence_confirmed,
                "next_deadline_ms": command.next_deadline_ms,
                "next_game_status": (
                    GameStatus.ACTIVE.value
                    if resolution.end_reason is None
                    else GameStatus.FINISHED.value
                ),
                "next_end_reason": (
                    None if resolution.end_reason is None else resolution.end_reason.value
                ),
            },
        )

    async def finalize_game(self, command: FinalizeRuntimeGame) -> VoteMutationResult:
        return await self._mutate(
            command.room_id,
            command.request_id,
            "finalize_game",
            command.turn_no,
            {
                "game_id": command.game_id,
                "turn_no": command.turn_no,
                "expected_state_version": command.expected_state_version,
                "end_reason": command.end_reason.value,
                "winner": command.winner.value,
                "resolution_id": command.resolution_id,
            },
        )

    async def _mutate(
        self,
        room_id: str,
        request_id: str,
        operation: str,
        turn_no: int,
        payload: Mapping[str, object],
        *,
        previous_turn_no: int | None = None,
    ) -> VoteMutationResult:
        complete_payload = {
            **payload,
            "room_id": room_id,
            "fingerprint": self._fingerprint(operation, payload),
        }
        result = await self._scripts.execute(
            VOTE_MUTATION,
            keys=self._mutation_keys(room_id, turn_no)
            + (
                ()
                if previous_turn_no is None
                else (
                    RedisKeyspace.room_votes(room_id, previous_turn_no),
                    RedisKeyspace.room_vote_tally(room_id, previous_turn_no),
                    RedisKeyspace.room_resolver(room_id, previous_turn_no),
                )
            )
            + (
                (
                    start_intent_key(room_id, _string(payload, "game_id")),
                    start_phase_key(room_id, _string(payload, "game_id")),
                )
                if operation == "initialize"
                else ()
            ),
            args=(
                operation,
                request_id,
                ROOM_REQUEST_DEDUPE_TTL_MS,
                RESOLVER_LEASE_MS,
                VersionedJsonCodec.encode(complete_payload),
            ),
        )
        decoded = self._result(result)
        self._raise_rejection(decoded)
        return self._mutation_result(decoded)

    @staticmethod
    def _read_keys(room_id: str, turn_no: int) -> tuple[str, ...]:
        return (
            RedisKeyspace.room_meta(room_id),
            RedisKeyspace.room_participants(room_id),
            RedisKeyspace.room_game(room_id),
            RedisKeyspace.room_board(room_id),
            RedisKeyspace.room_votes(room_id, turn_no),
            RedisKeyspace.room_vote_tally(room_id, turn_no),
            RedisKeyspace.room_resolver(room_id, turn_no),
        )

    @classmethod
    def _mutation_keys(cls, room_id: str, turn_no: int) -> tuple[str, ...]:
        return (
            RedisKeyspace.room_meta(room_id),
            RedisKeyspace.room_participants(room_id),
            RedisKeyspace.room_connections(room_id),
            RedisKeyspace.room_game(room_id),
            RedisKeyspace.room_board(room_id),
            RedisKeyspace.room_votes(room_id, turn_no),
            RedisKeyspace.room_vote_tally(room_id, turn_no),
            RedisKeyspace.room_resolver(room_id, turn_no),
            RedisKeyspace.room_votes(room_id, turn_no + 1),
            RedisKeyspace.room_vote_tally(room_id, turn_no + 1),
            RedisKeyspace.room_resolver(room_id, turn_no + 1),
            RedisKeyspace.room_requests(room_id),
            RedisKeyspace.room_request_expiries(room_id),
        )

    @staticmethod
    def _discard_keys(room_id: str, turn_no: int) -> tuple[str, ...]:
        return (
            RedisKeyspace.room_game(room_id),
            RedisKeyspace.room_board(room_id),
            RedisKeyspace.room_votes(room_id, turn_no),
            RedisKeyspace.room_vote_tally(room_id, turn_no),
            RedisKeyspace.room_resolver(room_id, turn_no),
            RedisKeyspace.room_votes(room_id, turn_no + 1),
            RedisKeyspace.room_vote_tally(room_id, turn_no + 1),
            RedisKeyspace.room_resolver(room_id, turn_no + 1),
            RedisKeyspace.room_requests(room_id),
            RedisKeyspace.room_request_expiries(room_id),
        )

    @staticmethod
    def _fingerprint(operation: str, payload: Mapping[str, object]) -> str:
        encoded = VersionedJsonCodec.encode({"operation": operation, **payload})
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _voter_value(value: Voter) -> dict[str, object]:
        return {
            "participant_id": value.participant_id,
            "team": value.team.value,
            "role": value.role.value,
            "connected": value.connected,
        }

    @staticmethod
    def _resolution_value(value: TurnResolution) -> dict[str, object]:
        move = value.applied_move
        return {
            "game_id": value.game_id,
            "turn_no": value.turn_no,
            "team": value.team.value,
            "result": value.result.value,
            "status": value.status.value,
            "selected_coordinate": (
                None if value.selected_coordinate is None else value.selected_coordinate.canonical
            ),
            "applied_move": (
                None
                if move is None
                else {
                    "move_no": move.move_no,
                    "team": move.team.value,
                    "coordinate": move.coordinate.canonical,
                }
            ),
            "end_reason": None if value.end_reason is None else value.end_reason.value,
        }

    @staticmethod
    def _result(result: object) -> dict[str, object]:
        if not isinstance(result, (bytes, str)):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        decoded = VersionedJsonCodec.decode(result)
        if not isinstance(decoded.get("ok"), bool):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return decoded

    @staticmethod
    def _raise_rejection(result: Mapping[str, object]) -> None:
        if result["ok"] is True:
            return
        error = result.get("error")
        if not isinstance(error, str):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        if error in {"REDIS_SNAPSHOT_CHANGED", "VOTE_SCHEMA_VERSION_MISMATCH"}:
            raise RedisProviderError(error)
        raise VoteRuleViolation(error)

    @classmethod
    def _mutation_result(cls, value: Mapping[str, object]) -> VoteMutationResult:
        snapshot = cls._optional_snapshot(value.get("snapshot"))
        if snapshot is None:
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return VoteMutationResult(
            snapshot=snapshot,
            replayed=_optional_bool(value, "replayed", False),
            closure=cls._optional_closure(value.get("closure")),
            resolution=cls._optional_resolution(value.get("resolution")),
            valid_voter_count=_optional_integer(value, "valid_voter_count"),
        )

    @classmethod
    def _optional_snapshot(cls, value: object) -> VoteRuntimeSnapshot | None:
        if value is None:
            return None
        item = _mapping(value)
        if _integer(item, "schema_version") != VOTE_RUNTIME_SCHEMA_VERSION:
            raise RedisProviderError("VOTE_SCHEMA_VERSION_MISMATCH")
        if "last_move" not in item:
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        snapshot = VoteRuntimeSnapshot(
            room_id=_string(item, "room_id"),
            game_id=_string(item, "game_id"),
            state_version=_integer(item, "state_version"),
            turn_no=_integer(item, "turn_no"),
            turn_status=TurnStatus(_string(item, "turn_status")),
            current_team=Stone(_string(item, "current_team")),
            deadline_ms=_optional_integer(item, "deadline_ms"),
            consecutive_passes=_integer(item, "consecutive_passes"),
            move_no=_integer(item, "move_no"),
            game_status=GameStatus(_string(item, "game_status")),
            end_reason=_optional_end_reason(item.get("end_reason")),
            participants=tuple(cls._voter(value) for value in _list(item["participants"])),
            votes=tuple(cls._vote(value) for value in _list(item["votes"])),
            tally=tuple(cls._tally(value) for value in _list(item["tally"])),
            candidates=tuple(
                Coordinate.parse(_scalar_string(value)) for value in _list(item["candidates"])
            ),
            occupied_cells=tuple(cls._cell(value) for value in _list(item["occupied_cells"])),
            last_move=cls._last_move(item.get("last_move")),
            resolver=cls._optional_resolver(item.get("resolver")),
            valid_voter_count=_optional_integer(item, "valid_voter_count"),
            schema_version=_integer(item, "schema_version"),
        )
        move = snapshot.last_move
        if (snapshot.move_no == 0 and move is not None) or (
            snapshot.move_no > 0
            and (
                move is None
                or move.move_no != snapshot.move_no
                or not any(
                    cell.coordinate == move.coordinate and cell.stone is move.team
                    for cell in snapshot.occupied_cells
                )
            )
        ):
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return snapshot

    @staticmethod
    def _last_move(value: object) -> AppliedMove | None:
        if value is None:
            return None
        item = _mapping(value)
        number, team, coordinate = (
            _integer(item, "move_no"),
            _string(item, "team"),
            _string(item, "coordinate"),
        )
        if not 1 <= number <= 225 or team not in {"BLACK", "WHITE"}:
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        try:
            parsed = Coordinate.parse(coordinate)
        except ValueError as error:
            raise RedisProviderError("REDIS_RESPONSE_INVALID") from error
        if parsed.canonical != coordinate:
            raise RedisProviderError("REDIS_RESPONSE_INVALID")
        return AppliedMove(number, Stone(team), parsed)

    @staticmethod
    def _voter(value: object) -> Voter:
        item = _mapping(value)
        return Voter(
            participant_id=_string(item, "participant_id"),
            team=Stone(_string(item, "team")),
            role=ParticipantRole(_string(item, "role")),
            connected=_boolean(item, "connected"),
        )

    @staticmethod
    def _vote(value: object) -> Vote:
        item = _mapping(value)
        return Vote(_string(item, "participant_id"), Coordinate.parse(_string(item, "coordinate")))

    @staticmethod
    def _tally(value: object) -> VoteTally:
        item = _mapping(value)
        return VoteTally(Coordinate.parse(_string(item, "coordinate")), _integer(item, "count"))

    @staticmethod
    def _cell(value: object) -> BoardCell:
        item = _mapping(value)
        return BoardCell(
            Coordinate.parse(_string(item, "coordinate")), Stone(_string(item, "stone"))
        )

    @staticmethod
    def _optional_resolver(value: object) -> ResolverLease | None:
        if value is None:
            return None
        item = _mapping(value)
        raw = item.get("departure")
        departure = None
        if raw is not None:
            decision = _mapping(raw)
            reason = EndReason(_string(decision, "end_reason"))
            winner = Stone(_string(decision, "winner"))
            move_no = _integer(decision, "expected_move_no")
            ended_at_ms = _integer(decision, "ended_at_ms")
            if (
                not (
                    (reason is EndReason.FORFEIT and winner in {Stone.BLACK, Stone.WHITE})
                    or (reason is EndReason.JOINT_LOSS and winner is Stone.EMPTY)
                )
                or not 0 <= move_no <= 225
                or ended_at_ms < 0
            ):
                raise RedisProviderError("REDIS_RESPONSE_INVALID")
            departure = DepartureFinalization(reason, winner, move_no, ended_at_ms)
        return ResolverLease(
            _string(item, "resolution_id"), _integer(item, "expires_at_ms"), departure
        )

    @classmethod
    def _optional_closure(cls, value: object) -> TurnClosure | None:
        if value is None:
            return None
        item = _mapping(value)
        return TurnClosure(
            game_id=_string(item, "game_id"),
            turn_no=_integer(item, "turn_no"),
            team=Stone(_string(item, "team")),
            result=TurnResultKind(_string(item, "result")),
            status=TurnStatus(_string(item, "status")),
            tally=tuple(cls._tally(value) for value in _list(item["tally"])),
            candidates=tuple(
                Coordinate.parse(_scalar_string(value)) for value in _list(item["candidates"])
            ),
        )

    @staticmethod
    def _optional_resolution(value: object) -> TurnResolution | None:
        if value is None:
            return None
        item = _mapping(value)
        selected = _optional_scalar_string(item.get("selected_coordinate"))
        move_value = item.get("applied_move")
        move = None
        if move_value is not None:
            move_item = _mapping(move_value)
            move = AppliedMove(
                move_no=_integer(move_item, "move_no"),
                team=Stone(_string(move_item, "team")),
                coordinate=Coordinate.parse(_string(move_item, "coordinate")),
            )
        return TurnResolution(
            game_id=_string(item, "game_id"),
            turn_no=_integer(item, "turn_no"),
            team=Stone(_string(item, "team")),
            result=TurnResultKind(_string(item, "result")),
            status=TurnStatus(_string(item, "status")),
            selected_coordinate=None if selected is None else Coordinate.parse(selected),
            applied_move=move,
            end_reason=_optional_end_reason(item.get("end_reason")),
        )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if isinstance(value, dict) and not value:
        return []
    if not isinstance(value, list):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return cast(list[object], value)


def _string(value: Mapping[str, object], key: str) -> str:
    return _scalar_string(value.get(key))


def _scalar_string(value: object) -> str:
    if not isinstance(value, str):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return value


def _optional_scalar_string(value: object) -> str | None:
    if value is None:
        return None
    return _scalar_string(value)


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _optional_integer(value: Mapping[str, object], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if type(item) is not int:
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _optional_bool(value: Mapping[str, object], key: str, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise RedisProviderError("REDIS_RESPONSE_INVALID")
    return item


def _optional_end_reason(value: object) -> EndReason | None:
    text = _optional_scalar_string(value)
    return None if text is None else EndReason(text)
