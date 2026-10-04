"""SQLAlchemy async adapter for Member identity data in MariaDB."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from seokpan.identity.application import (
    CreateMember,
    IdentityRuleViolation,
    StoredMember,
)
from seokpan.identity.domain import Member
from seokpan.persistence.mariadb.models import MemberRow

IdentitySessionFactory = Callable[[], AsyncSession]


class MariaDBIdentityAdapter:
    """Use only the identity_svc connection and the member table."""

    def __init__(self, session_factory: IdentitySessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, command: CreateMember) -> StoredMember:
        async with self._session_factory() as session:
            await session.begin()
            try:
                await self._reject_existing(session, command)
                row = MemberRow(
                    login_id=command.login_id,
                    nickname=command.nickname,
                    password_hash=command.password_hash,
                    rating=command.rating,
                )
                session.add(row)
                await session.flush()
                stored = self._stored(row)
                await session.commit()
                return stored
            except IdentityRuleViolation:
                await session.rollback()
                raise
            except IntegrityError as error:
                await session.rollback()
                return await self._resolve_create_error(command, error, conflict_expected=True)
            except SQLAlchemyError as error:
                await session.rollback()
                return await self._resolve_create_error(command, error, conflict_expected=False)

    async def find_by_login_id(self, login_id: str) -> StoredMember | None:
        return await self._find(MemberRow.login_id, login_id)

    async def find_by_nickname(self, nickname: str) -> StoredMember | None:
        return await self._find(MemberRow.nickname, nickname)

    async def find_by_member_id(self, member_id: int) -> StoredMember | None:
        return await self._find(MemberRow.member_id, member_id)

    async def _find(
        self,
        column: InstrumentedAttribute[str] | InstrumentedAttribute[int],
        value: str | int,
    ) -> StoredMember | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(select(MemberRow).where(column == value))
                ).scalar_one_or_none()
                return None if row is None else self._stored(row)
        except SQLAlchemyError as error:
            raise IdentityRuleViolation("IDENTITY_PROVIDER_UNAVAILABLE") from error

    async def _resolve_create_error(
        self,
        command: CreateMember,
        original_error: SQLAlchemyError,
        *,
        conflict_expected: bool,
    ) -> StoredMember:
        try:
            async with self._session_factory() as session:
                by_login = (
                    await session.execute(
                        select(MemberRow).where(MemberRow.login_id == command.login_id)
                    )
                ).scalar_one_or_none()
                by_nickname = (
                    await session.execute(
                        select(MemberRow).where(MemberRow.nickname == command.nickname)
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError as verify_error:
            code = (
                "IDENTITY_PROVIDER_UNAVAILABLE"
                if conflict_expected
                else "IDENTITY_COMMIT_UNCERTAIN"
            )
            raise IdentityRuleViolation(code) from verify_error

        if by_login is not None:
            if self._matches(by_login, command):
                return self._stored(by_login)
            raise IdentityRuleViolation("LOGIN_ID_ALREADY_EXISTS") from original_error
        if by_nickname is not None:
            raise IdentityRuleViolation("NICKNAME_ALREADY_EXISTS") from original_error
        code = "IDENTITY_PROVIDER_UNAVAILABLE" if conflict_expected else "IDENTITY_COMMIT_UNCERTAIN"
        raise IdentityRuleViolation(code) from original_error

    @staticmethod
    async def _reject_existing(session: AsyncSession, command: CreateMember) -> None:
        by_login = (
            await session.execute(select(MemberRow).where(MemberRow.login_id == command.login_id))
        ).scalar_one_or_none()
        if by_login is not None:
            raise IdentityRuleViolation("LOGIN_ID_ALREADY_EXISTS")
        by_nickname = (
            await session.execute(select(MemberRow).where(MemberRow.nickname == command.nickname))
        ).scalar_one_or_none()
        if by_nickname is not None:
            raise IdentityRuleViolation("NICKNAME_ALREADY_EXISTS")

    @staticmethod
    def _matches(row: MemberRow, command: CreateMember) -> bool:
        return (
            row.login_id == command.login_id
            and row.nickname == command.nickname
            and row.password_hash == command.password_hash
            and row.rating == command.rating
        )

    @staticmethod
    def _stored(row: MemberRow) -> StoredMember:
        return StoredMember(
            member=Member(
                member_id=row.member_id,
                login_id=row.login_id,
                nickname=row.nickname,
                rating=row.rating,
            ),
            password_hash=row.password_hash,
        )
