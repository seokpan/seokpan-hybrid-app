from __future__ import annotations

import pytest

from seokpan.identity.application import (
    AuthenticateMember,
    CreateMember,
    IdentityRuleViolation,
    MemberIdentityService,
    RegisterMember,
)
from seokpan.persistence.memory import InMemoryIdentityAdapter


class RecordingPasswordHasher:
    def __init__(self, *, needs_rehash: bool = False) -> None:
        self.hash_inputs: list[str] = []
        self.verify_inputs: list[tuple[str, str]] = []
        self.rehash_inputs: list[str] = []
        self._needs_rehash = needs_rehash

    def hash(self, password: str) -> str:
        self.hash_inputs.append(password)
        return "$argon2id$test-member-hash"

    def verify(self, encoded_hash: str, password: str) -> bool:
        self.verify_inputs.append((encoded_hash, password))
        return encoded_hash == "$argon2id$test-member-hash" and password == "correct-pass"

    def needs_rehash(self, encoded_hash: str) -> bool:
        self.rehash_inputs.append(encoded_hash)
        return self._needs_rehash


def service(
    persistence: InMemoryIdentityAdapter,
    hasher: RecordingPasswordHasher,
) -> MemberIdentityService:
    return MemberIdentityService(
        persistence,
        hasher,
        dummy_password_hash="$argon2id$dummy-member-hash",
    )


@pytest.mark.asyncio
async def test_registration_normalizes_nickname_hashes_password_and_uses_initial_rating() -> None:
    persistence = InMemoryIdentityAdapter()
    hasher = RecordingPasswordHasher()

    member = await service(persistence, hasher).register(
        RegisterMember("member_01", "  돌장인  ", "correct-pass")
    )

    stored = await persistence.find_by_login_id("member_01")
    assert (member.member_id, member.nickname, member.rating) == (1, "돌장인", 1000)
    assert stored is not None
    assert stored.password_hash == "$argon2id$test-member-hash"
    assert hasher.hash_inputs == ["correct-pass"]
    assert "correct-pass" not in repr(RegisterMember("member_01", "돌장인", "correct-pass"))


@pytest.mark.asyncio
async def test_registration_distinguishes_login_and_nickname_conflicts() -> None:
    persistence = InMemoryIdentityAdapter()
    hasher = RecordingPasswordHasher()
    identity = service(persistence, hasher)
    await identity.register(RegisterMember("member_01", "돌장인", "correct-pass"))

    with pytest.raises(IdentityRuleViolation, match="LOGIN_ID_ALREADY_EXISTS"):
        await identity.register(RegisterMember("member_01", "다른이름", "correct-pass"))
    with pytest.raises(IdentityRuleViolation, match="NICKNAME_ALREADY_EXISTS"):
        await identity.register(RegisterMember("member_02", "돌장인", "correct-pass"))


@pytest.mark.asyncio
async def test_authentication_returns_member_and_rehash_signal() -> None:
    persistence = InMemoryIdentityAdapter()
    hasher = RecordingPasswordHasher(needs_rehash=True)
    identity = service(persistence, hasher)
    await persistence.create(CreateMember("member_01", "돌장인", "$argon2id$test-member-hash"))

    result = await identity.authenticate(AuthenticateMember("member_01", "correct-pass"))

    assert result.member.member_id == 1
    assert result.password_rehash_required is True
    assert hasher.rehash_inputs == ["$argon2id$test-member-hash"]


@pytest.mark.asyncio
async def test_find_member_returns_public_member_without_password_hash() -> None:
    persistence = InMemoryIdentityAdapter()
    identity = service(persistence, RecordingPasswordHasher())
    stored = await persistence.create(
        CreateMember("member_01", "돌장인", "$argon2id$test-member-hash")
    )

    member = await identity.find_member(stored.member.member_id)

    assert member == stored.member
    assert "$argon2id$" not in repr(member)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login_id", "password", "expected_hash"),
    [
        ("member_01", "wrong-pass", "$argon2id$test-member-hash"),
        ("missing_01", "wrong-pass", "$argon2id$dummy-member-hash"),
    ],
)
async def test_authentication_hides_account_existence_and_performs_password_verification(
    login_id: str,
    password: str,
    expected_hash: str,
) -> None:
    persistence = InMemoryIdentityAdapter()
    hasher = RecordingPasswordHasher()
    identity = service(persistence, hasher)
    await persistence.create(CreateMember("member_01", "돌장인", "$argon2id$test-member-hash"))

    with pytest.raises(IdentityRuleViolation, match="AUTH_INVALID_CREDENTIALS") as error:
        await identity.authenticate(AuthenticateMember(login_id, password))

    assert error.value.code == "AUTH_INVALID_CREDENTIALS"
    assert hasher.verify_inputs[-1] == (expected_hash, password)
    assert login_id not in str(error.value)
    assert password not in str(error.value)


@pytest.mark.asyncio
async def test_application_maps_domain_validation_to_stable_identity_error() -> None:
    identity = service(InMemoryIdentityAdapter(), RecordingPasswordHasher())

    with pytest.raises(IdentityRuleViolation, match="INVALID_LOGIN_ID") as error:
        await identity.register(RegisterMember("BAD", "돌장인", "correct-pass"))

    assert error.value.code == "INVALID_LOGIN_ID"


def test_service_requires_explicit_dummy_hash() -> None:
    with pytest.raises(IdentityRuleViolation, match="DUMMY_PASSWORD_HASH_REQUIRED"):
        MemberIdentityService(
            InMemoryIdentityAdapter(), RecordingPasswordHasher(), dummy_password_hash=""
        )
