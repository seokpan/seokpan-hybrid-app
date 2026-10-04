"""Compose captured lifecycle dependencies as one server-side rollout unit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from seokpan.clock import MillisecondClock
    from seokpan.game.application.captured_completion import CapturedGameCompletion
    from seokpan.game.application.captured_invalidation import CapturedGameInvalidation
    from seokpan.game.application.captured_startup import CapturedGameStartup
    from seokpan.game.application.persistence import GamePersistencePort
    from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
    from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter
    from seokpan.production import ProductionProviders
    from seokpan.room.application import RoomApplicationService


@dataclass(frozen=True, slots=True)
class GameLifecycleBindings:
    startup: CapturedGameStartup | None = None
    invalidation: CapturedGameInvalidation | None = None
    completion: CapturedGameCompletion | None = None

    def __post_init__(self) -> None:
        present = tuple(
            item is not None
            for item in (
                self.startup,
                self.invalidation,
                self.completion,
            )
        )
        if any(present) and not all(present):
            raise ValueError("INCOMPLETE_GAME_LIFECYCLE")


def _captured(mode: Literal["legacy", "captured"]) -> bool:
    if mode not in {"legacy", "captured"}:
        raise ValueError("INVALID_GAME_LIFECYCLE_MODE")
    return mode == "captured"


def build_memory_game_lifecycle(
    *,
    mode: Literal["legacy", "captured"],
    rooms: InMemoryRoomRuntimeAdapter,
    votes: InMemoryVoteRuntimeAdapter,
    games: GamePersistencePort,
    room_service: RoomApplicationService,
    clock: MillisecondClock,
) -> GameLifecycleBindings:
    if not _captured(mode):
        return GameLifecycleBindings()

    from seokpan.game.application.captured_completion import CapturedGameCompletion
    from seokpan.game.application.captured_invalidation import CapturedGameInvalidation
    from seokpan.game.application.captured_startup import CapturedGameStartup
    from seokpan.persistence.memory.start_closure import InMemoryCapturedClosureStore
    from seokpan.persistence.memory.start_completion import InMemoryCapturedCompletionStore
    from seokpan.persistence.memory.start_initialization import InMemoryCapturedVoteInitializer

    initializer = InMemoryCapturedVoteInitializer(rooms=rooms, votes=votes, clock=clock)
    return GameLifecycleBindings(
        startup=CapturedGameStartup(
            rooms=room_service,
            runtime=rooms,
            games=games,
            votes=votes,
            initializer=initializer,
            clock=clock,
        ),
        invalidation=CapturedGameInvalidation(
            closures=InMemoryCapturedClosureStore(rooms=rooms, votes=votes),
            games=games,
        ),
        completion=CapturedGameCompletion(
            records=InMemoryCapturedCompletionStore(rooms=rooms, votes=votes),
            games=games,
            rooms=rooms,
        ),
    )


def build_redis_game_lifecycle(
    *,
    mode: Literal["legacy", "captured"],
    providers: ProductionProviders,
    room_service: RoomApplicationService,
) -> GameLifecycleBindings:
    if not _captured(mode):
        return GameLifecycleBindings()

    from seokpan.game.application.captured_completion import CapturedGameCompletion
    from seokpan.game.application.captured_invalidation import CapturedGameInvalidation
    from seokpan.game.application.captured_startup import CapturedGameStartup
    from seokpan.persistence.redis.start_closure import RedisCapturedClosureStore
    from seokpan.persistence.redis.start_completion import RedisCapturedCompletionStore
    from seokpan.persistence.redis.start_initialization import RedisCapturedVoteInitializer

    # Reuse the resources owned by production_resources; do not open another client.
    client = providers.redis_client
    return GameLifecycleBindings(
        startup=CapturedGameStartup(
            rooms=room_service,
            runtime=providers.rooms,
            games=providers.games,
            votes=providers.votes,
            initializer=RedisCapturedVoteInitializer(client, providers.votes),
            clock=providers.clock,
        ),
        invalidation=CapturedGameInvalidation(
            closures=RedisCapturedClosureStore(client),
            games=providers.games,
        ),
        completion=CapturedGameCompletion(
            records=RedisCapturedCompletionStore(client),
            games=providers.games,
            rooms=providers.rooms,
        ),
    )
