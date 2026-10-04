"""The five tools, over stdio MCP.

Design rules enforced here rather than documented and hoped for:

* **No caching.** Every tool call opens a fresh Docker client and re-reads
  runtime state. A cached answer is worse than none (memo section 5).
* **``from`` is required** on the two tools that need it, and is never
  defaulted. A default is how you get a confidently wrong address (section 4.1).
* **A negative answer is as specific as a positive one.** Returning empty is a
  failure mode, so every failure names what it checked and points at where the
  service *is* reachable.
* **Discriminating and probing are separate.** ``resolve_service`` returns a
  candidate; only ``check_reachable`` confirms.
* **Every response is stamped** ``verified_at``.

The parameter is named ``from_vantage`` because ``from`` is a Python keyword.
It is the memo's ``from``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:  # mcp >= 2.0 renamed FastMCP to MCPServer. Support both majors.
    from mcp.server.mcpserver import MCPServer as _ServerImpl
except ModuleNotFoundError:  # pragma: no cover - mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _ServerImpl

from . import __version__
from .docker_api import DEFAULT_SOCKET, DockerAPIError, DockerClient
from .graph import (
    addresses_for,
    container_summary,
    preferred_container_port,
    reachable_from,
    resolve,
)
from .inventory import Inventory, load
from .probe import probe, split_target
from .vantage import Vantage, VantageError, detect_self, parse

# httpx logs every request at INFO. On a stdio MCP server that is pure noise:
# the protocol owns stdout, and one log line per Docker call buries the actual
# answers in the client's log view. Errors still surface.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = _ServerImpl("nas-reach-mcp")

DEFAULT_HOSTNAME = os.environ.get("NAS_REACH_HOSTNAME", "nas")


def _inventory() -> Inventory:
    socket_path = os.environ.get("DOCKER_SOCKET", DEFAULT_SOCKET)
    with DockerClient(socket_path=socket_path) as client:
        return load(client)


def _error(exc: Exception) -> dict[str, Any]:
    return {
        "error": type(exc).__name__,
        "detail": str(exc),
        "hint": (
            "The Docker API could not be read. This tool is read-only and needs "
            "/var/run/docker.sock mounted; a read-only mount does not make the "
            "socket read-only, it is root-equivalent either way."
        ),
    }


def _vantage(spec: str, inventory: Inventory) -> tuple[Vantage, dict[str, Any]]:
    vantage = parse(spec)
    if vantage.spec == "self":
        vantage, detection = detect_self(inventory)
        return vantage, detection
    return vantage, {}


def _probe_vantage_label(vantage: Vantage, detection: dict[str, Any]) -> str:
    """Describe the namespace the probe actually runs in, from runtime state.

    This is a fact read from ``detect_self``, not a claim baked into the code:
    if the deployment shape changes -- a bridge container instead of
    ``network_mode: host`` -- this string changes with it, and so does the
    vantage the by-name evidence is measured against.
    """
    container = detection.get("container")
    if container is None:
        return f"self (this process is not a visible Docker container; vantage {vantage.label})"
    if detection.get("detection") == "host-network-container":
        return f"self (this container {container!r}; network_mode: host; vantage {vantage.label})"
    return f"self (this container {container!r}; vantage {vantage.label})"


def _by_name_evidence_applies(requested: Vantage, probe_vantage: Vantage) -> bool:
    """Is a successful *by-name* probe evidence for the requested vantage?

    The probe runs in the process's own namespace, and a by-name address is the
    address *from a vantage*. So a by-name success only confirms the requested
    vantage when the two are the same network namespace -- which is why a
    host-network process cannot confirm a ``network:*`` vantage by name, only
    by IP.
    """
    if requested.kind == "host":
        return probe_vantage.kind == "host"
    if requested.kind == "network":
        return probe_vantage.kind == "network" and probe_vantage.target == requested.target
    return probe_vantage.kind == "container" and probe_vantage.target == requested.target


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Report this process's own vantage point.

    The ``from`` argument is required and never defaulted, so it must also be
    *discoverable* -- otherwise every caller guesses, which is the exact failure
    this tool exists to prevent. This tool answers "where am I standing".
    """
    try:
        inventory = _inventory()
    except DockerAPIError as exc:
        return _error(exc)

    vantage, detection = _vantage("self", inventory)
    return {
        "vantage": vantage.label,
        "resolved_from": vantage.spec,
        "detection": detection,
        "hostname": DEFAULT_HOSTNAME,
        "deployment_note": (
            "Deployed with network_mode: host, which is what makes the 'host' "
            "vantage genuinely the host and makes every host-network container "
            "visible. The cost: this process has no name on any Docker network, "
            "so it cannot resolve container names -- only host names and ports."
        ),
        "verified_at": inventory.verified_at,
    }


