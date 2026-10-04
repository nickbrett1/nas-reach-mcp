"""The resolver: the same service, a different address from every vantage."""

from __future__ import annotations

from nas_reach_mcp import graph
from nas_reach_mcp.inventory import build
from nas_reach_mcp.vantage import parse


def resolve(inventory, name, spec):
    return graph.resolve(inventory, name, parse(spec))


def test_same_service_two_vantages_two_addresses(inventory):
    """The whole reason this tool exists, in one test."""
    from_network = resolve(inventory, "gateway", "network:svc_net")
    from_host = resolve(inventory, "gateway", "host")

    assert (from_network.status, from_network.url) == ("resolved", "http://gateway:4000")
    assert (from_host.status, from_host.url) == ("resolved", "http://nas:4000")

    # Both are correct. Neither is *the* address.
    assert from_network.url != from_host.url


def test_host_port_and_container_port_are_different_ports(inventory):
    """shelf publishes 3005 -> 3000: the port changes with the vantage too."""
    assert resolve(inventory, "shelf", "host").url == "http://nas:3005"
    assert resolve(inventory, "shelf", "network:svc_net").url == "http://shelf:3000"
    assert resolve(inventory, "shelf", "network:shelf_net").url == "http://shelf:3000"


def test_host_network_container_is_host_only(inventory):
    for spec in ("network:svc_net", "container:registry", "host"):
        result = resolve(inventory, "metrics", spec)
        assert result.status == graph.HOST_ONLY
        assert result.url is None
        assert "no name on any Docker network" in result.reason or "host port" in result.reason


def test_not_attached_names_where_the_service_is_reachable(inventory):
    result = resolve(inventory, "toolbox-api", "network:svc_net")

    assert result.status == graph.NOT_ATTACHED
    assert result.url is None
    assert "registry_net" in result.reason, "a negative answer must say where it *is*"
    assert "will not resolve" in result.reason
    # And the honest nuance: not-resolvable is not the same as not-routable.
    assert "may still route" in result.reason


def test_a_bridge_does_not_make_its_dependents_reachable(inventory):
    """toolbox-api is on registry_net only: being behind a bridge is not enough."""
    via_registry_from_svc = resolve(inventory, "toolbox-api", "network:svc_net")
    assert via_registry_from_svc.status == graph.NOT_ATTACHED

    # But standing *behind* the bridge, it resolves.
    through_registry = resolve(inventory, "toolbox-api", "container:registry")
    assert through_registry.status == graph.RESOLVED
    assert through_registry.url == "http://toolbox-api:8797"


def test_container_vantage_reports_missing_shared_network(inventory):
    result = resolve(inventory, "gateway", "container:toolbox-api")

    assert result.status == graph.NOT_ATTACHED
    assert "shares no network" in result.reason
    assert "registry_net" in result.reason


def test_attached_but_no_visible_port(inventory):
    result = resolve(inventory, "db", "network:svc_net")

    assert result.status == graph.UNKNOWN_PORT
    assert result.url is None
    assert "db" in result.reason


def test_unknown_port_from_the_host_too(inventory):
    result = resolve(inventory, "db", "host")

    assert result.status == graph.UNKNOWN_PORT
    assert "publishes no host port" in result.reason


def test_unresolvable_name_is_not_the_same_as_not_attached(inventory):
    result = resolve(inventory, "does-not-exist", "network:svc_net")

    assert result.status == graph.UNRESOLVABLE_NAME
    assert result.container is None
    # The distinction matters: DSM packages and host processes are invisible here.
    assert "DSM packages" in result.reason


def test_unknown_vantage_is_reported_before_anything_else(inventory):
    network = resolve(inventory, "gateway", "network:nope")
    container = resolve(inventory, "gateway", "container:nope")

    assert network.status == graph.UNKNOWN_NETWORK
    assert container.status == graph.UNKNOWN_CONTAINER


def test_ambiguous_name_is_refused_rather_than_guessed():
    containers = [
        {
            "Id": "cc01",
            "Names": ["/twin"],
            "HostConfig": {"NetworkMode": "net_a"},
            "NetworkSettings": {"Networks": {"net_a": {"IPAddress": "10.0.0.2"}}},
            "Ports": [{"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
        },
        {
            "Id": "cc02",
            "Names": ["/twin"],
            "HostConfig": {"NetworkMode": "net_b"},
            "NetworkSettings": {"Networks": {"net_b": {"IPAddress": "10.0.1.2"}}},
            "Ports": [{"PrivatePort": 80, "PublicPort": 8081, "Type": "tcp"}],
        },
    ]
    inv = build(containers, {"net_a": {"Name": "net_a"}, "net_b": {"Name": "net_b"}})

    result = graph.resolve(inv, "twin", parse("host"))

    assert result.status == graph.AMBIGUOUS
    assert "confidently wrong" in result.reason


def test_every_resolution_is_stamped(inventory):
    assert resolve(inventory, "gateway", "host").verified_at == inventory.verified_at
    assert resolve(inventory, "nope", "host").verified_at == inventory.verified_at


def test_alternatives_cover_host_and_every_network(inventory):
    result = resolve(inventory, "shelf", "host")
    vantages = {address.vantage for address in result.addresses}

    assert vantages == {"host", "network:svc_net", "network:shelf_net"}
    urls = {a.url for a in result.addresses}
    assert urls == {"http://nas:3005", "http://shelf:3000"}


def test_loopback_published_port_is_not_given_the_host_name(inventory):
    """A port published to 127.0.0.1 is an address on the host and nowhere else."""
    result = resolve(inventory, "toolbox-api", "host")

    assert result.status == graph.RESOLVED
    assert result.url == "http://127.0.0.1:8797", "not http://nas:8797, which would not answer"
    assert "nowhere else" in result.reason


def test_a_reachable_container_is_reachable_even_from_a_network_it_is_not_on(inventory):
    """toolbox-api is on registry_net only, but it still publishes locally."""
    result = resolve(inventory, "toolbox-api", "network:svc_net")
    assert result.status == graph.NOT_ATTACHED
    # The distinction the two statuses carry: no name, but an address exists.
    host_view = {a.vantage: a.url for a in result.addresses}["host"]
    assert host_view == "http://127.0.0.1:8797"


def test_reachable_from_host_and_from_a_network(inventory):
    from_host = graph.reachable_from(inventory, parse("host"))
    assert {s["name"] for s in from_host} == {
        "gateway",
        "registry",
        "shelf",
        "edge",
        "metrics",
        "toolbox-api",
    }

    from_svc = graph.reachable_from(inventory, parse("network:svc_net"))
    assert {s["name"] for s in from_svc} == {"gateway", "registry", "shelf", "db"}


def test_reachable_through_a_container_excludes_itself(inventory):
    through = graph.reachable_from(inventory, parse("container:registry"))

    assert {s["name"] for s in through} == {"gateway", "shelf", "db", "toolbox-api"}
    assert "registry" not in {s["name"] for s in through}
