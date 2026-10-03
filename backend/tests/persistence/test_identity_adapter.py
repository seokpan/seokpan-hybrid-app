from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from seokpan.identity.application import CreateMember, IdentityRuleViolation
from seokpan.persistence.mariadb.identity_adapter import MariaDBIdentityAdapter
from seokpan.persistence.mariadb.models import MemberRow, MemberStatsRow


class ResultBag:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalar_one_or_none(self) -> object | None:
        if len(self.rows) > 1:
            raise AssertionError("expected at most one row")
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(
        self,
        *,
        execute_results: list[list[object]] | None = None,
        fail_execute: bool = False,
        fail_flush_integrity: bool = False,
        fail_commit: bool = False,
        assigned_member_id: int = 1,
    ) -> None:
        self.execute_results = list(execute_results or [])
        self.fail_execute = fail_execute
        self.fail_flush_integrity = fail_flush_integrity
        self.fail_commit = fail_commit
        self.assigned_member_id = assigned_member_id
        self.added: list[object] = []
        self.begin_count = 0
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def begin(self) -> None:
        self.begin_count += 1

    async def execute(self, _statement: object) -> ResultBag:
        if self.fail_execute:
            raise SQLAlchemyError("sensitive-provider-detail")
        return ResultBag(self.execute_results.pop(0))

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_count += 1
        if self.fail_flush_integrity:
            raise IntegrityError("insert member", {}, RuntimeError("duplicate"))
        for row in self.added:
            if isinstance(row, MemberRow) and row.member_id is None:
                row.member_id = self.assigned_member_id

    async def commit(self) -> None:
        self.commit_count += 1
        if self.fail_commit:
            raise SQLAlchemyError("sensitive-provider-detail")

    async def rollback(self) -> None:
        self.rollback_count += 1


class SessionFactory:
    def __init__(self, *sessions: FakeSession) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> Any:
        return self.sessions.pop(0)


def command(**changes: object) -> CreateMember:
    values: dict[str, object] = {
        "login_id": "member_01",
        "nickname": "돌장인",
        "password_hash": "$argon2id$stored-hash",
        "rating": 1000,
    }
    values.update(changes)
    return CreateMember(**values)  # type: ignore[arg-type]


def member_row(**changes: object) -> MemberRow:
    values: dict[str, object] = {
        "member_id": 7,
        "login_id": "member_01",
        "nickname": "돌장인",
        "password_hash": "$argon2id$stored-hash",
        "rating": 1000,
    }
    values.update(changes)
    return MemberRow(**values)


@pytest.mark.asyncio
async def test_create_writes_only_member_in_one_transaction() -> None:
    session = FakeSession(execute_results=[[], []], assigned_member_id=7)
    adapter = MariaDBIdentityAdapter(SessionFactory(session))

    stored = await adapter.create(command())

    assert (stored.member.member_id, stored.member.rating) == (7, 1000)
    assert stored.password_hash == "$argon2id$stored-hash"
    assert (session.begin_count, session.flush_count, session.commit_count) == (1, 1, 1)
    assert len(session.added) == 1
    assert isinstance(session.added[0], MemberRow)
    assert not any(isinstance(row, MemberStatsRow) for row in session.added)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execute_results", "expected"),
    [
        ([[member_row()]], "LOGIN_ID_ALREADY_EXISTS"),
        ([[], [member_row()]], "NICKNAME_ALREADY_EXISTS"),
    ],
)
async def test_create_distinguishes_preexisting_unique_values(
    execute_results: list[list[object]],
    expected: str,
) -> None:
    session = FakeSession(execute_results=execute_results)

    with pytest.raises(IdentityRuleViolation, match=expected) as error:
        await MariaDBIdentityAdapter(SessionFactory(session)).create(command())

    assert error.value.code == expected
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_uncertain_commit_converges_when_exact_member_is_visible() -> None:
    writing = FakeSession(execute_results=[[], []], fail_commit=True, assigned_member_id=7)
    verifying = FakeSession(execute_results=[[member_row()], [member_row()]])
    adapter = MariaDBIdentityAdapter(SessionFactory(writing, verifying))

    stored = await adapter.create(command())

    assert stored.member.member_id == 7
    assert writing.rollback_count == 1


@pytest.mark.asyncio
async def test_uncertain_commit_without_visible_member_returns_stable_secret_free_error() -> None:
    writing = FakeSession(execute_results=[[], []], fail_commit=True)
    verifying = FakeSession(execute_results=[[], []])
    adapter = MariaDBIdentityAdapter(SessionFactory(writing, verifying))

    with pytest.raises(IdentityRuleViolation, match="IDENTITY_COMMIT_UNCERTAIN") as error:
        await adapter.create(command())

    assert "sensitive-provider-detail" not in str(error.value)


@pytest.mark.asyncio
async def test_find_maps_member_without_touching_other_tables() -> None:
    session = FakeSession(execute_results=[[member_row()]])

    stored = await MariaDBIdentityAdapter(SessionFactory(session)).find_by_login_id("member_01")

    assert stored is not None
    assert (stored.member.login_id, stored.member.nickname) == ("member_01", "돌장인")
    assert session.added == []


@pytest.mark.asyncio
async def test_find_by_member_id_maps_the_same_public_member() -> None:
    session = FakeSession(execute_results=[[member_row()]])

    stored = await MariaDBIdentityAdapter(SessionFactory(session)).find_by_member_id(7)

    assert stored is not None
    assert (stored.member.member_id, stored.member.nickname) == (7, "돌장인")


@pytest.mark.asyncio
async def test_find_hides_provider_details_behind_stable_error() -> None:
    session = FakeSession(fail_execute=True)

    with pytest.raises(IdentityRuleViolation, match="IDENTITY_PROVIDER_UNAVAILABLE") as error:
        await MariaDBIdentityAdapter(SessionFactory(session)).find_by_login_id("member_01")

    assert "sensitive-provider-detail" not in str(error.value)


@pytest.mark.asyncio
async def test_concurrent_unique_conflict_is_resolved_to_login_or_nickname_code() -> None:
    writing = FakeSession(
        execute_results=[[], []],
        fail_flush_integrity=True,
    )
    verifying = FakeSession(execute_results=[[member_row(password_hash="different")], []])

    with pytest.raises(IdentityRuleViolation, match="LOGIN_ID_ALREADY_EXISTS") as error:
        await MariaDBIdentityAdapter(SessionFactory(writing, verifying)).create(command())

    assert error.value.code == "LOGIN_ID_ALREADY_EXISTS"
    assert writing.rollback_count == 1


@pytest.mark.asyncio
async def test_integrity_failure_without_observable_conflict_is_provider_unavailable() -> None:
    writing = FakeSession(execute_results=[[], []], fail_flush_integrity=True)
    verifying = FakeSession(execute_results=[[], []])

    with pytest.raises(IdentityRuleViolation, match="IDENTITY_PROVIDER_UNAVAILABLE"):
        await MariaDBIdentityAdapter(SessionFactory(writing, verifying)).create(command())
