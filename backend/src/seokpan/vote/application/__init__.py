"""Application contracts for Vote runtime state."""

from seokpan.vote.application.runtime import (
    RESOLVER_LEASE_MS,
    VOTE_RUNTIME_SCHEMA_VERSION,
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    FinalizeRuntimeGame,
    InitializeVoteRuntime,
    RemoveRuntimeVote,
    ResolverLease,
    VoteMutationResult,
    VoteRuntimePort,
    VoteRuntimeSnapshot,
)

__all__ = [
    "RESOLVER_LEASE_MS",
    "VOTE_RUNTIME_SCHEMA_VERSION",
    "AcquireRuntimeResolver",
    "ApplyRuntimeResolution",
    "CastRuntimeVote",
    "CloseRuntimeTurn",
    "FinalizeRuntimeGame",
    "InitializeVoteRuntime",
    "RemoveRuntimeVote",
    "ResolverLease",
    "VoteMutationResult",
    "VoteRuntimePort",
    "VoteRuntimeSnapshot",
]
