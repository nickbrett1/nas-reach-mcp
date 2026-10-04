"""Normalise the Docker API into the two things this tool reasons about:
containers (with their network attachments and published ports) and networks
(with their members).

Two measured facts shape this module:

1. Network membership only exists on ``GET /networks/{name}``. Building an
   inventory from the ``/networks`` *summary* yields a graph with no edges at
   all -- silently. ``build`` therefore takes network *details*, and
   ``build_from_summary`` exists only to make that failure mode testable.
2. ``HostConfig.NetworkMode`` is not a network name. For a container on a
   user-defined network it is the bare network id or hash, and for the host
   network it is the literal ``"host"``. Host-network membership is therefore
   detected from that field, not from ``NetworkSettings.Networks`` (which is
   empty for host-network containers).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

HOST_NETWORK = "host"
LOOPBACK_ADDRESS = "127.0.0.1"


def is_loopback(ip: str | None) -> bool:
    """True for a loopback bind address.

    A loopback-published port is reachable from the host and from nowhere else:
    it is not a LAN address and not a Tailscale address. This matters because
    the standing preference for some services is exactly that binding, and
    reporting the NAS hostname for one would send callers to a closed port.
    """
    if not ip:
        return False
    return ip.startswith("127.") or ip in ("::1", "[::1]")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PortBinding:
    """A published port: ``host_port -> container_port``."""

    host_port: int
    container_port: int
    proto: str = "tcp"
    host_ip: str | None = None


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str
    state: str
    status: str
    network_mode: str
    networks: tuple[str, ...]
    ips: Mapping[str, str]
    published: tuple[PortBinding, ...]
    project: str | None = None

    @property
    def is_host_network(self) -> bool:
        """True when the container shares the host's network namespace.

        Such a container has no name on any Docker network and is reachable by
        host port or not at all.
        """
        return self.network_mode == HOST_NETWORK

    @property
    def is_bridge(self) -> bool:
        """True when the container is attached to more than one network."""
        return len(self.networks) > 1

    @property
    def container_ports(self) -> tuple[int, ...]:
        """Ports the container listens on, as far as Docker knows them.

        ``PrivatePort`` of a published binding is the container-side port; it is
        a hint, not a contract, because an unpublished listener is invisible to
        the summary API.
        """
        seen = {b.container_port for b in self.published}
        return tuple(sorted(seen))

    @property
    def primary_binding(self) -> PortBinding | None:
        """The binding to advertise, de-duplicated and preferring non-loopback.

        One published port appears twice in the API (once for IPv4, once for
        IPv6). More importantly, a port published to ``127.0.0.1`` is an address
        on the host and *nowhere else* -- answering with the NAS hostname for
        such a port would be a confidently wrong address, so the loopback
        binding is preferred last and reported as loopback.
        """
        if not self.published:
            return None

        deduped: dict[tuple[int, int], PortBinding] = {}
        for binding in self.published:
            deduped.setdefault((binding.host_port, binding.container_port), binding)
        bindings = list(deduped.values())

        for binding in bindings:
            if not is_loopback(binding.host_ip):
                return binding
        return bindings[0]

    def ip_on(self, network: str) -> str | None:
        return self.ips.get(network)


@dataclass(frozen=True)
class Network:
    name: str
    id: str
    driver: str
    internal: bool
    containers: tuple[str, ...]


@dataclass(frozen=True)
class Inventory:
    containers: Mapping[str, Container]
    networks: Mapping[str, Network]
    verified_at: str
    duplicates: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def host_only(self) -> tuple[Container, ...]:
        """Containers on no named network: reachable by host port or not at all."""
        return tuple(c for c in self.containers.values() if c.is_host_network)

    @property
    def bridges(self) -> tuple[Container, ...]:
        return tuple(c for c in self.containers.values() if c.is_bridge)

    def containers_on(self, network: str) -> tuple[Container, ...]:
        return tuple(c for c in self.containers.values() if network in c.networks)


def _container_from_json(raw: Mapping[str, Any]) -> Container:
    names = raw.get("Names") or []
    name = names[0].lstrip("/") if names else (raw.get("Id") or "?")[:12]

    net_settings = raw.get("NetworkSettings") or {}
    networks_map = net_settings.get("Networks") or {}
    networks = tuple(sorted(networks_map))
    ips = {n: (v or {}).get("IPAddress") or "" for n, v in networks_map.items()}

    published: list[PortBinding] = []
    for port in raw.get("Ports") or []:
        host_port = port.get("PublicPort")
        if not host_port:
            continue
        published.append(
            PortBinding(
                host_port=int(host_port),
                container_port=int(port.get("PrivatePort") or 0),
                proto=port.get("Type") or "tcp",
                host_ip=port.get("IP"),
            )
        )

    labels = raw.get("Labels") or {}
    host_config = raw.get("HostConfig") or {}

    return Container(
        id=raw.get("Id") or "",
        name=name,
        image=raw.get("Image") or "",
        state=raw.get("State") or "",
        status=raw.get("Status") or "",
        network_mode=host_config.get("NetworkMode") or "",
        networks=networks,
        ips=ips,
        published=tuple(published),
        project=labels.get("com.docker.compose.project"),
    )


def _network_from_json(raw: Mapping[str, Any]) -> Network:
    members = raw.get("Containers") or {}
    names = tuple(
        sorted((v or {}).get("Name") or "" for v in members.values() if (v or {}).get("Name"))
    )
    return Network(
        name=raw.get("Name") or "",
        id=raw.get("Id") or "",
        driver=raw.get("Driver") or "",
        internal=bool(raw.get("Internal")),
        containers=names,
    )


def build(
    containers_json: Iterable[Mapping[str, Any]],
    network_details: Mapping[str, Mapping[str, Any]],
    verified_at: str | None = None,
) -> Inventory:
    """Build an inventory from container summaries and network *details*.

    ``network_details`` must come from ``GET /networks/{name}``. Passing the
    output of ``GET /networks`` here is the documented trap: every network would
    report zero containers and the graph would silently have no edges.
    """
    containers: dict[str, Container] = {}
    duplicates: dict[str, list[str]] = {}

    for raw in containers_json:
        container = _container_from_json(raw)
        if container.name in containers:
            duplicates.setdefault(container.name, [containers[container.name].id])
            duplicates[container.name].append(container.id)
            continue
        containers[container.name] = container

    networks = {n: _network_from_json(raw) for n, raw in network_details.items() if n}

    return Inventory(
        containers=containers,
        networks=networks,
        verified_at=verified_at or _utcnow(),
        duplicates={k: tuple(v) for k, v in duplicates.items()},
    )


def build_from_summary(
    containers_json: Iterable[Mapping[str, Any]],
    network_summaries: Iterable[Mapping[str, Any]],
) -> Inventory:
    """Build from ``GET /networks`` -- the wrong way, kept so the trap is testable.

    Every **network** ends up with no members. Note the limit of the trap, since
    it is easy to overstate: container-side attachments come from
    ``NetworkSettings`` and survive, so ``containers_on`` still works. What
    breaks is the network-side view -- ``Network.containers`` is empty for every
    network, so anything that enumerates members *from the network* (a listing,
    a count, a bridge analysis) is silently wrong.
    """
    return build(containers_json, {raw.get("Name", ""): raw for raw in network_summaries})


def load(client: Any, verified_at: str | None = None) -> Inventory:
    """Fetch and build the inventory from a live Docker client."""
    containers = client.containers()
    names = [n.get("Name") for n in client.networks() if n.get("Name")]
    details = {name: client.network(name) for name in names}
    return build(containers, details, verified_at=verified_at)
