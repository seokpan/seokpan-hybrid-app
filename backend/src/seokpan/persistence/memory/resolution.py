"""Explicit Headless fakes for the provider-neutral Turn resolution runner."""

from __future__ import annotations

from seokpan.game.application import (
    DueTurn,
    TieSelectionRecord,
    TurnFinalizationApproval,
)
from seokpan.room.application.lobby import LobbyRoomRuntimePort
from seokpan.room.domain import RoomStatus
from seokpan.vote.application import VoteRuntimePort, VoteRuntimeSnapshot


class InMemoryDueTurnSource:
    def __init__(self, values: tuple[DueTurn, ...] = ()) -> None:
        self.values = values

    async def due_turns(self, *, now_ms: int, limit: int) -> tuple[DueTurn, ...]:
        del now_ms
        return self.values[:limit]


class MemoryRoomTurnSource:
    """Development discovery only; no production Redis index or scheduling claim."""

    def __init__(self, rooms: LobbyRoomRuntimePort, votes: VoteRuntimePort) -> None:
        self._rooms = rooms
        self._votes = votes

    async def due_turns(self, *, now_ms: int, limit: int) -> tuple[DueTurn, ...]:
        if limit < 1:
            raise ValueError("INVALID_DUE_TURN_LIMIT")
        result = []
        for room in await self._rooms.list_rooms():
            if room.status is not RoomStatus.PLAYING or room.game_id is None:
                continue
            vote = await self._votes.get(room.room_id)
            if vote is None or vote.game_id != room.game_id:
                continue
            if vote.deadline_ms is None or vote.deadline_ms <= now_ms:
                result.append(DueTurn(room.room_id, vote.game_id, vote.turn_no))
            if len(result) == limit:
                break
        return tuple(result)


class InMemoryTurnFinalizationGate:
    def __init__(
        self,
        approval: TurnFinalizationApproval = TurnFinalizationApproval.ALLOWED,
    ) -> None:
        self.approval = approval

    async def assess(
        self,
        *,
        due_turn: DueTurn,
        snapshot: VoteRuntimeSnapshot,
    ) -> TurnFinalizationApproval:
        del due_turn, snapshot
        return self.approval


class InMemoryTieSelector:
    def __init__(self, selected_coordinate: str | None = None) -> None:
        self.selected_coordinate = selected_coordinate
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    async def select(
        self,
        *,
        game_id: str,
        turn_no: int,
        candidates: tuple[str, ...],
    ) -> str:
        self.calls.append((game_id, turn_no, candidates))
        if self.selected_coordinate is not None:
            return self.selected_coordinate
        if not candidates:
            raise ValueError("TIE_CANDIDATES_REQUIRED")
        # Deterministic Fake only; production selection is a separate adapter.
        return candidates[0]


class InMemoryTieSelectionAudit:
    def __init__(self) -> None:
        self.records: list[TieSelectionRecord] = []

    async def record(self, value: TieSelectionRecord) -> None:
        if value not in self.records:
            self.records.append(value)
