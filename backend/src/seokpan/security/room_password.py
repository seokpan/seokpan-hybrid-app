"""Async Room password boundary backed by the configured Argon2id provider."""

from seokpan.security.argon2_hasher import Argon2PasswordHasher


class Argon2RoomPassword:
    def __init__(self, hasher: Argon2PasswordHasher) -> None:
        self._hasher = hasher

    async def encode(self, raw_password: str) -> str:
        return self._hasher.hash(raw_password)

    async def verify(self, encoded_password: str, candidate_password: str) -> bool:
        return self._hasher.verify(encoded_password, candidate_password)
