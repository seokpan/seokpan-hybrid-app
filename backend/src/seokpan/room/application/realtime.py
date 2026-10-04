"""Provider-neutral Lobby and Room realtime event contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

REALTIME_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    event_type: str
    event_id: str
    occurred_at: str
    state_version: int
    payload: Mapping[str, object]
    room_id: str | None = None
    game_id: str | None = None
    turn_no: int | None = None
    schema_version: int = REALTIME_SCHEMA_VERSION


class RealtimeSubscription(Protocol):
    async def receive(self) -> RealtimeEvent: ...

    async def close(self) -> None: ...


class RealtimeEventPort(Protocol):
    @property
    def lobby_version(self) -> int: ...

    def room_version(self, room_id: str) -> int: ...

    async def current_lobby_version(self) -> int: ...

    async def current_room_version(self, room_id: str) -> int: ...

    async def subscribe_lobby(self) -> RealtimeSubscription: ...

    async def subscribe_room(self, room_id: str) -> RealtimeSubscription: ...

    async def lobby_rooms_changed(
        self,
        payload: Mapping[str, object],
        *,
        event_key: str | None = None,
    ) -> None: ...

    async def room_changed(
        self,
        *,
        event_type: str,
        room_id: str,
        payload: Mapping[str, object],
        event_key: str | None = None,
        game_id: str | None = None,
        turn_no: int | None = None,
    ) -> None: ...


class NullRealtimeEventAdapter:
    """Default used outside transports which need realtime delivery."""

    @property
    def lobby_version(self) -> int:
        return 1

    def room_version(self, room_id: str) -> int:
        del room_id
        return 1

    async def current_lobby_version(self) -> int:
        return self.lobby_version

    async def current_room_version(self, room_id: str) -> int:
        return self.room_version(room_id)

    async def subscribe_lobby(self) -> RealtimeSubscription:
        raise RuntimeError("REALTIME_SUBSCRIPTION_NOT_CONFIGURED")

    async def subscribe_room(self, room_id: str) -> RealtimeSubscription:
        del room_id
        raise RuntimeError("REALTIME_SUBSCRIPTION_NOT_CONFIGURED")

    async def lobby_rooms_changed(
        self,
        payload: Mapping[str, object],
        *,
        event_key: str | None = None,
    ) -> None:
        del payload, event_key

    async def room_changed(
        self,
        *,
        event_type: str,
        room_id: str,
        payload: Mapping[str, object],
        event_key: str | None = None,
        game_id: str | None = None,
        turn_no: int | None = None,
    ) -> None:
        del event_type, room_id, payload, event_key, game_id, turn_no
