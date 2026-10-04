"""Member registration and authentication application boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from seokpan.identity.domain import (
    INITIAL_MEMBER_RATING,
    LoginId,
    Member,
    MemberRuleViolation,
    Nickname,
    PlainPassword,
)


class IdentityRuleViolation(ValueError):
    """A stable Identity rejection without provider or secret details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RegisterMember:
    login_id: str
    nickname: str
    password: str = field(repr=False)

    def validated(self) -> tuple[LoginId, Nickname, PlainPassword]:
        return LoginId(self.login_id), Nickname(self.nickname), PlainPassword(self.password)


@dataclass(frozen=True, slots=True)
class AuthenticateMember:
    login_id: str
    password: str = field(repr=False)

    def validated(self) -> tuple[LoginId, PlainPassword]:
        return LoginId(self.login_id), PlainPassword(self.password)


@dataclass(frozen=True, slots=True)
class CreateMember:
    login_id: str
    nickname: str
    password_hash: str = field(repr=False)
    rating: int = INITIAL_MEMBER_RATING

    def __post_init__(self) -> None:
        LoginId(self.login_id)
        normalized_nickname = Nickname(self.nickname).value
        object.__setattr__(self, "nickname", normalized_nickname)
        if not self.password_hash:
            raise IdentityRuleViolation("INVALID_PASSWORD_HASH")
        if self.rating != INITIAL_MEMBER_RATING:
            raise IdentityRuleViolation("INVALID_INITIAL_RATING")


@dataclass(frozen=True, slots=True)
class StoredMember:
    member: Member
    password_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.password_hash:
            raise IdentityRuleViolation("INVALID_PASSWORD_HASH")


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    member: Member
    password_rehash_required: bool


class PasswordHashPort(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, encoded_hash: str, password: str) -> bool: ...

    def needs_rehash(self, encoded_hash: str) -> bool: ...


class IdentityPersistencePort(Protocol):
    async def create(self, command: CreateMember) -> StoredMember: ...

    async def find_by_login_id(self, login_id: str) -> StoredMember | None: ...

    async def find_by_nickname(self, nickname: str) -> StoredMember | None: ...

    async def find_by_member_id(self, member_id: int) -> StoredMember | None: ...


class MemberIdentityService:
    """Coordinate validation, password hashing, and Member persistence."""

    def __init__(
        self,
        persistence: IdentityPersistencePort,
        password_hasher: PasswordHashPort,
        *,
        dummy_password_hash: str,
    ) -> None:
        if not dummy_password_hash:
            raise IdentityRuleViolation("DUMMY_PASSWORD_HASH_REQUIRED")
        self._persistence = persistence
        self._password_hasher = password_hasher
        self._dummy_password_hash = dummy_password_hash

    async def register(self, request: RegisterMember) -> Member:
        try:
            login_id, nickname, password = request.validated()
        except MemberRuleViolation as error:
            raise IdentityRuleViolation(error.code) from error
        if await self._persistence.find_by_login_id(login_id.value) is not None:
            raise IdentityRuleViolation("LOGIN_ID_ALREADY_EXISTS")
        if await self._persistence.find_by_nickname(nickname.value) is not None:
            raise IdentityRuleViolation("NICKNAME_ALREADY_EXISTS")
        encoded_hash = self._password_hasher.hash(password.value)
        stored = await self._persistence.create(
            CreateMember(
                login_id=login_id.value,
                nickname=nickname.value,
                password_hash=encoded_hash,
            )
        )
        return stored.member

    async def authenticate(self, request: AuthenticateMember) -> AuthenticationResult:
        try:
            login_id, password = request.validated()
        except MemberRuleViolation as error:
            raise IdentityRuleViolation(error.code) from error
        stored = await self._persistence.find_by_login_id(login_id.value)
        encoded_hash = stored.password_hash if stored is not None else self._dummy_password_hash
        verified = self._password_hasher.verify(encoded_hash, password.value)
        if stored is None or not verified:
            raise IdentityRuleViolation("AUTH_INVALID_CREDENTIALS")
        return AuthenticationResult(
            member=stored.member,
            password_rehash_required=self._password_hasher.needs_rehash(stored.password_hash),
        )

    async def find_member(self, member_id: int) -> Member | None:
        if member_id <= 0:
            raise IdentityRuleViolation("INVALID_MEMBER_ID")
        stored = await self._persistence.find_by_member_id(member_id)
        return None if stored is None else stored.member
