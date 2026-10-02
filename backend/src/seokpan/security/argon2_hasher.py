"""Explicitly configured Argon2id password hashing provider."""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher, Type, exceptions

from seokpan.identity.application import IdentityRuleViolation


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    time_cost: int
    memory_cost_kib: int
    parallelism: int
    hash_len: int = 32
    salt_len: int = 16

    def __post_init__(self) -> None:
        if self.time_cost <= 0 or self.parallelism <= 0:
            raise ValueError("Argon2 time_cost and parallelism must be positive")
        if self.memory_cost_kib < 8 * self.parallelism:
            raise ValueError("Argon2 memory_cost_kib must be at least 8 * parallelism")
        if self.hash_len < 4 or self.salt_len < 8:
            raise ValueError("Argon2 hash_len or salt_len is too small")


# Measured in the pinned Backend Linux image on worker-02 with a 1 CPU/512 MiB
# benchmark envelope (2026-09-14): hash median 110.186 ms, verify 110.034 ms.
PRODUCTION_ARGON2_PARAMETERS = Argon2Parameters(
    time_cost=3,
    memory_cost_kib=64 * 1024,
    parallelism=1,
)


class Argon2PasswordHasher:
    """Argon2id provider with no implicit production cost defaults."""

    def __init__(self, parameters: Argon2Parameters) -> None:
        self._hasher = PasswordHasher(
            time_cost=parameters.time_cost,
            memory_cost=parameters.memory_cost_kib,
            parallelism=parameters.parallelism,
            hash_len=parameters.hash_len,
            salt_len=parameters.salt_len,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        try:
            return self._hasher.hash(password)
        except exceptions.HashingError as error:
            raise IdentityRuleViolation("PASSWORD_HASH_UNAVAILABLE") from error

    def verify(self, encoded_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded_hash, password)
        except (exceptions.Argon2Error, exceptions.InvalidHashError):
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except (exceptions.Argon2Error, exceptions.InvalidHashError) as error:
            raise IdentityRuleViolation("PASSWORD_HASH_INVALID") from error
