"""The server: tool registration, and the reporting contract of check_reachable.

The registration tests need no Docker and no network. The check_reachable tests
inject a fixture inventory and fake probe results, so they exercise the
reporting -- which probe counts as evidence for which vantage -- without a host.
"""

from __future__ import annotations

import asyncio

import pytest

import nas_reach_mcp.server as server_module
from nas_reach_mcp.probe import Probe
from nas_reach_mcp.server import mcp
from nas_reach_mcp.vantage import detect_self

STAMP = "2026-10-04T00:00:00+00:00"


def _probe_result(target: str, outcome: str, status: int | None = None, server: str | None = None) -> Probe:
    """A canned Probe for reporting tests; the outcome is what matters here."""
    return Probe(
        outcome=outcome,
        target=target,
        url=target,
        http_status=status,
        server_header=server,
        identity=None,
        resolved_ips=() if outcome == "unresolvable_name" else ("10.0.0.1",),
        latency_ms=1.0 if outcome == "ok" else None,
        detail=f"{outcome} for {target}",
        verified_at=STAMP,
    )


def _install_probes(monkeypatch, mapping):
    """Answer probes from a fixed target -> Probe map, failing on the unexpected."""

    def fake_probe(target, identity=True):
        if target not in mapping:
            raise AssertionError(f"unexpected probe target: {target!r}")
        return mapping[target]

    monkeypatch.setattr(server_module, "probe", fake_probe)


@pytest.fixture
def wired(monkeypatch, inventory):
    """check_reachable against the synthetic fixture inventory, not a socket."""
    monkeypatch.setattr(server_module, "_inventory", lambda: inventory)
    monkeypatch.delenv("NAS_REACH_SELF_CONTAINER", raising=False)
    return inventory


def _input_schema(tool):
    """mcp 2.x renamed the field ``inputSchema`` -> ``input_schema``.

    Reading it through a helper keeps this suite green on both majors, which is
    the same reason the server imports its implementation with a fallback.
    """
    return getattr(tool, "input_schema", None) or tool.inputSchema


EXPECTED = {
    "whoami",
    "list_networks",
    "list_services",
    "resolve_service",
    "check_reachable",
    "describe_service",
}


def test_expected_tools_are_registered():
    tools = asyncio.run(mcp.list_tools())

    assert {tool.name for tool in tools} == EXPECTED


def test_from_is_required_where_the_design_says_so():
    """`from` is never defaulted on the two tools that need it.

    A defaulted vantage is how you get a confidently wrong address, so this is
    asserted rather than trusted to review.
    """
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    for name in ("resolve_service", "check_reachable"):
        schema = _input_schema(tools[name])
        assert "from_vantage" in schema["required"], f"{name} must require from_vantage"

    # list_services is the one exception: "everything from where I am" is a
    # complete question on its own.
    assert "from_vantage" not in _input_schema(tools["list_services"]).get("required", [])


def test_list_networks_takes_no_vantage():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert _input_schema(tools["list_networks"]).get("properties", {}) == {}


# --- defect 1: the flag is derived from the evidence, not from vantage.kind ---


def test_network_vantage_with_a_successful_by_ip_probe_is_confirmed(wired, monkeypatch):
    """A network vantage whose name will not resolve here but whose bridge IP answers.

    gateway is on svc_net. The host-network probe cannot resolve the name, but
    the bridge IP returns 200 -- proof the service answers from the requested
    vantage. Reporting ``confirms_requested_vantage: false`` next to that
    evidence is the defect.
    """
    _install_probes(
        monkeypatch,
        {
            "http://gateway:4000": _probe_result("http://gateway:4000", "unresolvable_name"),
            "http://172.31.0.21:4000": _probe_result("http://172.31.0.21:4000", "ok", 200),
        },
    )

    result = server_module.check_reachable("gateway", "network:svc_net")

    assert result["probe"]["outcome"] == "unresolvable_name"
    assert result["by_ip_probe"]["outcome"] == "ok"
    assert result["confirms_requested_vantage"] is True
    assert result["confirmed_by"] == "by-ip"
    assert result["confirms_note"] is None


def test_by_name_probe_from_the_matching_network_vantage_is_confirmed(wired, monkeypatch):
    """When the probe really is on the requested network, by-name is the basis."""
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "shelf")  # bridge -> network:shelf_net

    _install_probes(
        monkeypatch,
        {
            "http://shelf:3000": _probe_result("http://shelf:3000", "ok", 200),
            "http://192.168.250.2:3000": _probe_result("http://192.168.250.2:3000", "ok", 200),
        },
    )

    result = server_module.check_reachable("shelf", "self")

    assert result["from"] == "network:shelf_net"
    assert result["confirms_requested_vantage"] is True
    assert result["confirmed_by"] == "by-name"


