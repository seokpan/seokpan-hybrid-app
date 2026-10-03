"""In-memory adapters used by provider-neutral component tests."""

from seokpan.persistence.memory.game_adapter import InMemoryGamePersistenceAdapter
from seokpan.persistence.memory.identity_adapter import InMemoryIdentityAdapter
from seokpan.persistence.memory.realtime_adapter import InMemoryRealtimeEventAdapter
from seokpan.persistence.memory.resolution import (
    InMemoryDueTurnSource,
    InMemoryTieSelectionAudit,
    InMemoryTieSelector,
    InMemoryTurnFinalizationGate,
)
from seokpan.persistence.memory.room_adapter import InMemoryRoomRuntimeAdapter
from seokpan.persistence.memory.session_adapter import InMemorySessionAdapter, ManualClock
from seokpan.persistence.memory.session_workflow import InMemorySessionWorkflow
from seokpan.persistence.memory.vote_adapter import InMemoryVoteRuntimeAdapter

__all__ = [
    "InMemoryIdentityAdapter",
    "InMemoryGamePersistenceAdapter",
    "InMemoryRoomRuntimeAdapter",
    "InMemoryRealtimeEventAdapter",
    "InMemoryDueTurnSource",
    "InMemoryTieSelectionAudit",
    "InMemoryTieSelector",
    "InMemoryTurnFinalizationGate",
    "InMemorySessionAdapter",
    "InMemorySessionWorkflow",
    "InMemoryVoteRuntimeAdapter",
    "ManualClock",
]
