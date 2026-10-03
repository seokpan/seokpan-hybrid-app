"""Pinned redis-py over loopback TLS to a synthetic RESP peer, never Cloud Redis."""

import asyncio
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import pytest
from redis.exceptions import AuthenticationError, ConnectionError
from test_database_tls_handshake import certificates as certificates
from test_database_tls_handshake import handshake

from seokpan.connection_contract import strict_ca_context
from seokpan.persistence.mariadb.connection import DATABASE_HOST
from seokpan.persistence.redis.connection import ExplicitCATLSConnection, runtime_redis
from seokpan.settings import Settings

TOKEN = "synthetic-redis-auth"


@pytest.mark.parametrize(
    ("leaf", "host", "valid"),
    [
        ("valid", DATABASE_HOST, True),
        ("valid", "wrong.example.test", False),
        ("expired", DATABASE_HOST, False),
        ("future", DATABASE_HOST, False),
    ],
)
def test_pinned_driver_selected_context_checks_validity_and_hostname(
    certificates: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
    host: str,
    valid: bool,
) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    context = strict_ca_context(str(certificates / "ca.crt"))
    connection = ExplicitCATLSConnection(strict_context=context, host=DATABASE_HOST, password=TOKEN)
    assert connection._connection_arguments()["ssl"] is context
    if valid:
        assert handshake(context, certificates, leaf, host) in {"TLSv1.2", "TLSv1.3"}
    else:
        with pytest.raises(ssl.SSLCertVerificationError):
            handshake(context, certificates, leaf, host)


@pytest.fixture(scope="module")
def redis_certificates(certificates: Path) -> Path:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("loopback TLS verification requires OpenSSL")

    def run(*args: str) -> None:
        result = subprocess.run(
            [openssl, *args], cwd=certificates, capture_output=True, timeout=30, check=False
        )
        assert result.returncode == 0, "ephemeral Redis TLS certificate generation failed"

    run(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "redis.key",
        "-out",
        "redis.csr",
        "-subj",
        "/CN=localhost",
    )
    (certificates / "redis.ext").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\nsubjectAltName=DNS:localhost\n",
        encoding="ascii",
    )
    run(
        "x509",
        "-req",
        "-in",
        "redis.csr",
        "-CA",
        "ca.crt",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "redis.crt",
        "-days",
        "1",
        "-extfile",
        "redis.ext",
    )
    return certificates


@asynccontextmanager
async def tls_peer(directory: Path) -> AsyncIterator[tuple[int, list[list[bytes]]]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "redis.crt", directory / "redis.key")
    observed: list[list[bytes]] = []
    writers: list[asyncio.StreamWriter] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                assert header.startswith(b"*")
                command = []
                for _ in range(int(header[1:])):
                    size = await reader.readline()
                    assert size.startswith(b"$")
                    command.append(await reader.readexactly(int(size[1:])))
                    assert await reader.readexactly(2) == b"\r\n"
                observed.append(command)
                if command[0] == b"HELLO":
                    response = (
                        b"%1\r\n$5\r\nproto\r\n:3\r\n"
                        if command[-1] == TOKEN.encode()
                        else b"-WRONGPASS invalid credentials\r\n"
                    )
                elif command[0] == b"AUTH":
                    response = (
                        b"+OK\r\n"
                        if command[-1] == TOKEN.encode()
                        else b"-WRONGPASS invalid credentials\r\n"
                    )
                elif command[0] == b"PING":
                    response = b"+PONG\r\n"
                else:
                    response = b"+OK\r\n"
                writer.write(response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1], observed
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()


def settings(port: int, ca: Path, *, host: str = "localhost", token: str = TOKEN) -> Settings:
    return Settings(
        connection_profile="lab",
        redis_expected_host=host,
        redis_expected_port=port,
        redis_expected_database=0,
        redis_url=f"rediss://{host}:{port}/0",
        redis_auth_token=token,
        redis_ca_file=str(ca),
    )


@pytest.mark.asyncio
async def test_pinned_driver_performs_tls_separate_auth_and_ping(
    redis_certificates: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    async with tls_peer(redis_certificates) as (port, observed):
        async with runtime_redis(settings(port, redis_certificates / "ca.crt")) as client:
            assert await client.ping()
        assert any(
            command[0] in {b"AUTH", b"HELLO"} and command[-1] == TOKEN.encode()
            for command in observed
        )
        assert any(command[0] == b"PING" for command in observed)


@pytest.mark.parametrize("failure", ["auth", "ca", "hostname"])
@pytest.mark.asyncio
async def test_pinned_driver_rejects_wrong_auth_ca_or_hostname(
    failure: str,
    redis_certificates: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    async with tls_peer(redis_certificates) as (port, observed):
        configured = settings(
            port,
            (Path(__file__).parent / "fixtures/public-ca.crt")
            if failure == "ca"
            else redis_certificates / "ca.crt",
            host="127.0.0.1" if failure == "hostname" else "localhost",
            token="synthetic-wrong-auth" if failure == "auth" else TOKEN,
        )
        expected = AuthenticationError if failure == "auth" else ConnectionError
        with pytest.raises(expected) as error:
            async with runtime_redis(configured) as client:
                await client.ping()
        assert "synthetic-wrong-auth" not in str(error.value)
        assert TOKEN not in str(error.value)
        assert not any(command[0] == b"PING" for command in observed)
        if failure in {"ca", "hostname"}:
            assert observed == []  # No AUTH is sent to an unverified TLS peer.
