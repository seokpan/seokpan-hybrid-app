"""Cryptographically strong opaque token generation."""

from __future__ import annotations

import secrets


class SecretsTokenSource:
    def issue(self) -> str:
        return secrets.token_urlsafe(32)
