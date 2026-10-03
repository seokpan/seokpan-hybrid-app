"""Provider-neutral Member identity values and validation rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

INITIAL_MEMBER_RATING = 1000
_LOGIN_ID_PATTERN = re.compile(r"[a-z0-9_]{4,20}")
_NICKNAME_PATTERN = re.compile(r"[A-Za-z0-9_\uac00-\ud7a3]{2,12}")


class MemberRuleViolation(ValueError):
    """A stable public rejection without secret-bearing context."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LoginId:
    value: str

    def __post_init__(self) -> None:
        if _LOGIN_ID_PATTERN.fullmatch(self.value) is None:
            raise MemberRuleViolation("INVALID_LOGIN_ID")


@dataclass(frozen=True, slots=True)
class Nickname:
    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if _NICKNAME_PATTERN.fullmatch(normalized) is None:
            raise MemberRuleViolation("INVALID_NICKNAME")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class PlainPassword:
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not 8 <= len(self.value) <= 64:
            raise MemberRuleViolation("INVALID_PASSWORD")


@dataclass(frozen=True, slots=True)
class Member:
    member_id: int
    login_id: str
    nickname: str
    rating: int = INITIAL_MEMBER_RATING

    def __post_init__(self) -> None:
        if self.member_id <= 0:
            raise MemberRuleViolation("INVALID_MEMBER_ID")
        LoginId(self.login_id)
        normalized_nickname = Nickname(self.nickname).value
        object.__setattr__(self, "nickname", normalized_nickname)
        if self.rating < 0:
            raise MemberRuleViolation("INVALID_MEMBER_RATING")