def test_host_vantage_confirmed_only_by_bridge_ip_reports_false_with_a_reason(wired, monkeypatch):
    """The one case that stays false despite an ``ok`` probe keeps a ``why``.

    From the host, a bridge IP that answers is host routing, not the published
    host address the host vantage is defined by -- and the request asked for an
    explicit reason rather than a bare ``false``.
    """
    _install_probes(
        monkeypatch,
        {
            "http://nas:4000": _probe_result("http://nas:4000", "refused"),
            "http://172.31.0.21:4000": _probe_result("http://172.31.0.21:4000", "ok", 200),
        },
    )

    result = server_module.check_reachable("gateway", "host")

    assert result["confirms_requested_vantage"] is False
    assert result["confirmed_by"] is None
    assert result["confirms_note"] is not None
    assert "bridge IP" in result["confirms_note"]


def test_by_name_ok_from_a_different_namespace_is_not_taken_as_confirmation(wired, monkeypatch):
    """An ok by-name probe from the wrong namespace is explained, not silently false."""
    _install_probes(
        monkeypatch,
        {
            "http://registry:3000": _probe_result("http://registry:3000", "ok", 200),
            "http://192.168.251.2:3000": _probe_result("http://192.168.251.2:3000", "refused"),
        },
    )

    result = server_module.check_reachable("registry", "network:registry_net")

    assert result["probe"]["outcome"] == "ok"
    assert result["confirms_requested_vantage"] is False
    assert result["confirmed_by"] is None
    assert result["confirms_note"] is not None
    assert "namespace" in result["confirms_note"]


# --- defect 2: probe_vantage is read from runtime state, not hardcoded ---


def test_probe_vantage_label_reports_the_detected_host_network_container(monkeypatch, inventory):
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "metrics")
    vantage, detection = detect_self(inventory)

    label = server_module._probe_vantage_label(vantage, detection)

    assert "metrics" in label
    assert "network_mode: host" in label
    assert vantage.label in label


def test_probe_vantage_label_follows_a_bridge_deployment(monkeypatch, inventory):
    """The string is a fact: change the deployment shape and it changes too."""
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "shelf")
    vantage, detection = detect_self(inventory)

    label = server_module._probe_vantage_label(vantage, detection)

    assert "shelf" in label
    assert "network:shelf_net" in label
    assert "network_mode: host" not in label


def test_check_reachable_reports_the_detected_probe_vantage(wired, monkeypatch):
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "metrics")
    _install_probes(
        monkeypatch,
        {
            "http://nas:4000": _probe_result("http://nas:4000", "ok", 200),
            "http://172.31.0.21:4000": _probe_result("http://172.31.0.21:4000", "ok", 200),
        },
    )

    result = server_module.check_reachable("gateway", "host")

    assert "metrics" in result["probe_vantage"]
    assert "network_mode: host" in result["probe_vantage"]


# --- defect 3: a URL whose host is a known container gets the graph + by-IP ---


def test_url_target_directs_the_graph_lookup_at_its_host(wired, monkeypatch):
    """The reproduced case: a URL host that is a container skips the fallback.

    http://gateway:4000/health/liveliness, from network:svc_net, must get the
    same lookup and by-IP fallback a bare name gets -- keyed on the URL's host
    and preserving the URL's port and path in the fallback.
    """
    target = "http://gateway:4000/health/liveliness"
    by_ip = "http://172.31.0.21:4000/health/liveliness"
    _install_probes(
        monkeypatch,
        {
            target: _probe_result(target, "unresolvable_name"),
            by_ip: _probe_result(by_ip, "ok", 200),
        },
    )

    result = server_module.check_reachable(target, "network:svc_net")

    assert result["candidate_url"] == target  # probed as given
    assert result["resolution"] is not None
    assert result["resolution"]["service"] == "gateway"
    assert result["by_ip_probe"] is not None
    assert result["by_ip_probe"]["outcome"] == "ok"
    assert result["confirms_requested_vantage"] is True
    assert result["confirmed_by"] == "by-ip"
    assert any("host 'gateway'" in note for note in result["notes"])


def test_url_target_with_an_unknown_host_skips_the_graph_lookup(wired, monkeypatch):
    target = "http://not-a-container.invalid:4000/health"
    _install_probes(monkeypatch, {target: _probe_result(target, "unresolvable_name")})

    result = server_module.check_reachable(target, "network:svc_net")

    assert result["resolution"] is None
    assert result["by_ip_probe"] is None
    assert any("no graph lookup was done" in note for note in result["notes"])
