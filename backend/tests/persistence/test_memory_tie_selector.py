import pytest

from seokpan.game.application import TieSelectionRecord
from seokpan.persistence.memory import InMemoryTieSelectionAudit, InMemoryTieSelector


@pytest.mark.asyncio
async def test_default_fake_selection_uses_a_candidate_and_records_input() -> None:
    selector = InMemoryTieSelector()
    assert await selector.select(game_id="game", turn_no=2, candidates=("H8", "I8")) == "H8"
    assert selector.calls == [("game", 2, ("H8", "I8"))]


@pytest.mark.asyncio
async def test_default_fake_selection_rejects_empty_candidates() -> None:
    with pytest.raises(ValueError, match="^TIE_CANDIDATES_REQUIRED$"):
        await InMemoryTieSelector().select(game_id="game", turn_no=1, candidates=())


@pytest.mark.asyncio
async def test_repeated_tie_audit_does_not_duplicate_the_same_selection() -> None:
    audit = InMemoryTieSelectionAudit()
    record = TieSelectionRecord("game", 1, ("H8", "I8"), "H8")
    await audit.record(record)
    await audit.record(record)
    assert audit.records == [record]
