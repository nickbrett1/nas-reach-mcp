"""Reachability as a graph query, not a name lookup.

A container is reachable *by name* from a network if and only if it is attached
to that network: Docker's embedded DNS resolves container names within a
user-defined network and nowhere else.

One measured nuance, and the implementation is deliberately honest about it:

    "not attached" means the NAME does not resolve. It does not mean the
    address is unreachable. Docker bridges route to each other through the
    host, so a container's bridge IP is often reachable even from a network it
    is not attached to. That is host routing, not Docker DNS, and it is not
    something a name-based caller can rely on -- so it is reported in a note,
    never as the answer.

Every answer carries ``verified_at``. Everything here is runtime state and
runtime state goes stale: one network gained a member inside a single day while
this tool was being designed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .inventory import LOOPBACK_ADDRESS, Container, Inventory, is_loopback
from .vantage import Vantage

# Outcome vocabulary. The first four come from the design memo; the rest are
# additions the implementation needed in order to answer negatively and
# specifically, which the memo requires ("returning empty is a failure mode").
RESOLVED = "resolved"
HOST_ONLY = "host_only"
NOT_ATTACHED = "not_attached"
UNRESOLVABLE_NAME = "unresolvable_name"
UNKNOWN_PORT = "unknown_port"
AMBIGUOUS = "ambiguous"
UNKNOWN_VANTAGE = "unknown_vantage"
UNKNOWN_NETWORK = "unknown_network"
UNKNOWN_CONTAINER = "unknown_container"

DEFAULT_SCHEME = "http"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Address:
    """A candidate address for one vantage. Never a confirmed one."""

    vantage: str
    url: str | None
    port: int | None
    note: str

    def as_dict(self) -> dict[str, object]:
        return {"vantage": self.vantage, "url": self.url, "port": self.port, "note": self.note}


@dataclass(frozen=True)
class Resolution:
    status: str
    service: str
    vantage: str
    url: str | None
    port: int | None
    reason: str
    addresses: tuple[Address, ...]
    container: Mapping[str, object] | None
    verified_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "service": self.service,
            "from": self.vantage,
            "url": self.url,
            "port": self.port,
            "reason": self.reason,
            "alternatives": [a.as_dict() for a in self.addresses],
            "container": self.container,
            "verified_at": self.verified_at,
        }


def preferred_container_port(container: Container) -> tuple[int | None, str]:
    """Pick the port a caller should try, and say how confident that is.

    Docker's summary API only reveals a container-side port when that port is
    also *published*. An internal listener behind a proxy is therefore
    invisible, which is why an unknown port is reported as unknown rather than
    guessed.
    """
    ports = container.container_ports
    if not ports:
        return None, "no published binding, so no container-side port is visible to the Docker API"
    if len(ports) == 1:
        return ports[0], "the container's single published container-side port"
    return ports[0], f"the lowest of {len(ports)} container-side ports: {list(ports)}"


def container_summary(container: Container) -> dict[str, object]:
    return {
        "name": container.name,
        "image": container.image,
        "state": container.state,
        "status": container.status,
        "network_mode": container.network_mode,
        "networks": list(container.networks),
        "ips": dict(container.ips),
        "published": [
            {"host_port": b.host_port, "container_port": b.container_port, "proto": b.proto}
            for b in container.published
        ],
        "compose_project": container.project,
        "host_network": container.is_host_network,
        "bridge": container.is_bridge,
    }


def host_address(
    container: Container, hostname: str, scheme: str = DEFAULT_SCHEME
) -> tuple[str | None, int | None, str | None]:
    """The host-vantage address for a container: ``(url, port, note)``.

    A loopback-published port resolves to ``127.0.0.1``, not to the host's name.
    Answering ``http://<hostname>:<port>`` for a loopback bind would be an
    address that does not work from anywhere except the host itself.
    """
    binding = container.primary_binding
    if binding is None:
        return None, None, None

    if is_loopback(binding.host_ip):
        return (
            f"{scheme}://{LOOPBACK_ADDRESS}:{binding.host_port}",
            binding.host_port,
            (
                f"published {binding.host_port}->{binding.container_port} on "
                f"{binding.host_ip}: an address on the host and nowhere else"
            ),
        )

    return (
        f"{scheme}://{hostname}:{binding.host_port}",
        binding.host_port,
        (
            f"published {binding.host_port}->{binding.container_port} on "
            f"{binding.host_ip or 'all interfaces'}; the host port is the address from the host"
        ),
    )


def addresses_for(
    inventory: Inventory,
    container: Container,
    hostname: str = "nas",
    scheme: str = DEFAULT_SCHEME,
) -> tuple[Address, ...]:
    """Every vantage this container answers to, with the address for each.

    This is the part a single flat answer would get wrong: the same service has
    a different address from the host than from each of its networks.
    """
    addresses: list[Address] = []
    port, port_note = preferred_container_port(container)

    # From the host: a published port is the address. This is how the container
    # is meant to be reached from outside Docker.
    host_url, host_port, host_note = host_address(container, hostname, scheme)
    if host_url is not None:
        addresses.append(
            Address(vantage="host", url=host_url, port=host_port, note=host_note or "")
        )
    elif container.is_host_network:
        addresses.append(
            Address(
                vantage="host",
                url=None,
                port=None,
                note=(
                    "network_mode: host, so it binds the host's namespace directly; "
                    "the Docker API does not report its ports and no name exists on "
                    "any network. Reachable by host port or not at all."
                ),
            )
        )
    else:
        addresses.append(
            Address(
                vantage="host",
                url=None,
                port=port,
                note=(
                    "not published, so there is no host address. Its bridge "
                    f"IPs ({dict(container.ips)}) may still route from the host."
                ),
            )
        )

    # From each attached network: the container name is the address.
    for network in container.networks:
        ip = container.ip_on(network)
        if port is None:
            addresses.append(
                Address(
                    vantage=f"network:{network}",
                    url=None,
                    port=None,
                    note=f"name resolves here (IP {ip}) but {port_note}",
                )
            )
            continue
        addresses.append(
            Address(
                vantage=f"network:{network}",
                url=f"{scheme}://{container.name}:{port}",
                port=port,
                note=f"resolves by container name on {network}; {port_note}",
            )
        )

    return tuple(addresses)


def resolve(
    inventory: Inventory,
    name: str,
    vantage: Vantage,
    hostname: str = "nas",
    scheme: str = DEFAULT_SCHEME,
) -> Resolution:
    """Answer: what is the address of ``name`` from ``vantage``, or why not."""
    verified_at = inventory.verified_at

    def finish(
        status: str,
        reason: str,
        url: str | None = None,
        port: int | None = None,
        container: Container | None = None,
        addresses: tuple[Address, ...] = (),
    ) -> Resolution:
        return Resolution(
            status=status,
            service=name,
            vantage=vantage.label,
            url=url,
            port=port,
            reason=reason,
            addresses=addresses,
            container=container_summary(container) if container else None,
            verified_at=verified_at,
        )

    # 1. The vantage itself must exist, or every answer below is meaningless.
    if vantage.kind == "network" and (vantage.target or "") not in inventory.networks:
        return finish(
            UNKNOWN_NETWORK,
            f"there is no Docker network named {vantage.target!r}; "
            f"{len(inventory.networks)} networks exist",
        )
    if vantage.kind == "container" and (vantage.target or "") not in inventory.containers:
        return finish(
            UNKNOWN_CONTAINER,
            f"there is no container named {vantage.target!r} to stand behind",
        )

    # 2. The service itself must be a container Docker knows about.
    container = inventory.containers.get(name)
    if container is None:
        return finish(
            UNRESOLVABLE_NAME,
            (
                f"{name!r} is not a running Docker container name on this host. "
                "DSM packages and host processes are invisible to the Docker API, "
                "so they cannot be resolved here; a name that only exists as a "
                "host resolver or Tailscale MagicDNS entry is not in this graph."
            ),
        )

    if name in inventory.duplicates:
        return finish(
            AMBIGUOUS,
            (
                f"{name!r} matches {1 + len(inventory.duplicates[name])} containers with "
                "different ids. Two containers sharing a name on different networks is "
                "a real state, and guessing between them would be a confidently wrong "
                "answer."
            ),
            container=container,
            addresses=addresses_for(inventory, container, hostname, scheme),
        )

    addresses = addresses_for(inventory, container, hostname, scheme)

    # 3. A host-network container has no name on any network, by construction.
    if vantage.kind != "host" and container.is_host_network:
        return finish(
            HOST_ONLY,
            (
                f"{name!r} runs network_mode: host, so it has no name on any Docker "
                f"network and cannot be resolved from {vantage.label}. It is reachable "
                "by host port from the host vantage, or not at all."
            ),
            container=container,
            addresses=addresses,
        )

    # 4. The host vantage: the address is a host port.
    if vantage.kind == "host":
        if container.primary_binding is not None:
            host_url, host_port, host_note = host_address(container, hostname, scheme)
            return finish(
                RESOLVED,
                f"from the host, {name!r} answers at {host_url} ({host_note})",
                url=host_url,
                port=host_port,
                container=container,
                addresses=addresses,
            )
        if container.is_host_network:
            return finish(
                HOST_ONLY,
                (
                    f"{name!r} runs network_mode: host. It is reachable by host port "
                    "or not at all, and the Docker API does not report which ports it "
                    "binds -- that needs the host's own socket table."
                ),
                container=container,
                addresses=addresses,
            )
        return finish(
            UNKNOWN_PORT,
            (
                f"{name!r} publishes no host port, so the host vantage has no address "
                f"for it. Its bridge IPs ({dict(container.ips)}) may still route from "
                "the host, but no port is visible to the Docker API."
            ),
            container=container,
            addresses=addresses,
        )

    # 5. A network vantage.
    if vantage.kind == "network":
        network = vantage.target or ""
        direct = network in container.networks

        if not direct:
            elsewhere = ", ".join(container.networks) or "no network"
            route_note = (
                f"Its bridge IPs ({dict(container.ips)}) may still route through the "
                "host, but that is host routing rather than Docker DNS, and a "
                "name-based caller cannot rely on it."
            )
            return finish(
                NOT_ATTACHED,
                (
                    f"{name!r} is not attached to {network!r}, so its name will not "
                    f"resolve from there. It is attached to: {elsewhere}. {route_note}"
                ),
                container=container,
                addresses=addresses,
            )

        port, port_note = preferred_container_port(container)
        if port is None:
            return finish(
                UNKNOWN_PORT,
                (
                    f"{name!r} is attached to {network!r} and its name resolves there, "
                    f"but {port_note}"
                ),
                container=container,
                addresses=addresses,
            )
        return finish(
            RESOLVED,
            f"from {network!r}, {name!r} answers by container name on port {port}",
            url=f"{scheme}://{name}:{port}",
            port=port,
            container=container,
            addresses=addresses,
        )

    # 6. A container vantage: reachable if it shares a network with that container.
    behind = inventory.containers.get(vantage.target or "")
    if behind is None:  # pragma: no cover - guarded in step 1
        return finish(UNKNOWN_CONTAINER, f"no container named {vantage.target!r}")

    shared = sorted(set(behind.networks) & set(container.networks))
    if not shared:
        return finish(
            NOT_ATTACHED,
            (
                f"{name!r} shares no network with {behind.name!r}, so its name will not "
                f"resolve through it. {behind.name!r} is attached to "
                f"{list(behind.networks)}; {name!r} is attached to "
                f"{list(container.networks)}."
            ),
            container=container,
            addresses=addresses,
        )

    port, port_note = preferred_container_port(container)
    if port is None:
        return finish(
            UNKNOWN_PORT,
            (
                f"{name!r} is reachable through {behind.name!r} over {shared}, but "
                f"{port_note}"
            ),
            container=container,
            addresses=addresses,
        )
    return finish(
        RESOLVED,
        (
            f"through {behind.name!r}, {name!r} answers by container name on "
            f"{shared} over port {port}"
        ),
        url=f"{scheme}://{name}:{port}",
        port=port,
        container=container,
        addresses=addresses,
    )


def reachable_from(inventory: Inventory, vantage: Vantage, hostname: str = "nas") -> list[dict[str, object]]:
    """Everything answerable from a vantage, each with its address.

    Never empty where a negative answer is possible: a vantage with no members
    still reports which networks do have members, because an empty list is the
    failure mode the memo calls out.
    """
    out: list[dict[str, object]] = []

    if vantage.kind == "host":
        for container in inventory.containers.values():
            if container.published or container.is_host_network:
                resolution = resolve(inventory, container.name, vantage, hostname)
                out.append(
                    {
                        "name": container.name,
                        "url": resolution.url,
                        "host_port": resolution.port,
                        "reachable_by": (
                            "host port" if container.published else "host port only (host network)"
                        ),
                    }
                )
        return out

    if vantage.kind == "network":
        network = vantage.target or ""
        for container in inventory.containers_on(network):
            resolution = resolve(inventory, container.name, vantage, hostname)
            out.append(
                {
                    "name": container.name,
                    "url": resolution.url,
                    "network": network,
                    "ip": container.ip_on(network),
                    "also_on": [n for n in container.networks if n != network],
                }
            )
        return out

    behind = inventory.containers.get(vantage.target or "")
    if behind is None:  # pragma: no cover - guarded by caller
        return out

    seen: set[str] = set()
    for network in behind.networks:
        for container in inventory.containers_on(network):
            if container.name in seen or container.name == behind.name:
                continue
            seen.add(container.name)
            resolution = resolve(inventory, container.name, vantage, hostname)
            out.append(
                {
                    "name": container.name,
                    "url": resolution.url,
                    "via_network": network,
                    "ip_on": container.ip_on(network),
                }
            )
    return out