@mcp.tool()
def list_networks() -> dict[str, Any]:
    """List Docker networks with their members and the bridges between them.

    A container attached to more than one network is a bridge, and bridges are
    the interesting part of the graph: ``registry`` fronts containers that live
    on ``registry_net``, where nothing on ``svc_net`` can name them.
    """
    try:
        inventory = _inventory()
    except DockerAPIError as exc:
        return _error(exc)

    networks = [
        {
            "name": net.name,
            "driver": net.driver,
            "internal": net.internal,
            "container_count": len(net.containers),
            "containers": list(net.containers),
        }
        for net in sorted(inventory.networks.values(), key=lambda n: n.name)
    ]

    return {
        "networks": networks,
        "network_count": len(networks),
        "host_network_containers": [c.name for c in inventory.host_only],
        "bridges": [
            {"name": c.name, "networks": list(c.networks)} for c in sorted(inventory.bridges, key=lambda c: c.name)
        ],
        "containers_running": len(inventory.containers),
        "note": (
            "Host-network containers have no membership on any network and are "
            "listed separately: they are reachable by host port or not at all."
        ),
        "verified_at": inventory.verified_at,
    }


@mcp.tool()
def list_services(from_vantage: str = "self") -> dict[str, Any]:
    """List what is reachable from a vantage, with each one's address.

    ``from_vantage`` accepts ``self``, ``host``, ``network:<name>`` or
    ``container:<name>``. Optional here (the only tool where it is), because
    "everything from where I am" is a complete question on its own.
    """
    try:
        inventory = _inventory()
    except DockerAPIError as exc:
        return _error(exc)

    try:
        vantage, detection = _vantage(from_vantage, inventory)
    except VantageError as exc:
        return {"error": "VantageError", "detail": str(exc)}

    services = reachable_from(inventory, vantage, DEFAULT_HOSTNAME)

    return {
        "from": vantage.label,
        "vantage_description": vantage.describe(inventory),
        "detection": detection,
        "count": len(services),
        "services": services,
        "verified_at": inventory.verified_at,
    }


@mcp.tool()
def resolve_service(name: str, from_vantage: str) -> dict[str, Any]:
    """Answer: what is the address of ``name`` from ``from_vantage`` -- or why not.

    Returns a *candidate*. Only ``check_reachable`` confirms one: an address
    derived from the graph is never itself evidence that something answers.

    Negative answers are specific: ``not_attached`` names the networks the
    service *is* on, ``host_only`` says no name exists anywhere, and
    ``unresolvable_name`` distinguishes "no such container" from "the name does
    not resolve from here".
    """
    try:
        inventory = _inventory()
    except DockerAPIError as exc:
        return _error(exc)

    try:
        vantage, detection = _vantage(from_vantage, inventory)
    except VantageError as exc:
        return {"error": "VantageError", "detail": str(exc)}

    resolution = resolve(inventory, name, vantage, DEFAULT_HOSTNAME)
    payload = resolution.as_dict()
    payload["vantage_description"] = vantage.describe(inventory)
    if detection:
        payload["detection"] = detection
    return payload


