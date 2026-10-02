"""Deterministic in-memory Member identity adapter."""

from __future__ import annotations

from dataclasses import replace

from seokpan.identity.application import (
    CreateMember,
    IdentityRuleViolation,
    StoredMember,
)
from seokpan.identity.domain import Member
from seokpan.statistics import MemberStatistics


class InMemoryIdentityAdapter:
    """A Fake for Identity contract tests; it is not MariaDB evidence."""

    def __init__(
        self, *, first_member_id: int = 1, member_ratings: dict[int, int] | None = None
    ) -> None:
        if first_member_id <= 0:
            raise ValueError("first_member_id must be positive")
        self._next_member_id = first_member_id
        self._by_login_id: dict[str, StoredMember] = {}
        self._by_nickname: dict[str, StoredMember] = {}
        self._by_member_id: dict[int, StoredMember] = {}
        self._member_ratings = {} if member_ratings is None else member_ratings

    async def create(self, command: CreateMember) -> StoredMember:
        if command.login_id in self._by_login_id:
            raise IdentityRuleViolation("LOGIN_ID_ALREADY_EXISTS")
        if command.nickname in self._by_nickname:
            raise IdentityRuleViolation("NICKNAME_ALREADY_EXISTS")
        stored = StoredMember(
            member=Member(
                member_id=self._next_member_id,
                login_id=command.login_id,
                nickname=command.nickname,
                rating=command.rating,
            ),
            password_hash=command.password_hash,
        )
        self._next_member_id += 1
        self._by_login_id[command.login_id] = stored
        self._by_nickname[command.nickname] = stored
        self._by_member_id[stored.member.member_id] = stored
        self._member_ratings[stored.member.member_id] = command.rating
        return stored

    async def find_by_login_id(self, login_id: str) -> StoredMember | None:
        return self._with_current_rating(self._by_login_id.get(login_id))

    async def find_by_nickname(self, nickname: str) -> StoredMember | None:
        return self._with_current_rating(self._by_nickname.get(nickname))

    async def find_by_member_id(self, member_id: int) -> StoredMember | None:
        return self._with_current_rating(self._by_member_id.get(member_id))

    def public_statistics_members(self) -> tuple[MemberStatistics, ...]:
        """Fake read projection: never pass login IDs or password hashes to rankings."""
        rows: list[MemberStatistics] = []
        for stored in self._by_member_id.values():
            current = self._with_current_rating(stored)
            assert current is not None
            member = current.member
            rows.append(MemberStatistics(member.member_id, member.nickname, member.rating))
        return tuple(rows)

    def _with_current_rating(self, stored: StoredMember | None) -> StoredMember | None:
        if stored is None:
            return None
        rating = self._member_ratings.get(stored.member.member_id)
        if rating is None:
            raise IdentityRuleViolation("IDENTITY_PROVIDER_UNAVAILABLE")
        return replace(stored, member=replace(stored.member, rating=rating))
