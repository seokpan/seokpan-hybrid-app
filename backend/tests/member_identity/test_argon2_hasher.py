import pytest

from seokpan.security import (
    PRODUCTION_ARGON2_PARAMETERS,
    Argon2Parameters,
    Argon2PasswordHasher,
)


def test_argon2id_hash_verify_and_rehash_boundary() -> None:
    parameters = Argon2Parameters(time_cost=1, memory_cost_kib=1024, parallelism=1)
    hasher = Argon2PasswordHasher(parameters)

    encoded = hasher.hash("correct-pass")

    assert encoded.startswith("$argon2id$")
    assert "correct-pass" not in encoded
    assert hasher.verify(encoded, "correct-pass") is True
    assert hasher.verify(encoded, "wrong-pass") is False
    assert hasher.needs_rehash(encoded) is False


def test_argon2_provider_reports_rehash_when_parameters_change() -> None:
    old = Argon2PasswordHasher(Argon2Parameters(time_cost=1, memory_cost_kib=1024, parallelism=1))
    current = Argon2PasswordHasher(
        Argon2Parameters(time_cost=2, memory_cost_kib=1024, parallelism=1)
    )

    assert current.needs_rehash(old.hash("correct-pass")) is True


def test_argon2_parameters_are_explicit() -> None:
    parameters = Argon2Parameters(time_cost=1, memory_cost_kib=1024, parallelism=1)
    assert parameters == Argon2Parameters(1, 1024, 1, 32, 16)


def test_production_parameters_match_linux_measurement() -> None:
    assert (
        Argon2Parameters(
            time_cost=3,
            memory_cost_kib=64 * 1024,
            parallelism=1,
        )
        == PRODUCTION_ARGON2_PARAMETERS
    )
    assert PRODUCTION_ARGON2_PARAMETERS.memory_cost_kib > 8 * 1024


def test_argon2_parameter_validation_rejects_unsafe_shape() -> None:
    with pytest.raises(ValueError):
        Argon2Parameters(time_cost=0, memory_cost_kib=1024, parallelism=1)
    with pytest.raises(ValueError):
        Argon2Parameters(time_cost=1, memory_cost_kib=7, parallelism=1)


def test_argon2_invalid_encoded_hash_is_secret_free_failure() -> None:
    hasher = Argon2PasswordHasher(
        Argon2Parameters(time_cost=1, memory_cost_kib=1024, parallelism=1)
    )

    assert hasher.verify("not-an-argon2-hash", "correct-pass") is False
