from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from seokpan.api.chat import ChatApiServices, chat_router
from seokpan.api.game import GameApiServices, game_router
from seokpan.api.identity import IdentityApiServices, identity_router
from seokpan.api.presence import PresenceApiServices, presence_router
from seokpan.api.problems import install_problem_handlers
from seokpan.api.realtime import (
    ActiveWebSocketRegistry,
    RealtimeApiServices,
    realtime_router,
)
from seokpan.api.room import RoomApiServices, room_router
from seokpan.api.statistics import StatisticsApiServices, statistics_router
from seokpan.clock import MillisecondClock
from seokpan.game.application import GameApplicationService, TurnResolutionRunner
from seokpan.game.application.resolution import DueTurnSource, TieSelector
from seokpan.game_lifecycle import build_memory_game_lifecycle, build_redis_game_lifecycle
from seokpan.health import RuntimeReadiness
from seokpan.health import router as health_router
from seokpan.identity.application import (
    AuthSessionService,
    MemberIdentityService,
)
from seokpan.logging_config import configure_logging
from seokpan.metrics import install_metrics
from seokpan.persistence.memory import (
    InMemoryDueTurnSource,
    InMemoryGamePersistenceAdapter,
    InMemoryIdentityAdapter,
    InMemoryRealtimeEventAdapter,
    InMemorySessionAdapter,
    InMemorySessionWorkflow,
    InMemoryTieSelectionAudit,
    InMemoryTieSelector,
    InMemoryTurnFinalizationGate,
    InMemoryVoteRuntimeAdapter,
    ManualClock,
)
from seokpan.persistence.memory.chat_adapter import InMemoryChatAdapter
from seokpan.persistence.memory.presence_adapter import InMemoryPresenceAdapter
from seokpan.persistence.memory.resolution import MemoryRoomTurnSource
from seokpan.persistence.memory.room_admission import (
    SessionAdmissionMemoryRoomAdapter as InMemoryRoomRuntimeAdapter,
)
from seokpan.persistence.memory.statistics_adapter import InMemoryStatisticsAdapter
from seokpan.room.application import (
    DisconnectExpiryRunner,
    RealtimeEventPort,
    RoomApplicationService,
    RoomConnectionCoordinator,
)
from seokpan.security import (
    Argon2Parameters,
    Argon2PasswordHasher,
    Argon2RoomPassword,
    SecretsTokenSource,
)
from seokpan.settings import Settings


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    identity_api: IdentityApiServices
    room_api: RoomApiServices | None = None
    game_api: GameApiServices | None = None
    realtime_api: RealtimeApiServices | None = None
    disconnect_expiry: DisconnectExpiryRunner | None = None
    headless_clock: ManualClock | None = None
    turn_resolution: TurnResolutionRunner | None = None
    headless_due_turns: InMemoryDueTurnSource | None = None
    statistics_api: StatisticsApiServices | None = None
    chat_api: ChatApiServices | None = None
    presence_api: PresenceApiServices | None = None


