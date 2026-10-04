from unittest.mock import AsyncMock, patch

import pytest

from seokpan.persistence.redis.connection import (
    RedisConfigurationError,
    runtime_redis,
    validated_redis_url,
)
from seokpan.settings import Settings

MODULE = "seokpan.persistence.redis.connection"
OFFICIAL_URL = "redis://redis.platform.svc.cluster.local:6379/0"


@pytest.mark.parametrize(
    "url",
    [
        OFFICIAL_URL,
        "redis://redis.platform.svc.cluster.local/0",
    ],
)
def test_official_runtime_url_is_canonicalized(url: str) -> None:
    assert validated_redis_url(url) == OFFICIAL_URL


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "sensitive-malformed",
        OFFICIAL_URL + "\n",
        OFFICIAL_URL + "?ssl=false",
        OFFICIAL_URL + "#fragment",
        OFFICIAL_URL.replace("redis://", "rediss://"),
        OFFICIAL_URL.replace("redis.platform.svc.cluster.local", "10.1.93.91"),
        OFFICIAL_URL.replace(":6379", ":6380"),
        OFFICIAL_URL.replace("/0", "/1"),
        OFFICIAL_URL.replace("redis://", "redis://user:secret@"),
    ],
)
def test_runtime_url_fails_closed_without_echoing_input(url: str | None) -> None:
    with pytest.raises(RedisConfigurationError) as error:
        validated_redis_url(url)
    assert "secret" not in str(error.value)
    assert "sensitive" not in str(error.value)


@pytest.mark.asyncio
async def test_runtime_client_is_lazy_configured_and_closed() -> None:
    client = AsyncMock()
    with patch(MODULE + ".Redis.from_url", return_value=client) as create:
        async with runtime_redis(Settings(redis_url=OFFICIAL_URL)) as actual:
            assert actual is client
            client.ping.assert_not_awaited()
        client.aclose.assert_awaited_once()
    create.assert_called_once_with(
        OFFICIAL_URL,
        decode_responses=False,
        socket_connect_timeout=5.0,
        socket_timeout=5.0,
        retry_on_timeout=False,
    )


@pytest.mark.asyncio
async def test_runtime_client_closes_after_cancellation() -> None:
    client = AsyncMock()
    with (
        patch(MODULE + ".Redis.from_url", return_value=client),
        pytest.raises(BaseException, match="synthetic"),
    ):
        async with runtime_redis(Settings(redis_url=OFFICIAL_URL)):
            raise BaseException("synthetic")
    client.aclose.assert_awaited_once()
