from __future__ import annotations

import pytest

from seokpan.game.domain import AppliedMove, Coordinate, EndReason, GameStatus, Stone
from seokpan.persistence.memory import InMemoryVoteRuntimeAdapter, ManualClock
from seokpan.vote.application import (
    RESOLVER_LEASE_MS,
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    InitializeVoteRuntime,
    RemoveRuntimeVote,
)
from seokpan.vote.domain import (
    ParticipantRole,
    TurnResolution,
    TurnResultKind,
    TurnStatus,
    Voter,
    VoteRuleViolation,
)


def participants() -> tuple[Voter, ...]:
    return (
        Voter("black-1", Stone.BLACK),
        Voter("black-2", Stone.BLACK),
        Voter("white-1", Stone.WHITE),
        Voter("guest-1", Stone.BLACK, role=ParticipantRole.SPECTATOR),
    )


async def initialized(
    clock: ManualClock,
) -> tuple[InMemoryVoteRuntimeAdapter, int]:
    adapter = InMemoryVoteRuntimeAdapter(clock)
    result = await adapter.initialize(
        InitializeVoteRuntime(
            room_id="room-1",
            request_id="initialize-1",
            game_id="game-1",
            participants=participants(),
            deadline_ms=1_000,
            expected_state_version=1,
        )
    )
    return adapter, result.snapshot.state_version


@pytest.mark.asyncio
async def test_vote_replace_delete_and_request_replay_are_deterministic() -> None:
    clock = ManualClock()
    adapter, version = await initialized(clock)

    first = await adapter.cast_vote(
        CastRuntimeVote("room-1", "vote-1", "game-1", 1, "black-1", Coordinate.parse("A1"), version)
    )
    assert first.snapshot.state_version == version + 1
    assert first.snapshot.tally[0].coordinate.canonical == "A1"

    replay = await adapter.cast_vote(
        CastRuntimeVote("room-1", "vote-1", "game-1", 1, "black-1", Coordinate.parse("A1"), version)
    )
    assert replay.replayed
    assert replay.snapshot == first.snapshot

    replaced = await adapter.cast_vote(
        CastRuntimeVote(
            "room-1",
            "vote-2",
            "game-1",
            1,
            "black-1",
            Coordinate.parse("B2"),
            first.snapshot.state_version,
        )
    )
    assert tuple(item.coordinate.canonical for item in replaced.snapshot.votes) == ("B2",)

    removed = await adapter.remove_vote(
        RemoveRuntimeVote(
            "room-1",
            "remove-1",
            "game-1",
            1,
            "black-1",
            replaced.snapshot.state_version,
        )
    )
    assert removed.snapshot.votes == ()
    assert removed.snapshot.tally == ()


@pytest.mark.asyncio
async def test_vote_rejects_wrong_role_team_deadline_and_stale_version() -> None:
    clock = ManualClock()
    adapter, version = await initialized(clock)

    for participant_id, error in (
        ("white-1", "CURRENT_TEAM_REQUIRED"),
        ("guest-1", "PLAYER_REQUIRED"),
    ):
        with pytest.raises(VoteRuleViolation, match=error):
            await adapter.cast_vote(
                CastRuntimeVote(
                    "room-1",
                    f"vote-{participant_id}",
                    "game-1",
                    1,
                    participant_id,
                    Coordinate.parse("A1"),
                    version,
                )
            )

    with pytest.raises(VoteRuleViolation, match="STATE_VERSION_CONFLICT"):
        await adapter.cast_vote(
            CastRuntimeVote(
                "room-1", "vote-stale", "game-1", 1, "black-1", Coordinate.parse("A1"), 9
            )
        )

    clock.advance(1_000)
    with pytest.raises(VoteRuleViolation, match="TURN_DEADLINE_REACHED"):
        await adapter.cast_vote(
            CastRuntimeVote(
                "room-1",
                "vote-late",
                "game-1",
                1,
                "black-1",
                Coordinate.parse("A1"),
                version,
            )
        )


@pytest.mark.asyncio
async def test_participant_disconnect_reports_and_removes_active_vote() -> None:
    clock = ManualClock()
    adapter, version = await initialized(clock)
    await adapter.cast_vote(
        CastRuntimeVote(
            "room-1",
            "vote-disconnect",
            "game-1",
            1,
            "black-1",
            Coordinate.parse("H8"),
            version,
        )
    )

    assert await adapter.participant_disconnected("room-1", "black-1") is True
    snapshot = await adapter.get("room-1")
    assert snapshot is not None
    assert snapshot.votes == ()
    participant = next(item for item in snapshot.participants if item.participant_id == "black-1")
    assert participant.connected is False
    assert await adapter.participant_disconnected("room-1", "black-1") is False


