"""Headless connection coordination and due disconnect processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from seokpan.identity.application import SessionRecord
from seokpan.room.application.lobby import RoomApplicationService
from seokpan.room.application.runtime import (
    DueRoomDisconnect,
    DueRoomDisconnectSource,
    RoomMutationResult,
)
from seokpan.room.domain import GameTermination, RoomRuleViolation
from seokpan.vote.application import VoteRuntimePort
from seokpan.vote.domain import TurnStatus

_LOGGER = logging.getLogger(__name__)


class MillisecondClock(Protocol):
    @property
    def now_ms(self) -> int: ...


class ConfirmedDepartureFinalizer(Protocol):
    async def finalize_departures(self, *, room_id: str, game_id: str) -> bool: ...


class DisconnectExpiryStatus(StrEnum):
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    RETRY_REQUIRED = "RETRY_REQUIRED"


@dataclass(frozen=True, slots=True)
class DisconnectExpiryResult:
    due: DueRoomDisconnect
    status: DisconnectExpiryStatus


class RoomConnectionCoordinator:
    def __init__(
        self,
        *,
        rooms: RoomApplicationService,
        votes: VoteRuntimePort,
        clock: MillisecondClock,
        departures: ConfirmedDepartureFinalizer | None = None,
    ) -> None:
        self._rooms = rooms
        self._votes = votes
        self._clock = clock
        self._departures = departures

    async def connect(self, *, session: SessionRecord, room_id: str) -> tuple[int, int]:
        result = await self._rooms.connect(session=session, room_id=room_id)
        if result.connection_generation is None:
            raise RoomRuleViolation("CONNECTION_GENERATION_MISSING")
        if result.snapshot is None:
            raise RoomRuleViolation("ROOM_SNAPSHOT_MISSING")
        return result.connection_generation, result.snapshot.state_version

    async def disconnect(
        self,
        *,
        room_id: str,
        participant_id: str,
        connection_generation: int,
    ) -> None:
        result = await self._rooms.disconnect_participant(
            room_id=room_id,
            participant_id=participant_id,
            connection_generation=connection_generation,
            active_vote_turn=await self._active_vote_turn(room_id),
        )
        await self._finalize_game_transition(room_id, result)

    async def expire(self, due: DueRoomDisconnect) -> DisconnectExpiryResult:
        try:
            result = await self._rooms.expire_disconnect(
                room_id=due.room_id,
                participant_id=due.participant_id,
                connection_generation=due.connection_generation,
                active_vote_turn=await self._active_vote_turn(due.room_id),
            )
        except RoomRuleViolation as error:
            if error.code in {"ROOM_NOT_FOUND", "CONNECTION_NOT_FOUND"}:
                return DisconnectExpiryResult(due, DisconnectExpiryStatus.STALE)
            if error.code in {"DISCONNECT_LEASE_ACTIVE", "STATE_VERSION_CONFLICT"}:
                return DisconnectExpiryResult(due, DisconnectExpiryStatus.RETRY_REQUIRED)
            raise
        if not result.stale_connection:
            await self._finalize_game_transition(due.room_id, result)
        return DisconnectExpiryResult(
            due,
            (
                DisconnectExpiryStatus.STALE
                if result.stale_connection
                else DisconnectExpiryStatus.EXPIRED
            ),
        )

    async def _finalize_game_transition(
        self,
        room_id: str,
        result: RoomMutationResult,
    ) -> None:
        if self._departures is None or result.replayed:
            return
        if result.game_termination is GameTermination.SYSTEM_INVALID:
            return
        if result.snapshot is not None and result.snapshot.game_id is not None:
            await self._departures.finalize_departures(
                room_id=room_id,
                game_id=result.snapshot.game_id,
            )

    async def _active_vote_turn(self, room_id: str) -> int | None:
        runtime = await self._votes.get(room_id)
        if (
            runtime is None
            or runtime.turn_status is not TurnStatus.VOTING
            or runtime.deadline_ms is None
            or self._clock.now_ms >= runtime.deadline_ms
        ):
            return None
        return runtime.turn_no


class DisconnectExpiryRunner:
    def __init__(
        self,
        *,
        due_disconnects: DueRoomDisconnectSource,
        connections: RoomConnectionCoordinator,
        clock: MillisecondClock,
    ) -> None:
        self._due_disconnects = due_disconnects
        self._connections = connections
        self._clock = clock

    async def run_once(self, *, limit: int = 100) -> tuple[DisconnectExpiryResult, ...]:
        if limit < 1:
            raise ValueError("INVALID_DUE_DISCONNECT_LIMIT")
        due = await self._due_disconnects.due_disconnects(
            now_ms=self._clock.now_ms,
            limit=limit,
        )
        results: list[DisconnectExpiryResult] = []
        for item in due:
            try:
                results.append(await self._connections.expire(item))
            except RoomRuleViolation:
                _LOGGER.exception(
                    "Disconnect expiry item failed",
                    extra={
                        "event": "disconnect_expiry.item_failed",
                        "room_id": item.room_id,
                        "participant_id": item.participant_id,
                    },
                )
        return tuple(results)
