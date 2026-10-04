"""The probe: HTTP 000 is three diseases, and this is where they are separated.

These tests do not touch the network. The local HTTP server binds 127.0.0.1:0
and the failure modes are injected.
"""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

from nas_reach_mcp import probe as probe_module
from nas_reach_mcp.probe import probe, split_target, tcp_classify


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Server", "test-server")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # keep pytest output clean
        pass


@pytest.fixture
def http_port():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("gateway:4000", ("http", "gateway", 4000, "/")),
        ("gateway", ("http", "gateway", 80, "/")),
        ("http://gateway:4000/health", ("http", "gateway", 4000, "/health")),
        ("https://gateway", ("https", "gateway", 443, "/")),
    ],
)
def test_split_target(target, expected):
    assert split_target(target) == expected


def test_a_successful_probe_reports_identity_not_just_a_code(http_port):
    result = probe(f"http://127.0.0.1:{http_port}/")

    assert result.outcome == "ok"
    assert result.http_status == 200
    assert "test-server" in result.server_header
    assert result.is_up
    assert result.latency_ms is not None
    assert result.resolved_ips == ("127.0.0.1",)


def test_refused_is_its_own_outcome(http_port):
    """A resolved name with nothing listening is 'refused', never a bare 000."""
    result = probe(f"http://127.0.0.1:{_closed_port()}/")

    assert result.outcome == "refused"
    assert result.http_status is None
    assert not result.is_up
    assert result.resolved_ips == ("127.0.0.1",)
    assert "refused" in result.detail


def test_unresolvable_name_is_its_own_outcome():
    """The nas:4000 case: the name never resolves, and says so."""
    result = probe("http://no-such-host.invalid:4000/")

    assert result.outcome == "unresolvable_name"
    assert result.http_status is None
    assert result.resolved_ips == ()
    assert "does not resolve" in result.detail


def test_the_two_000s_are_told_apart():
    """The measured distinction, asserted directly.

    name-never-resolved vs connection-refused are the same curl status code and
    different diseases. This test is the reason the module exists.
    """
    unresolvable = probe("http://no-such-host.invalid:4000/")
    refused = probe(f"http://127.0.0.1:{_closed_port()}/")

    assert unresolvable.outcome != refused.outcome
    assert unresolvable.resolved_ips == ()
    assert refused.resolved_ips != ()


def test_timeout_is_its_own_outcome(monkeypatch):
    monkeypatch.setattr(
        probe_module.socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    outcome, ips, detail = tcp_classify("127.0.0.1", 4000, timeout=0.01)

    assert outcome == "timeout"
    assert ips == ("127.0.0.1",)
    assert "timed out" in detail


def test_no_route_is_its_own_outcome(monkeypatch):
    monkeypatch.setattr(
        probe_module.socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(101, "Network is unreachable")),
    )

    outcome, ips, detail = tcp_classify("127.0.0.1", 4000, timeout=0.01)

    assert outcome == "unreachable"
    assert ips == ("127.0.0.1",)
    assert "unreachable" in detail


def test_tcp_classify_succeeds_against_a_live_listener(http_port):
    outcome, ips, detail = tcp_classify("127.0.0.1", http_port)

    assert outcome == "ok"
    assert ips == ("127.0.0.1",)
    assert "succeeded" in detail


def test_identity_is_only_attempted_for_mcp_paths(monkeypatch, http_port):
    calls: list[str] = []

    def fake_mcp(url, timeout=3.0):
        calls.append(url)
        return {"protocol": "mcp", "name": "fake"}

    monkeypatch.setattr(probe_module, "mcp_server_info", fake_mcp)

    probe(f"http://127.0.0.1:{http_port}/health")  # not an MCP path
    assert calls == []

    probe(f"http://127.0.0.1:{http_port}/mcp")  # an MCP path
    assert calls == [f"http://127.0.0.1:{http_port}/mcp"]


def test_mcp_identity_survives_a_service_that_is_not_mcp(http_port):
    """Identity is best-effort: a non-MCP 200 must not become an error."""
    assert probe_module.mcp_server_info(f"http://127.0.0.1:{http_port}/") is None
