"""A stand-in for a generated app's container, for the Docker-backed router tests.

It answers two ways and that is the whole point:

* an ordinary request is echoed back as a parseable line, so a test can assert the REQUEST
  LINE the router composed rather than merely which host it dialled. Asserting the host alone
  is how the keyless arm's missing prefix survived the first draft of this design — the
  request reached the right container and the framework answered 404.
* a WebSocket handshake is COMPLETED, so live reload can be proven with a real 101 rather than
  inferred from the `Upgrade` header having been forwarded.

The TLS certificate is minted in-process at start. The upstream is dialled over https because
the real one is Azure Container Apps ingress, and `proxy_ssl_server_name` / SNI is part of what
these tests exist to prove — a plaintext stub would skip it. Nothing here trusts the cert
(nginx's `proxy_ssl_verify` is off by default, exactly as it is against the backend), so a
throwaway self-signed one is the honest shape. Minting it here rather than shipping a fixture
keeps the harness free of a checked-in key and of an expiry date that goes stale.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import socket
import ssl
import tempfile
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# RFC 6455's fixed handshake GUID. Not a secret, not configurable.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEAD = 65536


def _mint_cert() -> tuple[str, str]:
    """A throwaway self-signed cert covering any name the router might dial."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bial-router-test-stub")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("*.bial-apps.test"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    tmp = Path(tempfile.mkdtemp())
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def _read_head(conn: ssl.SSLSocket) -> bytes | None:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > _MAX_HEAD:
            return None
    return buf


def _parse(buf: bytes) -> tuple[str, dict[str, str]]:
    lines = buf.split(b"\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
    return lines[0].decode("latin-1"), headers


def _handle(conn: ssl.SSLSocket) -> None:
    try:
        buf = _read_head(conn)
        if buf is None:
            return
        request_line, headers = _parse(buf)
        parts = request_line.split(" ")
        method, target = (parts + ["", ""])[:2]
        if headers.get("upgrade", "").lower() == "websocket":
            accept = base64.b64encode(
                hashlib.sha1(  # noqa: S324 - RFC 6455 mandates SHA-1 here
                    (headers.get("sec-websocket-key", "") + _WS_GUID).encode()
                ).digest()
            ).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    f"X-Stub-Target: {target}\r\n"
                    "\r\n"
                ).encode()
            )
            conn.recv(4096)
            return
        body = (
            f"REQ={method}|{target}|HOST={headers.get('host', '')}"
            f"|UP={headers.get('upgrade', '')}|CONN={headers.get('connection', '')}"
            f"|REF={headers.get('referer', '')}|CK={headers.get('cookie', '')}"
            f"|XFH={headers.get('x-forwarded-host', '')}\n"
        ).encode()
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
    except OSError:
        # A client that hung up mid-exchange is the test harness tearing down, not a defect.
        pass
    finally:
        conn.close()


def main() -> None:
    cert_path, key_path = _mint_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 443))  # noqa: S104 - a throwaway container on a private test network
    srv.listen(64)
    while True:
        raw, _ = srv.accept()
        try:
            wrapped = ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            continue
        threading.Thread(target=_handle, args=(wrapped,), daemon=True).start()


if __name__ == "__main__":
    main()
