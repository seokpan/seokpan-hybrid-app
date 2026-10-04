import pytest

from seokpan.identity.domain import LoginId, Member, MemberRuleViolation, Nickname, PlainPassword


@pytest.mark.parametrize("value", ["abcd", "member_01", "a" * 20])
def test_login_id_accepts_documented_boundary(value: str) -> None:
    assert LoginId(value).value == value


@pytest.mark.parametrize("value", ["abc", "a" * 21, "Member", "member-1", "회원"])
def test_login_id_rejects_invalid_length_or_characters(value: str) -> None:
    with pytest.raises(MemberRuleViolation, match="INVALID_LOGIN_ID"):
        LoginId(value)


@pytest.mark.parametrize("value", ["가나", "member_01", "가A_1", "a" * 12])
def test_nickname_accepts_documented_characters(value: str) -> None:
    assert Nickname(f"  {value}  ").value == value


@pytest.mark.parametrize("value", ["가", "a" * 13, "name space", "name-"])
def test_nickname_rejects_invalid_normalized_value(value: str) -> None:
    with pytest.raises(MemberRuleViolation, match="INVALID_NICKNAME"):
        Nickname(value)


@pytest.mark.parametrize("value", ["a" * 8, "비밀번호1234", "x" * 64])
def test_password_accepts_length_boundary_without_normalization(value: str) -> None:
    password = PlainPassword(value)
    assert password.value == value
    assert value not in repr(password)


@pytest.mark.parametrize("value", ["x" * 7, "x" * 65])
def test_password_rejects_outside_length_boundary(value: str) -> None:
    with pytest.raises(MemberRuleViolation, match="INVALID_PASSWORD"):
        PlainPassword(value)


def test_member_public_value_enforces_identity_and_rating() -> None:
    member = Member(member_id=1, login_id="member_01", nickname="  돌장인  ")
    assert (member.nickname, member.rating) == ("돌장인", 1000)


@pytest.mark.parametrize(
    ("member_id", "rating", "code"),
    [(0, 1000, "INVALID_MEMBER_ID"), (1, -1, "INVALID_MEMBER_RATING")],
)
def test_member_rejects_invalid_provider_values(member_id: int, rating: int, code: str) -> None:
    with pytest.raises(MemberRuleViolation, match=code):
        Member(member_id=member_id, login_id="member_01", nickname="돌장인", rating=rating)
