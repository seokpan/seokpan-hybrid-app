"""Redis adapters and shared runtime-state primitives."""

from seokpan.persistence.redis.chat_adapter import RedisChatAdapter
from seokpan.persistence.redis.connection import (
    RedisConfigurationError,
    runtime_redis,
    validated_redis_url,
)
from seokpan.persistence.redis.participation_adapter import RedisRoomParticipationResolver
from seokpan.persistence.redis.presence_adapter import RedisPresenceAdapter
from seokpan.persistence.redis.realtime_adapter import RedisRealtimeEventAdapter
from seokpan.persistence.redis.room_adapter import RedisRoomRuntimeAdapter
from seokpan.persistence.redis.session_adapter import RedisSessionAdapter
from seokpan.persistence.redis.session_workflow import RedisSessionWorkflow
from seokpan.persistence.redis.turn_coordinator import RedisTurnCoordinator
from seokpan.persistence.redis.vote_adapter import RedisVoteRuntimeAdapter

__all__ = [
    "RedisConfigurationError",
    "RedisChatAdapter",
    "RedisPresenceAdapter",
    "RedisRoomParticipationResolver",
    "RedisRealtimeEventAdapter",
    "RedisRoomRuntimeAdapter",
    "RedisSessionAdapter",
    "RedisSessionWorkflow",
    "RedisTurnCoordinator",
    "RedisVoteRuntimeAdapter",
    "runtime_redis",
    "validated_redis_url",
]
