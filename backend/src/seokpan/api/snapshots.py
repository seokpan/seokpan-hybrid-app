"""Read-only recovery views with bounded resource/stream consistency checks."""

from pydantic import BaseModel, ConfigDict, Field

from seokpan.api.game import GameApiServices, GameSnapshotResponse, game_snapshot_response
from seokpan.api.problems import ApiProblem
from seokpan.api.room import (
    LobbyResponse,
    RoomApiServices,
    RoomSnapshotResponse,
    lobby_room_response,
    room_snapshot_response,
)
from seokpan.identity.application import SessionRecord
from seokpan.room.application import RealtimeEventPort
from seokpan.room.domain import RoomRuleViolation


class LobbyRecoveryResponse(LobbyResponse):
    stream_version: int = Field(ge=1)


class RoomRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room: RoomSnapshotResponse
    game: GameSnapshotResponse | None
    stream_version: int = Field(ge=1)


class SnapshotReader:
    def __init__(
        self,
        rooms: RoomApiServices,
        games: GameApiServices | None,
        events: RealtimeEventPort,
    ) -> None:
        self._rooms = rooms
        self._games = games
        self._events = events

    async def lobby(self) -> LobbyRecoveryResponse:
        for _ in range(3):
            version = await self._events.current_lobby_version()
            rooms = await self._rooms.rooms.list_rooms()
            payload = [lobby_room_response(room) for room in rooms]
            current = await self._rooms.rooms.list_rooms()
            # Only public list fields matter; Ready/team changes are not Lobby events.
            if (
                payload == [lobby_room_response(room) for room in current]
                and version == await self._events.current_lobby_version()
            ):
                return LobbyRecoveryResponse(rooms=payload, stream_version=version)
        raise ApiProblem(503, "SNAPSHOT_CHANGED", "State changed while reading; retry the snapshot")

    async def room(self, session: SessionRecord, room_id: str) -> RoomRecoveryResponse:
        for _ in range(3):
            participant = await self._rooms.rooms.resolve_participation(session.session_digest)
            if participant is None or participant.room_id != room_id:
                raise RoomRuleViolation("SESSION_NOT_IN_ROOM")
            version = await self._events.current_room_version(room_id)
            room = await self._rooms.rooms.get(room_id)
            if room is None:
                raise RoomRuleViolation("ROOM_NOT_FOUND")
            try:
                game = None
                if room.game_id is not None:
                    if self._games is None:
                        raise ApiProblem(503, "GAME_UNAVAILABLE", "Game snapshot unavailable")
                    game = await self._games.games.get_game(session=session, game_id=room.game_id)
                payload = await room_snapshot_response(self._rooms, room)
                if room != await self._rooms.rooms.get(room_id):
                    continue
                if game is not None:
                    assert self._games is not None
                    current = await self._games.games.get_game(
                        session=session, game_id=game.game.game_id
                    )
                    if (
                        game.room != room
                        or current.room != room
                        or current.game != game.game
                        or current.viewer_participant_id != game.viewer_participant_id
                    ):
                        continue
                    game = current
            except RoomRuleViolation as error:
                if error.code not in {"GAME_NOT_IN_CURRENT_ROOM", "GAME_RUNTIME_NOT_FOUND"}:
                    raise
                continue
            if version != await self._events.current_room_version(
                room_id
            ) or participant != await self._rooms.rooms.resolve_participation(
                session.session_digest
            ):
                continue
            return RoomRecoveryResponse(
                room=payload,
                game=None if game is None else game_snapshot_response(game),
                stream_version=version,
            )
        raise ApiProblem(503, "SNAPSHOT_CHANGED", "State changed while reading; retry the snapshot")
