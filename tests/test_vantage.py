"""The vantage point: parsing, and detecting our own."""

from __future__ import annotations

import pytest

from nas_reach_mcp import vantage as vantage_module
from nas_reach_mcp.vantage import VantageError, detect_self, own_container_name, parse


@pytest.mark.parametrize(
    ("spec", "kind", "target"),
    [
        ("host", "host", None),
        ("nas", "host", None),
        ("self", "host", None),
        ("", "host", None),
        ("network:svc_net", "network", "svc_net"),
        ("net:svc_net", "network", "svc_net"),
        ("container:registry", "container", "registry"),
        ("ctr:registry", "container", "registry"),
        ("svc_net", "network", "svc_net"),
    ],
)
def test_parse(spec, kind, target):
    vantage = parse(spec)
    assert vantage.kind == kind
    assert vantage.target == target


@pytest.mark.parametrize("spec", ["network:", "container:", "nonsense:thing"])
def test_parse_rejects_bad_specs(spec):
    with pytest.raises(VantageError):
        parse(spec)


def test_self_alias_is_recorded_as_self_not_host():
    assert parse("self").spec == "self"
    assert parse("host").spec == "host"


def test_own_container_name_honours_the_override(monkeypatch, inventory):
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "gateway")
    assert own_container_name(inventory) == "gateway"


def test_own_container_name_is_none_when_the_hostname_matches_no_container(monkeypatch, inventory):
    monkeypatch.delenv("NAS_REACH_SELF_CONTAINER", raising=False)
    monkeypatch.setattr(vantage_module.socket, "gethostname", lambda: "not-a-container-id")
    assert own_container_name(inventory) is None


def test_detect_self_where_this_process_is_not_a_visible_container(monkeypatch, inventory):
    monkeypatch.delenv("NAS_REACH_SELF_CONTAINER", raising=False)
    monkeypatch.setattr(vantage_module.socket, "gethostname", lambda: "not-a-container-id")

    vantage, detection = detect_self(inventory)

    assert vantage.kind == "host"
    assert detection["detection"] == "not-a-visible-docker-container"


def test_detect_self_for_a_host_network_deployment(monkeypatch, inventory):
    """The production shape: network_mode: host means self == host."""
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "metrics")
    monkeypatch.setattr(vantage_module.socket, "gethostname", lambda: "whatever")

    vantage, detection = detect_self(inventory)

    assert vantage.kind == "host"
    assert detection["detection"] == "host-network-container"


def test_detect_self_for_a_bridge_deployment(monkeypatch, inventory):
    """If the deployment shape ever changes, 'self' must change with it."""
    monkeypatch.setenv("NAS_REACH_SELF_CONTAINER", "shelf")
    monkeypatch.setattr(vantage_module.socket, "gethostname", lambda: "whatever")

    vantage, detection = detect_self(inventory)

    # A bridge container has several vantages and 'self' is genuinely ambiguous
    # there, so the choice is arbitrary but deterministic: the first of the
    # sorted attachments. The detection payload carries the rest, and a caller
    # that cares must pass an explicit vantage.
    assert vantage.kind == "network"
    assert vantage.target == "shelf_net"
    assert detection["networks"] == ["shelf_net", "svc_net"]
    assert detection["detection"] == "bridge-container"