@mcp.tool()
def check_reachable(name_or_url: str, from_vantage: str, identity: bool = True) -> dict[str, Any]:
    """Probe a service and report whether it answers -- from this process.

    This is the only tool that confirms anything, and it is honest about a
    structural limit: the probe runs in *this* container's network namespace.
    When the requested vantage is a network this process is not on, the
    name-based address cannot be resolved here (that is expected), so the probe
    also tries the container's bridge IP, which is what actually answers.

    HTTP ``000`` is never returned as a single opaque failure. The outcome is
    classified: ``unresolvable_name`` (name never resolved), ``refused`` (port
    actively refused), ``timeout`` (filtered), ``unreachable`` (no route).
    """
    try:
        inventory = _inventory()
    except DockerAPIError as exc:
        return _error(exc)

    try:
        vantage, detection = _vantage(from_vantage, inventory)
    except VantageError as exc:
        return {"error": "VantageError", "detail": str(exc)}

    # The vantage the *probe* runs in, read from runtime state. Reused when the
    # caller already asked for 'self'; otherwise detected independently.
    if vantage.spec == "self":
        own_vantage, own_detection = vantage, detection
    else:
        own_vantage, own_detection = detect_self(inventory)

    notes: list[str] = []
    resolution = None
    candidate_url: str | None = None
    ip_target: str | None = None

    if "://" in name_or_url:
        candidate_url = name_or_url
        scheme, host, url_port, url_path = split_target(name_or_url)
        container = inventory.containers.get(host)
        if container is None:
            notes.append(
                f"target was a URL; its host {host!r} is not a known container, so it "
                "was probed as given and no graph lookup was done"
            )
        else:
            # A URL's host can name a container, so it gets the same graph lookup
            # and by-IP fallback a bare name would. The URL itself is still what
            # the primary probe targets, and the fallback keeps its port and path
            # so the same endpoint is measured.
            resolution = resolve(inventory, host, vantage, DEFAULT_HOSTNAME)
            notes.append(f"target was a URL; the graph lookup was done against its host {host!r}")
            ip = None
            if vantage.kind == "network":
                ip = container.ip_on(vantage.target or "")
            if ip is None and container.ips:
                ip = next(iter(container.ips.values()))
            if ip:
                ip_target = f"{scheme}://{ip}:{url_port}{url_path}"
                notes.append(
                    f"also probing {ip_target}: the probe runs in this process's "
                    "network namespace, where a container name resolves only if that "
                    "container is on the same network"
                )
    else:
        resolution = resolve(inventory, name_or_url, vantage, DEFAULT_HOSTNAME)
        candidate_url = resolution.url

        container = inventory.containers.get(name_or_url)
        if container is not None:
            port = resolution.port
            if port is None:
                port, _ = preferred_container_port(container)
            ip = None
            if vantage.kind == "network":
                ip = container.ip_on(vantage.target or "")
            if ip is None and container.ips:
                ip = next(iter(container.ips.values()))
            if ip and port:
                ip_target = f"http://{ip}:{port}"
                notes.append(
                    f"also probing {ip_target}: the probe runs in this process's "
                    "network namespace, where a container name resolves only if that "
                    "container is on the same network"
                )

    if candidate_url is None and ip_target is None:
        return {
            "service": name_or_url,
            "from": vantage.label,
            "outcome": "no_address",
            "resolution": resolution.as_dict() if resolution else None,
            "notes": notes,
            "detail": (
                "No address could be formed for that vantage, so there was nothing "
                "to probe. Read 'resolution' for why."
            ),
            "verified_at": inventory.verified_at,
        }

    primary = probe(candidate_url, identity=identity) if candidate_url else None
    fallback = probe(ip_target, identity=identity) if ip_target else None
    if fallback is not None and fallback.outcome == "ok" and (primary is None or primary.outcome != "ok"):
        notes.append(
            "the by-IP probe succeeded where the primary (by-name) probe did not; "
            "that difference is the vantage point, not a fault"
        )

    by_name_ok = primary is not None and primary.outcome == "ok"
    by_ip_ok = fallback is not None and fallback.outcome == "ok"

    # The flag is derived from the evidence, not from ``vantage.kind``: a
    # successful by-name probe confirms the requested vantage only when the
    # probe runs in that same namespace, and a successful by-IP probe confirms a
    # network/container vantage because the bridge IP is what actually answers.
    confirmed_by: str | None = None
    if by_name_ok and _by_name_evidence_applies(vantage, own_vantage):
        confirmed_by = "by-name"
    elif by_ip_ok and vantage.kind in ("network", "container"):
        confirmed_by = "by-ip"
    confirms = confirmed_by is not None

    # Never leave a bare ``false`` next to a successful probe that a reader would
    # take as proof: say why the ok probe is not evidence for this vantage.
    confirms_note: str | None = None
    if not confirms and by_name_ok:
        confirms_note = (
            f"the by-name probe returned ok, but it ran from the {own_vantage.label!r} "
            f"namespace rather than the requested {vantage.label!r} vantage, so it is "
            "not evidence for the requested vantage"
        )
    elif not confirms and by_ip_ok:
        confirms_note = (
            "the by-IP probe returned ok, but it reached a bridge IP through host "
            f"routing rather than the address of the requested {vantage.label!r} vantage"
        )

    return {
        "service": name_or_url,
        "from": vantage.label,
        "vantage_description": vantage.describe(inventory),
        "detection": detection,
        "probe_vantage": _probe_vantage_label(own_vantage, own_detection),
        "confirms_requested_vantage": confirms,
        "confirmed_by": confirmed_by,
        "confirms_note": confirms_note,
        "candidate_url": candidate_url,
        "probe": primary.as_dict() if primary else None,
        "by_ip_probe": fallback.as_dict() if fallback else None,
        "resolution": resolution.as_dict() if resolution else None,
        "notes": notes,
        "verified_at": inventory.verified_at,
    }


@mcp.tool()
def describe_service(name: str) -> dict[str, Any]:
    """Show a container's network attachments, IPs and published ports.

    A convenience wrapper over the same live inventory: useful when the question
    is "what is this thing attached to" rather than "what is its address".
    """
    try:
        inventory = _inventory()
    except DockerAPIError as exc:
        return _error(exc)

    container = inventory.containers.get(name)
    if container is None:
        return {
            "service": name,
            "status": "unresolvable_name",
            "reason": (
                f"{name!r} is not a running Docker container. DSM packages and host "
                "processes are invisible to the Docker API."
            ),
            "verified_at": inventory.verified_at,
        }

    return {
        "service": name,
        "container": container_summary(container),
        "addresses": [a.as_dict() for a in addresses_for(inventory, container, DEFAULT_HOSTNAME)],
        "verified_at": inventory.verified_at,
    }


def main() -> None:
    """Run the stdio MCP server (mcpo wraps this as Streamable HTTP)."""
    mcp.run()


__all__ = ["__version__", "main", "mcp"]
