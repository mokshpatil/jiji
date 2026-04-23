#!/usr/bin/env python3
"""Serve the jiji web client over HTTPS on the LAN.

Why HTTPS: WebCrypto (`window.crypto.subtle`) is only exposed in "secure
contexts" — HTTPS, or http://localhost. A plain `python3 -m http.server`
works from the same machine but fails the moment a second device hits the
LAN IP, because non-loopback HTTP is insecure in browsers.

Cert strategy:
  1. If `mkcert` is on PATH, use it — issues locally-trusted certs (no browser
     warning). One-time setup: `brew install mkcert && mkcert -install`.
  2. Else fall back to openssl self-signed. Browser will warn on first visit
     (click through "Advanced → Proceed"); trust is local to each device.

Certs land in ./.certs/ and are reused across runs.
"""

from __future__ import annotations

import argparse
import http.server
import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CERT_DIR = HERE / ".certs"
CERT_FILE = CERT_DIR / "dev.pem"
KEY_FILE = CERT_DIR / "dev-key.pem"


def lan_ip() -> str:
    """Best-effort local LAN IP (works without touching the internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(extra_hosts: list[str]) -> None:
    """Create dev cert + key if missing. SANs: localhost, 127.0.0.1, LAN IP,
    plus any user-supplied hosts."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return

    CERT_DIR.mkdir(exist_ok=True)
    # SANs: loopback names + detected LAN IP + anything the user added.
    # de-dupe while preserving order
    seen: set[str] = set()
    hosts: list[str] = []
    for h in ["localhost", "127.0.0.1", "::1", lan_ip(), *extra_hosts]:
        if h not in seen:
            seen.add(h)
            hosts.append(h)

    if shutil.which("mkcert"):
        print(f"generating cert with mkcert for: {', '.join(hosts)}")
        subprocess.run(
            ["mkcert", "-cert-file", str(CERT_FILE), "-key-file", str(KEY_FILE), *hosts],
            check=True,
        )
        return

    if shutil.which("openssl"):
        print(f"generating self-signed cert with openssl for: {', '.join(hosts)}")
        print("(browser will warn the first time — click through 'Advanced → Proceed')")
        san_lines = []
        dns_i = ip_i = 1
        for h in hosts:
            try:
                ipaddress.ip_address(h)
                san_lines.append(f"IP.{ip_i} = {h}")
                ip_i += 1
            except ValueError:
                san_lines.append(f"DNS.{dns_i} = {h}")
                dns_i += 1
        cfg = CERT_DIR / "openssl.cnf"
        cfg.write_text(
            "[req]\n"
            "distinguished_name = req_dn\n"
            "req_extensions = v3_req\n"
            "prompt = no\n"
            "[req_dn]\n"
            "CN = jiji-dev\n"
            "[v3_req]\n"
            "subjectAltName = @alt_names\n"
            "[alt_names]\n"
            + "\n".join(san_lines) + "\n"
        )
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", "365", "-config", str(cfg), "-extensions", "v3_req",
        ], check=True)
        return

    sys.exit("error: need either 'mkcert' or 'openssl' on PATH to generate a TLS cert")


def main() -> None:
    p = argparse.ArgumentParser(description="HTTPS static server for the jiji web client.")
    p.add_argument("--port", type=int, default=8443, help="listen port (default: 8443)")
    p.add_argument("--bind", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p.add_argument("--host", action="append", default=[], metavar="NAME",
                   help="extra hostname/IP to include in cert SANs (repeatable)")
    args = p.parse_args()

    ensure_cert(args.host)

    os.chdir(HERE)  # serve files from the frontend directory

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)

    httpd = http.server.ThreadingHTTPServer(
        (args.bind, args.port), http.server.SimpleHTTPRequestHandler
    )
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    ip = lan_ip()
    print(f"\n  jiji client served at:")
    print(f"    https://localhost:{args.port}")
    if ip != "127.0.0.1":
        print(f"    https://{ip}:{args.port}   (other devices on your LAN)")
    print("\n  Ctrl-C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.server_close()


if __name__ == "__main__":
    main()