def build_headless_services(
    settings: Settings,
    *,
    realtime_events: RealtimeEventPort | None = None,
    clock: MillisecondClock | None = None,
    discover_turns: bool = False,
    tie_selector: TieSelector | None = None,
) -> ApplicationServices:
    if settings.environment == "production":
        raise RuntimeError("Production provider configuration is required")
    password_hasher = Argon2PasswordHasher(
        Argon2Parameters(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
    )
    dummy_hash = password_hasher.hash(SecretsTokenSource().issue())
    member_ratings: dict[int, int] = {}
    member_store = InMemoryIdentityAdapter(member_ratings=member_ratings)
    members = MemberIdentityService(
        member_store,
        password_hasher,
        dummy_password_hash=dummy_hash,
    )
    clock = clock or ManualClock()
    events = realtime_events or InMemoryRealtimeEventAdapter()
    votes = InMemoryVoteRuntimeAdapter(clock, room_lookup=lambda room_id: room_runtime.get(room_id))
    room_runtime = InMemoryRoomRuntimeAdapter(clock, vote_connections=votes)
    room_service = RoomApplicationService(
        room_runtime,
        Argon2RoomPassword(password_hasher),
        events,
        votes,
    )
    session_adapter = InMemorySessionAdapter(clock)
    sessions = AuthSessionService(
        InMemorySessionWorkflow(session_adapter, room_service),
        SecretsTokenSource(),
    )
    identity_api = IdentityApiServices(settings, members, sessions, room_service)
    games = InMemoryGamePersistenceAdapter(member_ratings)
    lifecycle = build_memory_game_lifecycle(
        mode=settings.game_lifecycle_mode,
        rooms=room_runtime,
        votes=votes,
        games=games,
        room_service=room_service,
        clock=clock,
    )
    game_service = GameApplicationService(
        rooms=room_service,
        games=games,
        votes=votes,
        clock=clock,
        events=events,
        captured_startup=lifecycle.startup,
    )
    game_api = GameApiServices(identity_api, game_service)
    registry = ActiveWebSocketRegistry()
    due_turns: DueTurnSource = (
        MemoryRoomTurnSource(room_runtime, votes) if discover_turns else InMemoryDueTurnSource()
    )
    turn_resolution = TurnResolutionRunner(
        due_turns=due_turns,
        finalization_gate=InMemoryTurnFinalizationGate(),
        tie_selector=tie_selector or InMemoryTieSelector(),
        tie_audit=InMemoryTieSelectionAudit(),
        votes=votes,
        games=games,
        rooms=room_runtime,
        clock=clock,
        runner_id="headless",
        events=events,
        captured_invalidation=lifecycle.invalidation,
        captured_completion=lifecycle.completion,
    )
    room_api = RoomApiServices(identity_api, room_service, turn_resolution)
    connections = RoomConnectionCoordinator(
        rooms=room_service,
        votes=votes,
        clock=clock,
        departures=turn_resolution,
    )
    return ApplicationServices(
        identity_api,
        room_api,
        game_api,
        RealtimeApiServices(identity_api, room_api, game_api, events, connections, registry),
        DisconnectExpiryRunner(
            due_disconnects=room_runtime,
            connections=connections,
            clock=clock,
        ),
        clock if isinstance(clock, ManualClock) else None,
        turn_resolution,
        due_turns if isinstance(due_turns, InMemoryDueTurnSource) else None,
        StatisticsApiServices(identity_api, InMemoryStatisticsAdapter(member_store, games)),
        ChatApiServices(identity_api, room_service, InMemoryChatAdapter(), registry),
        PresenceApiServices(
            identity_api,
            InMemoryPresenceAdapter(lease_seconds=15, max_connections=1000),
            registry,
        ),
    )


def build_production_services(settings: Settings, providers: object) -> ApplicationServices:
    """Compose only concrete MariaDB/Redis providers; performs no external I/O."""

    from seokpan.persistence.redis import (
        RedisRoomParticipationResolver,
        RedisSessionWorkflow,
        RedisTurnCoordinator,
    )
    from seokpan.production import ProductionProviders

    if settings.environment != "production" or not isinstance(providers, ProductionProviders):
        raise RuntimeError("Production providers are required")
    resolver = RedisRoomParticipationResolver(providers.rooms, providers.sessions)
    room_service = RoomApplicationService(
        providers.rooms,
        providers.room_passwords,
        providers.realtime,
        providers.votes,
        resolver,
    )
    sessions = AuthSessionService(
        RedisSessionWorkflow(providers.sessions, room_service, providers.rooms),
        providers.tokens,
    )
    members = MemberIdentityService(
        providers.identities,
        providers.passwords,
        dummy_password_hash=providers.passwords.hash(providers.tokens.issue()),
    )
    identity_api = IdentityApiServices(settings, members, sessions, room_service)
    lifecycle = build_redis_game_lifecycle(
        mode=settings.game_lifecycle_mode,
        providers=providers,
        room_service=room_service,
    )
    game_service = GameApplicationService(
        rooms=room_service,
        games=providers.games,
        votes=providers.votes,
        clock=providers.clock,
        events=providers.realtime,
        captured_startup=lifecycle.startup,
    )
    game_api = GameApiServices(identity_api, game_service)
    registry = ActiveWebSocketRegistry()
    turn_coordinator = RedisTurnCoordinator(
        providers.redis_client,
        providers.rooms,
        providers.votes,
        providers.games,
    )
    turn_resolution = TurnResolutionRunner(
        due_turns=turn_coordinator,
        finalization_gate=turn_coordinator,
        tie_selector=turn_coordinator,
        tie_audit=turn_coordinator,
        votes=providers.votes,
        games=providers.games,
        rooms=providers.rooms,
        clock=providers.clock,
        runner_id=settings.instance_id,
        events=providers.realtime,
        captured_invalidation=lifecycle.invalidation,
        captured_completion=lifecycle.completion,
    )
    room_api = RoomApiServices(identity_api, room_service, turn_resolution)
    connections = RoomConnectionCoordinator(
        rooms=room_service,
        votes=providers.votes,
        clock=providers.clock,
        departures=turn_resolution,
    )
    return ApplicationServices(
        identity_api=identity_api,
        room_api=room_api,
        game_api=game_api,
        realtime_api=RealtimeApiServices(
            identity_api,
            room_api,
            game_api,
            providers.realtime,
            connections,
            registry,
        ),
        disconnect_expiry=DisconnectExpiryRunner(
            due_disconnects=providers.rooms,
            connections=connections,
            clock=providers.clock,
        ),
        turn_resolution=turn_resolution,
        statistics_api=StatisticsApiServices(identity_api, providers.statistics),
        chat_api=ChatApiServices(identity_api, room_service, providers.chat, registry),
        presence_api=PresenceApiServices(identity_api, providers.presence, registry),
    )


def create_app(
    *,
    settings: Settings | None = None,
    services: ApplicationServices | None = None,
    readiness: RuntimeReadiness | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_services = services or build_headless_services(resolved_settings)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        registry = (
            None
            if resolved_services.realtime_api is None
            else resolved_services.realtime_api.registry
        )
        if registry is not None:
            registry.begin_runtime()
        try:
            yield
        finally:
            if registry is not None:
                registry.end_runtime()

    application = FastAPI(
        title="Seokpan API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    install_metrics(application)
    install_problem_handlers(application)
    application.include_router(health_router)
    application.include_router(identity_router(resolved_services.identity_api))
    if resolved_services.room_api is not None:
        application.include_router(room_router(resolved_services.room_api))
    if resolved_services.game_api is not None:
        application.include_router(game_router(resolved_services.game_api))
    if resolved_services.statistics_api is not None:
        application.include_router(statistics_router(resolved_services.statistics_api))
    if resolved_services.realtime_api is not None:
        application.include_router(realtime_router(resolved_services.realtime_api))
    if resolved_services.chat_api is not None:
        application.include_router(chat_router(resolved_services.chat_api))
    if resolved_services.presence_api is not None:
        application.include_router(presence_router(resolved_services.presence_api))
    application.state.services = resolved_services
    application.state.readiness = readiness
    return application


def create_runtime_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved)
    if resolved.environment != "production":
        return create_app(settings=resolved)
    from seokpan.production_app import create_production_app

    return create_production_app(resolved)


app = create_runtime_app()
