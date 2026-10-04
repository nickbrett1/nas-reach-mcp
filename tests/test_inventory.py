"""The inventory, and the one API trap that would silently break the graph."""

from __future__ import annotations

from typing import Any

from nas_reach_mcp.inventory import build, build_from_summary


def test_membership_comes_from_the_detail_endpoint(inventory):
    assert len(inventory.networks["svc_net"].containers) == 4
    assert set(inventory.networks["registry_net"].containers) == {"registry", "toolbox-api"}
    assert inventory.networks["shelf_net"].containers == ("shelf",)


def test_the_summary_endpoint_has_no_membership_at_all(
    containers_json: list[dict[str, Any]], network_summary: list[dict[str, Any]]
):
    """GET /networks returns "Containers": {} for every network.

    Building from the summary yields a graph with no edges -- silently, without
    an error. Every reachability answer would then be wrong in the direction of
    "unreachable", which is how a first implementation gets this wrong.
    """
    trap = build_from_summary(containers_json, network_summary)

    assert trap.networks, "the summary still lists the networks themselves"

    # The trap itself: the network-side member list is unusable -- empty for
    # every network, with no error to tell you so.
    assert all(not net.containers for net in trap.networks.values())

    # The limit of the trap, worth stating precisely: container-side attachments
    # come from NetworkSettings and survive, so the graph is not wholly broken.
    assert len(trap.containers_on("svc_net")) == 4

    # So the two views disagree, which is exactly the silent failure.
    healthy = build(containers_json, {"svc_net": {"Name": "svc_net", "Containers": {}}}, verified_at="x")
    assert len(trap.networks["svc_net"].containers) == 0
    assert len(healthy.containers_on("svc_net")) == 4


def test_host_network_containers_have_no_network(inventory):
    assert [c.name for c in inventory.host_only] == ["metrics"]
    metrics = inventory.containers["metrics"]
    assert metrics.is_host_network
    assert metrics.networks == ()
    assert metrics.published == ()


def test_bridges_are_containers_on_more_than_one_network(inventory):
    assert {c.name for c in inventory.bridges} == {"registry", "shelf"}


def test_a_loopback_binding_is_recognised(inventory):
    loopback = inventory.containers["toolbox-api"].primary_binding
    assert loopback is not None
    assert loopback.host_ip == "127.0.0.1"
    assert inventory.containers["toolbox-api"].published[0].host_port == 8797

    # A published port that appears twice (IPv4 + IPv6) is one binding.
    duplicate = build(
        [
            {
                "Id": "dd01",
                "Names": ["/dual"],
                "HostConfig": {"NetworkMode": "svc_net"},
                "NetworkSettings": {"Networks": {"svc_net": {"IPAddress": "10.0.0.9"}}},
                "Ports": [
                    {"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
                    {"IP": "::", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
                ],
            }
        ],
        {"svc_net": {"Name": "svc_net"}},
    )
    assert len(duplicate.containers["dual"].published) == 2
    assert duplicate.containers["dual"].primary_binding.host_port == 8080


def test_ports_are_read_as_bindings(inventory):
    shelf = inventory.containers["shelf"]
    assert shelf.published[0].host_port == 3005
    assert shelf.published[0].container_port == 3000
    assert shelf.container_ports == (3000,)
    assert shelf.project == "shelf"

    # No published binding means no visible container-side port at all.
    assert inventory.containers["db"].published == ()
    assert inventory.containers["db"].container_ports == ()


def test_ip_lookup_is_per_network(inventory):
    shelf = inventory.containers["shelf"]
    assert shelf.ip_on("svc_net") == "172.31.0.30"
    assert shelf.ip_on("shelf_net") == "192.168.250.2"
    assert shelf.ip_on("nope") is None


def test_duplicate_names_are_recorded_not_silently_overwritten():
    containers = [
        {
            "Id": "cc01",
            "Names": ["/twin"],
            "HostConfig": {"NetworkMode": "net_a"},
            "NetworkSettings": {"Networks": {"net_a": {"IPAddress": "10.0.0.2"}}},
            "Ports": [],
        },
        {
            "Id": "cc02",
            "Names": ["/twin"],
            "HostConfig": {"NetworkMode": "net_b"},
            "NetworkSettings": {"Networks": {"net_b": {"IPAddress": "10.0.1.2"}}},
            "Ports": [],
        },
    ]
    inv = build(containers, {"net_a": {"Name": "net_a"}, "net_b": {"Name": "net_b"}})

    assert "twin" in inv.duplicates
    assert len(inv.duplicates["twin"]) == 2


def test_verified_at_is_stamped(inventory):
    assert inventory.verified_at == "2026-10-04T00:00:00+00:00"
