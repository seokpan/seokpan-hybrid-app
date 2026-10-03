from __future__ import annotations

import pytest

from seokpan.game.domain import AppliedMove, Coordinate, EndReason, GameStatus, Stone
from seokpan.vote.application import (
    AcquireRuntimeResolver,
    ApplyRuntimeResolution,
    CastRuntimeVote,
    CloseRuntimeTurn,
    InitializeVoteRuntime,
)
from seokpan.vote.domain import (
    ParticipantRole,
    TurnResolution,
    TurnResultKind,
    TurnStatus,
    Voter,
    VoteRuleViolation,
)

from .conftest import VoteRuntimeHarness


def participants() -> tuple[Voter, ...]:
    return (
        Voter("black-1", Stone.BLACK),
        Voter("black-2", Stone.BLACK),
        Voter("white-1", Stone.WHITE),
        Voter("spectator-1", Stone.BLACK, ParticipantRole.SPECTATOR),
    )


async def initialize(harness: VoteRuntimeHarness) -> int:
    result = await harness.adapter.initialize(
        InitializeVoteRuntime(
            "00000000-0000-4000-8000-000000000101",
            "initialize-1",
            "00000000-0000-4000-8000-000000000102",
            participants(),
            1_000,
            1,
        )
    )
    return result.snapshot.state_version


@pytest.mark.asyncio
async def test_discard_game_is_idempotent_and_rejects_a_different_game(
    vote_harness: VoteRuntimeHarness,
) -> None:
    await initialize(vote_harness)

    with pytest.raises(VoteRuleViolation, match="STALE_GAME"):
        await vote_harness.adapter.discard_game(
            "00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000104"
        )

    await vote_harness.adapter.discard_game(
        "00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000102"
    )
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") is None

    await vote_harness.adapter.discard_game(
        "00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000102"
    )
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") is None


@pytest.mark.asyncio
async def test_vote_close_resolver_and_move_flow(vote_harness: VoteRuntimeHarness) -> None:
    version = await initialize(vote_harness)
    first = await vote_harness.adapter.cast_vote(
        CastRuntimeVote(
            "00000000-0000-4000-8000-000000000101",
            "vote-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            "black-1",
            Coordinate.parse("H8"),
            version,
        )
    )
    assert first.snapshot.last_move is None
    second = await vote_harness.adapter.cast_vote(
        CastRuntimeVote(
            "00000000-0000-4000-8000-000000000101",
            "vote-2",
            "00000000-0000-4000-8000-000000000102",
            1,
            "black-2",
            Coordinate.parse("H8"),
            first.snapshot.state_version,
        )
    )
    vote_harness.clock.advance(1_000)
    closed = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "close-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            second.snapshot.state_version,
            next_deadline_ms=8_000,
        )
    )
    assert closed.closure is not None
    assert closed.closure.candidates == (Coordinate.parse("H8"),)
    assert closed.valid_voter_count == 2
    replayed_close = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "close-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            second.snapshot.state_version,
            next_deadline_ms=8_000,
        )
    )
    assert replayed_close.replayed is True
    assert replayed_close.closure == closed.closure
    assert replayed_close.snapshot == closed.snapshot

    lease = await vote_harness.adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "00000000-0000-4000-8000-000000000101",
            "lease-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            "resolver-1",
            closed.snapshot.state_version,
        )
    )
    assert lease.snapshot.candidates == (Coordinate.parse("H8"),)
    resolution = TurnResolution(
        game_id="00000000-0000-4000-8000-000000000102",
        turn_no=1,
        team=Stone.BLACK,
        result=TurnResultKind.MOVE_APPLIED,
        status=TurnStatus.MOVE_APPLIED,
        selected_coordinate=Coordinate.parse("H8"),
        applied_move=AppliedMove(1, Stone.BLACK, Coordinate.parse("H8")),
        end_reason=None,
    )
    resolve_command = ApplyRuntimeResolution(
        room_id="00000000-0000-4000-8000-000000000101",
        request_id="resolve-1",
        game_id="00000000-0000-4000-8000-000000000102",
        turn_no=1,
        resolution_id="resolver-1",
        resolution=resolution,
        expected_state_version=lease.snapshot.state_version,
        persistence_confirmed=True,
        next_deadline_ms=8_000,
    )
    applied = await vote_harness.adapter.apply_resolution(resolve_command)
    assert applied.resolution == resolution
    assert applied.snapshot.turn_no == 2
    assert applied.snapshot.move_no == 1
    assert applied.snapshot.last_move == resolution.applied_move
    assert applied.snapshot.occupied_cells[0].coordinate == Coordinate.parse("H8")
    assert (
        await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") == applied.snapshot
    )
    replayed_resolution = await vote_harness.adapter.apply_resolution(resolve_command)
    assert replayed_resolution.replayed is True
    assert replayed_resolution.resolution == resolution
    assert replayed_resolution.snapshot == applied.snapshot
    vote_harness.clock.advance(7_000)
    passed = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "pass-after-move",
            "00000000-0000-4000-8000-000000000102",
            2,
            applied.snapshot.state_version,
            next_deadline_ms=9_000,
        )
    )
    assert passed.snapshot.turn_no == 3
    assert passed.snapshot.last_move == applied.snapshot.last_move
    assert passed.snapshot.move_no == 1


