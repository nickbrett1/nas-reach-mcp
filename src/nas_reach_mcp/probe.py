"""The live probe -- and the disambiguation of HTTP ``000``.

``curl`` reports one status code for three different diseases, and the
difference is exactly the diagnosis that a recon session lacked. Measured on
this host:

    nas:4000        -> 000, time_namelookup = 0.000,  ~12 ms   name never resolved
    litellm:9999    -> 000, time_namelookup > 0, connect 0, ~3 ms  connection refused
    (not observed)  -> 000, time_connect > 0, total ~ timeout   filtered / timeout

So the probe does not ask an HTTP client to interpret a failure. It classifies
the failure itself, in phases, and then -- only if the TCP connection actually
succeeded -- does it speak HTTP. ``unresolvable_name`` / ``refused`` / ``timeout``
are therefore first-class results, not implementation details.

The graph hands out candidate addresses; only this module confirms them. Its
answers are stamped because liveness is a moment, not a property.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

OK = "ok"
UNRESOLVABLE_NAME = "unresolvable_name"
REFUSED = "refused"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
ERROR = "error"

DEFAULT_TIMEOUT = 3.0

# A bounded identity surface (design memo section 4.2). Identity is part of
# liveness because a 200 from the wrong service is worse than a 404 -- but
# "identity" must not become a protocol zoo, so only these are attempted.
MCP_PATHS = ("/mcp", "/sse", "/messages")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Probe:
    outcome: str
    target: str
    url: str | None
    http_status: int | None
    server_header: str | None
    identity: dict[str, Any] | None
    resolved_ips: tuple[str, ...]
    latency_ms: float | None
    detail: str
    verified_at: str

    @property
    def is_up(self) -> bool:
        return self.outcome == OK and self.http_status is not None and self.http_status < 500

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "target": self.target,
            "url": self.url,
            "http_status": self.http_status,
            "server": self.server_header,
            "identity": self.identity,
            "resolved_ips": list(self.resolved_ips),
            "latency_ms": self.latency_ms,
            "is_up": self.is_up,
            "detail": self.detail,
            "verified_at": self.verified_at,
        }


def split_target(target: str) -> tuple[str, str, int, str]:
    """Parse ``name``, ``name:port``, ``host/path`` or a full URL.

    Returns ``(scheme, host, port, path)``. A bare name with no scheme is read
    as HTTP, because that is what these services speak.
    """
    raw = target.strip()
    if "://" not in raw:
        raw = f"http://{raw}"

    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    return scheme, host, port, path


def tcp_classify(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, tuple[str, ...], str]:
    """Classify the connection attempt in phases, before any HTTP is spoken.

    Returns ``(outcome, resolved_ips, detail)``.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return (
            UNRESOLVABLE_NAME,
            (),
            (
                f"name resolution failed for {host!r} ({exc.strerror or exc}); the "
                "name does not resolve from this vantage"
            ),
        )
    except OSError as exc:  # pragma: no cover - defensive
        return ERROR, (), f"name resolution errored for {host!r}: {exc}"

    ips = tuple(sorted({info[4][0] for info in infos}))
    if not ips:
        return UNRESOLVABLE_NAME, (), f"{host!r} resolved to no addresses"

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        return (
            REFUSED,
            ips,
            f"resolved to {list(ips)} but port {port} actively refused the connection",
        )
    except TimeoutError:
        return (
            TIMEOUT,
            ips,
            f"resolved to {list(ips)} but connecting to port {port} timed out after {timeout}s",
        )
    except OSError as exc:
        return (
            UNREACHABLE,
            ips,
            f"resolved to {list(ips)} but connecting to port {port} failed: {exc}",
        )

    return OK, ips, f"TCP connect to {list(ips)}:{port} succeeded"


def mcp_server_info(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """Ask an MCP endpoint who it is, via a single ``initialize`` handshake.

    This is the ``serverInfo.name`` / version the recon session needed, and the
    reason identity belongs to liveness: an HTTP 200 alone cannot distinguish
    the service you asked for from the one that happens to be on that port.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "nas-reach-mcp", "version": "0.1.0"},
        },
    }
    try:
        response = httpx.post(
            url,
            json=payload,
            timeout=timeout,
            headers={"Accept": "application/json, text/event-stream"},
        )
        if response.status_code >= 400:
            return None
        data = response.json()
    except Exception:  # noqa: BLE001 - identity is best-effort, never fatal
        return None

    result = (data or {}).get("result") or {}
    server_info = result.get("serverInfo")
    if not isinstance(server_info, dict):
        return None
    return {
        "protocol": "mcp",
        "name": server_info.get("name"),
        "version": server_info.get("version"),
        "protocol_version": result.get("protocolVersion"),
    }


def probe(target: str, timeout: float = DEFAULT_TIMEOUT, identity: bool = True) -> Probe:
    """Probe ``target`` from this process's own vantage, classifying failures."""
    scheme, host, port, path = split_target(target)
    url = f"{scheme}://{host}:{port}{path}"

    outcome, ips, detail = tcp_classify(host, port, timeout)
    if outcome != OK:
        return Probe(
            outcome=outcome,
            target=target,
            url=url,
            http_status=None,
            server_header=None,
            identity=None,
            resolved_ips=ips,
            latency_ms=None,
            detail=detail,
            verified_at=_utcnow(),
        )

    started = time.perf_counter()
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        return Probe(
            outcome=ERROR,
            target=target,
            url=url,
            http_status=None,
            server_header=None,
            identity=None,
            resolved_ips=ips,
            latency_ms=None,
            detail=f"TCP connected but the HTTP request failed: {exc}",
            verified_at=_utcnow(),
        )

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    server_header = response.headers.get("server")

    identity_info: dict[str, Any] | None = None
    if identity and any(p in path for p in MCP_PATHS):
        identity_info = mcp_server_info(url, timeout)

    return Probe(
        outcome=OK,
        target=target,
        url=url,
        http_status=response.status_code,
        server_header=server_header,
        identity=identity_info,
        resolved_ips=ips,
        latency_ms=latency_ms,
        detail=f"HTTP {response.status_code} after {latency_ms} ms",
        verified_at=_utcnow(),
    )
