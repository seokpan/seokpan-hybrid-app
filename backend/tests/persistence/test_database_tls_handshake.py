"""Real TLS via MemoryBIO, with ephemeral test keys; never opens a network socket."""

import shutil
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from seokpan.persistence.mariadb.connection import DATABASE_HOST, database_ssl_context


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Path:
    openssl = shutil.which("openssl")
    if openssl is None:
        bundled = Path("C:/Program Files/Git/usr/bin/openssl.exe")
        openssl = str(bundled) if bundled.is_file() else None
    if openssl is None:
        pytest.skip("TLS handshake evidence requires OpenSSL CLI; not a completed TLS verification")
    directory = tmp_path_factory.mktemp("app50-tls")

    def run(*args: str) -> None:
        result = subprocess.run(
            [openssl, *args], cwd=directory, capture_output=True, timeout=30, check=False
        )
        assert result.returncode == 0, "ephemeral TLS certificate generation failed"

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
        "-days",
        "2",
        "-subj",
        "/CN=Seokpan Synthetic Test CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    run(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "server.key",
        "-out",
        "server.csr",
        "-subj",
        f"/CN={DATABASE_HOST}",
    )
    (directory / "index").write_text("", encoding="ascii")
    (directory / "serial").write_text("01\n", encoding="ascii")
    (directory / "ca.cnf").write_text(
        "[ca]\ndefault_ca=local\n[local]\ndatabase=index\nserial=serial\n"
        "new_certs_dir=.\ncertificate=ca.crt\nprivate_key=ca.key\n"
        "default_md=sha256\npolicy=policy\nunique_subject=no\nx509_extensions=server\n"
        "[policy]\ncommonName=supplied\n[server]\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
        f"subjectAltName=DNS:{DATABASE_HOST}\n",
        encoding="ascii",
    )
    now = datetime.now(UTC)
    for name, start, end in [("valid", -1, 24), ("expired", -24, -1), ("future", 1, 24)]:
        run(
            "ca",
            "-batch",
            "-config",
            "ca.cnf",
            "-in",
            "server.csr",
            "-out",
            f"{name}.crt",
            "-startdate",
            (now + timedelta(hours=start)).strftime("%Y%m%d%H%M%SZ"),
            "-enddate",
            (now + timedelta(hours=end)).strftime("%Y%m%d%H%M%SZ"),
        )
    return directory


def handshake(client_context: ssl.SSLContext, directory: Path, leaf: str, hostname: str) -> str:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(directory / f"{leaf}.crt", directory / "server.key")
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        for peer, done in [(client, client_done), (server, server_done)]:
            if not done:
                try:
                    peer.do_handshake()
                    if peer is client:
                        client_done = True
                    else:
                        server_done = True
                except ssl.SSLWantReadError:
                    pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if client_done and server_done:
            assert client.version() is not None
            return str(client.version())
    pytest.fail("in-memory TLS handshake did not complete")


def test_trusted_tls_handshake(certificates: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    context = database_ssl_context(str(certificates / "ca.crt"))
    assert handshake(context, certificates, "valid", DATABASE_HOST) in {"TLSv1.2", "TLSv1.3"}


@pytest.mark.parametrize(
    ("leaf", "hostname"),
    [
        ("valid", "wrong.example"),
        ("expired", DATABASE_HOST),
        ("future", DATABASE_HOST),
    ],
)
def test_invalid_peer_certificate_rejected(
    certificates: Path, monkeypatch: pytest.MonkeyPatch, leaf: str, hostname: str
) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    context = database_ssl_context(str(certificates / "ca.crt"))
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, certificates, leaf, hostname)


def test_untrusted_ca_rejected(certificates: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    context = database_ssl_context(str(Path(__file__).parent / "fixtures/public-ca.crt"))
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, certificates, "valid", DATABASE_HOST)


def test_plaintext_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    context = database_ssl_context(str(Path(__file__).parent / "fixtures/public-ca.crt"))
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname=DATABASE_HOST)
    incoming.write(b"not a TLS server response")
    with pytest.raises(ssl.SSLError):
        client.do_handshake()