@pytest.mark.asyncio
async def test_close_resolver_lease_handoff_and_move_application() -> None:
    clock = ManualClock()
    adapter, version = await initialized(clock)
    voted = await adapter.cast_vote(
        CastRuntimeVote("room-1", "vote-1", "game-1", 1, "black-1", Coordinate.parse("H8"), version)
    )
    clock.advance(1_000)
    closed = await adapter.close_turn(
        CloseRuntimeTurn(
            "room-1",
            "close-1",
            "game-1",
            1,
            voted.snapshot.state_version,
            next_deadline_ms=8_000,
        )
    )
    assert closed.closure is not None
    assert closed.closure.result is TurnResultKind.RESOLUTION_REQUIRED
    assert closed.valid_voter_count == 2
    assert closed.snapshot.turn_status is TurnStatus.RESOLVING

    acquired = await adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "room-1", "lease-1", "game-1", 1, "resolver-1", closed.snapshot.state_version
        )
    )
    assert acquired.snapshot.resolver is not None
    assert acquired.snapshot.resolver.expires_at_ms == 1_000 + RESOLVER_LEASE_MS

    with pytest.raises(VoteRuleViolation, match="RESOLVER_LEASE_HELD"):
        await adapter.acquire_resolver(
            AcquireRuntimeResolver(
                "room-1",
                "lease-2",
                "game-1",
                1,
                "resolver-2",
                closed.snapshot.state_version,
            )
        )

    clock.advance(RESOLVER_LEASE_MS)
    handed_off = await adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "room-1", "lease-3", "game-1", 1, "resolver-2", closed.snapshot.state_version
        )
    )
    resolved = await adapter.apply_resolution(
        ApplyRuntimeResolution(
            room_id="room-1",
            request_id="resolve-1",
            game_id="game-1",
            turn_no=1,
            resolution_id="resolver-2",
            resolution=TurnResolution(
                game_id="game-1",
                turn_no=1,
                team=Stone.BLACK,
                result=TurnResultKind.MOVE_APPLIED,
                status=TurnStatus.MOVE_APPLIED,
                selected_coordinate=Coordinate.parse("H8"),
                applied_move=AppliedMove(1, Stone.BLACK, Coordinate.parse("H8")),
                end_reason=None,
            ),
            expected_state_version=handed_off.snapshot.state_version,
            persistence_confirmed=True,
            next_deadline_ms=8_000,
        )
    )
    assert resolved.resolution is not None
    assert resolved.resolution.result is TurnResultKind.MOVE_APPLIED
    assert resolved.snapshot.move_no == 1
    assert resolved.snapshot.turn_no == 2
    assert resolved.snapshot.consecutive_passes == 0
    assert resolved.snapshot.resolver is None


@pytest.mark.asyncio
async def test_zero_vote_pass_advances_turn_without_move_and_two_passes_end_game() -> None:
    clock = ManualClock()
    adapter, version = await initialized(clock)
    clock.advance(1_000)

    first = await adapter.close_turn(
        CloseRuntimeTurn("room-1", "close-1", "game-1", 1, version, next_deadline_ms=2_000)
    )
    assert first.closure is not None
    assert first.closure.result is TurnResultKind.PASSED
    assert first.snapshot.turn_no == 2
    assert first.snapshot.move_no == 0
    assert first.snapshot.consecutive_passes == 1

    clock.advance(1_000)
    second = await adapter.close_turn(
        CloseRuntimeTurn("room-1", "close-2", "game-1", 2, first.snapshot.state_version)
    )
    assert second.closure is not None
    assert second.closure.result is TurnResultKind.JOINT_LOSS
    assert second.snapshot.game_status is GameStatus.ACTIVE
    assert second.snapshot.move_no == 0
    acquired = await adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "room-1", "lease-joint", "game-1", 2, "resolver-joint", second.snapshot.state_version
        )
    )
    applied = await adapter.apply_resolution(
        ApplyRuntimeResolution(
            room_id="room-1",
            request_id="apply-joint",
            game_id="game-1",
            turn_no=2,
            resolution_id="resolver-joint",
            resolution=TurnResolution(
                game_id="game-1",
                turn_no=2,
                team=Stone.WHITE,
                result=TurnResultKind.JOINT_LOSS,
                status=TurnStatus.PASSED,
                selected_coordinate=None,
                applied_move=None,
                end_reason=EndReason.JOINT_LOSS,
            ),
            expected_state_version=acquired.snapshot.state_version,
            persistence_confirmed=True,
        )
    )
    assert applied.snapshot.game_status is GameStatus.FINISHED
    assert applied.snapshot.consecutive_passes == 2


def test_resolution_requires_explicit_persistence_confirmation() -> None:
    with pytest.raises(VoteRuleViolation, match="PERSISTENCE_CONFIRMATION_REQUIRED"):
        ApplyRuntimeResolution(
            room_id="room-1",
            request_id="resolve-1",
            game_id="game-1",
            turn_no=1,
            resolution_id="resolver-1",
            resolution=TurnResolution(
                game_id="game-1",
                turn_no=1,
                team=Stone.BLACK,
                result=TurnResultKind.MOVE_APPLIED,
                status=TurnStatus.MOVE_APPLIED,
                selected_coordinate=Coordinate.parse("A1"),
                applied_move=AppliedMove(1, Stone.BLACK, Coordinate.parse("A1")),
                end_reason=None,
            ),
            expected_state_version=2,
            persistence_confirmed=False,
        )