@pytest.mark.asyncio
async def test_tie_requires_external_candidate_selection(
    vote_harness: VoteRuntimeHarness,
) -> None:
    version = await initialize(vote_harness)
    first = await vote_harness.adapter.cast_vote(
        CastRuntimeVote(
            "00000000-0000-4000-8000-000000000101",
            "vote-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            "black-1",
            Coordinate.parse("A1"),
            version,
        )
    )
    second = await vote_harness.adapter.cast_vote(
        CastRuntimeVote(
            "00000000-0000-4000-8000-000000000101",
            "vote-2",
            "00000000-0000-4000-8000-000000000102",
            1,
            "black-2",
            Coordinate.parse("B1"),
            first.snapshot.state_version,
        )
    )
    vote_harness.clock.advance(1_000)
    closed = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "close-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            second.snapshot.state_version,
            next_deadline_ms=8_000,
        )
    )
    assert closed.closure is not None
    assert closed.closure.candidates == (Coordinate.parse("A1"), Coordinate.parse("B1"))


@pytest.mark.asyncio
async def test_zero_vote_pass_and_joint_loss_keep_move_number(
    vote_harness: VoteRuntimeHarness,
) -> None:
    version = await initialize(vote_harness)
    vote_harness.clock.advance(1_000)
    first = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "close-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            version,
            next_deadline_ms=2_000,
        )
    )
    assert first.snapshot.turn_no == 2
    assert first.snapshot.move_no == 0
    assert first.snapshot.last_move is None
    vote_harness.clock.advance(1_000)
    second = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "close-2",
            "00000000-0000-4000-8000-000000000102",
            2,
            first.snapshot.state_version,
        )
    )
    assert second.closure is not None
    assert second.closure.result is TurnResultKind.JOINT_LOSS
    assert second.snapshot.game_status is GameStatus.ACTIVE
    assert second.snapshot.move_no == 0
    acquired = await vote_harness.adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "00000000-0000-4000-8000-000000000101",
            "lease-joint",
            "00000000-0000-4000-8000-000000000102",
            2,
            "resolver-joint",
            second.snapshot.state_version,
        )
    )
    applied = await vote_harness.adapter.apply_resolution(
        ApplyRuntimeResolution(
            room_id="00000000-0000-4000-8000-000000000101",
            request_id="apply-joint",
            game_id="00000000-0000-4000-8000-000000000102",
            turn_no=2,
            resolution_id="resolver-joint",
            resolution=TurnResolution(
                game_id="00000000-0000-4000-8000-000000000102",
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


@pytest.mark.asyncio
async def test_stale_version_and_request_id_conflict_do_not_mutate(
    vote_harness: VoteRuntimeHarness,
) -> None:
    version = await initialize(vote_harness)
    command = CastRuntimeVote(
        "00000000-0000-4000-8000-000000000101",
        "vote-1",
        "00000000-0000-4000-8000-000000000102",
        1,
        "black-1",
        Coordinate.parse("A1"),
        version,
    )
    first = await vote_harness.adapter.cast_vote(command)
    replay = await vote_harness.adapter.cast_vote(command)
    assert replay.replayed
    assert replay.snapshot == first.snapshot

    with pytest.raises(VoteRuleViolation, match="REQUEST_ID_CONFLICT"):
        await vote_harness.adapter.cast_vote(
            CastRuntimeVote(
                "00000000-0000-4000-8000-000000000101",
                "vote-1",
                "00000000-0000-4000-8000-000000000102",
                1,
                "black-1",
                Coordinate.parse("B1"),
                first.snapshot.state_version,
            )
        )
    with pytest.raises(VoteRuleViolation, match="STATE_VERSION_CONFLICT"):
        await vote_harness.adapter.cast_vote(
            CastRuntimeVote(
                "00000000-0000-4000-8000-000000000101",
                "vote-stale",
                "00000000-0000-4000-8000-000000000102",
                1,
                "black-2",
                Coordinate.parse("B1"),
                99,
            )
        )
    assert await vote_harness.adapter.get("00000000-0000-4000-8000-000000000101") == first.snapshot


@pytest.mark.asyncio
async def test_role_team_and_deadline_rejections_match_adapters(
    vote_harness: VoteRuntimeHarness,
) -> None:
    version = await initialize(vote_harness)
    for participant_id, code in (
        ("white-1", "CURRENT_TEAM_REQUIRED"),
        ("spectator-1", "PLAYER_REQUIRED"),
    ):
        with pytest.raises(VoteRuleViolation, match=code):
            await vote_harness.adapter.cast_vote(
                CastRuntimeVote(
                    "00000000-0000-4000-8000-000000000101",
                    f"vote-{participant_id}",
                    "00000000-0000-4000-8000-000000000102",
                    1,
                    participant_id,
                    Coordinate.parse("A1"),
                    version,
                )
            )
    vote_harness.clock.advance(1_000)
    with pytest.raises(VoteRuleViolation, match="TURN_DEADLINE_REACHED"):
        await vote_harness.adapter.cast_vote(
            CastRuntimeVote(
                "00000000-0000-4000-8000-000000000101",
                "vote-late",
                "00000000-0000-4000-8000-000000000102",
                1,
                "black-1",
                Coordinate.parse("A1"),
                version,
            )
        )


@pytest.mark.asyncio
async def test_resolver_lease_excludes_competitor_and_allows_expiry_handoff(
    vote_harness: VoteRuntimeHarness,
) -> None:
    version = await initialize(vote_harness)
    voted = await vote_harness.adapter.cast_vote(
        CastRuntimeVote(
            "00000000-0000-4000-8000-000000000101",
            "vote-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            "black-1",
            Coordinate.parse("A1"),
            version,
        )
    )
    vote_harness.clock.advance(1_000)
    closed = await vote_harness.adapter.close_turn(
        CloseRuntimeTurn(
            "00000000-0000-4000-8000-000000000101",
            "close-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            voted.snapshot.state_version,
            next_deadline_ms=8_000,
        )
    )
    acquired = await vote_harness.adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "00000000-0000-4000-8000-000000000101",
            "lease-1",
            "00000000-0000-4000-8000-000000000102",
            1,
            "resolver-1",
            closed.snapshot.state_version,
        )
    )
    vote_harness.clock.advance(1_000)
    renewed = await vote_harness.adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "00000000-0000-4000-8000-000000000101",
            "lease-renew",
            "00000000-0000-4000-8000-000000000102",
            1,
            "resolver-1",
            closed.snapshot.state_version,
        )
    )
    assert acquired.snapshot.resolver is not None
    assert renewed.snapshot.resolver is not None
    assert (
        renewed.snapshot.resolver.expires_at_ms == acquired.snapshot.resolver.expires_at_ms + 1_000
    )
    with pytest.raises(VoteRuleViolation, match="RESOLVER_LEASE_HELD"):
        await vote_harness.adapter.acquire_resolver(
            AcquireRuntimeResolver(
                "00000000-0000-4000-8000-000000000101",
                "lease-2",
                "00000000-0000-4000-8000-000000000102",
                1,
                "resolver-2",
                closed.snapshot.state_version,
            )
        )
    vote_harness.clock.advance(5_000)
    handed_off = await vote_harness.adapter.acquire_resolver(
        AcquireRuntimeResolver(
            "00000000-0000-4000-8000-000000000101",
            "lease-3",
            "00000000-0000-4000-8000-000000000102",
            1,
            "resolver-2",
            closed.snapshot.state_version,
        )
    )
    assert handed_off.snapshot.resolver is not None
    assert handed_off.snapshot.resolver.resolution_id == "resolver-2"
