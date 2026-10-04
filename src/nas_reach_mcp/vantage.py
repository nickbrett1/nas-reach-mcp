"""The vantage point: ``from`` is a required argument and is never defaulted.

A port is absolute; an address is relative to an observer. ``nas:4000`` and
``litellm:4000`` name the same gateway and neither is *the* address, so the
caller must say where it is standing (design memo sections 2 and 4.1).

Accepted specs:

    host                the NAS itself / any ``network_mode: host`` context
    network:<name>      a container attached to that network
    container:<name>    *through* a specific container (its networks)
    self                this process's own vantage, detected

``self`` exists because a required argument with no discovery path is just a
relocated guess: callers do not know what their own network is called, so
``whoami`` answers it and ``self`` consumes it.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Literal

from .inventory import Inventory

VantageKind = Literal["host", "network", "container"]

SELF_SENTINELS = {"", "self", "auto"}
HOST_SENTINELS = {"host", "nas", "localhost", "0.0.0.0"}


class VantageError(ValueError):
    """Raised for a vantage spec that cannot be parsed."""


@dataclass(frozen=True)
class Vantage:
    kind: VantageKind
    target: str | None
    spec: str

    @property
    def label(self) -> str:
        if self.kind == "host":
            return "host"
        return f"{self.kind}:{self.target}"

    def describe(self, inventory: Inventory) -> str:
        if self.kind == "host":
            return "the NAS host / any host-network context"
        if self.kind == "network":
            net = inventory.networks.get(self.target or "")
            if net is None:
                return f"network {self.target!r}, which does not exist"
            return f"network {self.target!r} ({net.driver}, {len(net.containers)} members)"
        return f"through container {self.target!r}"


def parse(spec: str | None) -> Vantage:
    """Parse a vantage spec. Raises ``VantageError`` on anything unrecognised."""
    raw = (spec or "").strip()
    lowered = raw.lower()

    if lowered in SELF_SENTINELS:
        return Vantage("host", None, "self")
    if lowered in HOST_SENTINELS:
        return Vantage("host", None, "host")

    if ":" in raw:
        kind, _, target = raw.partition(":")
        kind = kind.strip().lower()
        target = target.strip()
        if kind in ("network", "net"):
            if not target:
                raise VantageError("vantage 'network:' needs a network name")
            return Vantage("network", target, raw)
        if kind in ("container", "ctr"):
            if not target:
                raise VantageError("vantage 'container:' needs a container name")
            return Vantage("container", target, raw)
        raise VantageError(
            f"unknown vantage {raw!r}; expected 'host', 'self', "
            f"'network:<name>' or 'container:<name>'"
        )

    # A bare word is read as a network name, because that is the common case
    # and silently reading it as a container would be a confidently wrong answer.
    return Vantage("network", raw, raw)


def own_container_name(inventory: Inventory) -> str | None:
    """Best-effort identification of the container this process runs in.

    Docker sets the hostname to the container id (or the ``hostname:`` key), so
    the check is: does any known container id start with our hostname? Returns
    ``None`` when the process is not in a container attached to a Docker network
    we can see -- which is the case for a host-network container.
    """
    override = os.environ.get("NAS_REACH_SELF_CONTAINER")
    if override:
        return override

    try:
        hostname = socket.gethostname()
    except OSError:  # pragma: no cover - defensive
        return None
    if not hostname:
        return None

    for name, container in inventory.containers.items():
        if container.id.startswith(hostname):
            return name
    return None


def detect_self(inventory: Inventory) -> tuple[Vantage, dict[str, object]]:
    """Resolve the ``self`` vantage, and explain how it was determined.

    This tool is deployed ``network_mode: host`` so that the ``host`` vantage is
    genuinely the host and host-network containers are visible. That makes
    ``self`` equal to ``host`` -- stated explicitly rather than assumed, because
    the answer changes the moment the deployment shape does.
    """
    name = own_container_name(inventory)
    hostname = socket.gethostname()

    if name is None:
        return Vantage("host", None, "self"), {
            "detection": "not-a-visible-docker-container",
            "hostname": hostname,
            "note": (
                "This process is not a container attached to a visible Docker "
                "network, so its vantage is the host."
            ),
        }

    container = inventory.containers[name]
    if container.is_host_network:
        return Vantage("host", None, "self"), {
            "detection": "host-network-container",
            "container": name,
            "hostname": hostname,
            "note": (
                "This process runs network_mode: host, so 'self' is the host "
                "vantage. It can see host ports and all host-network containers, "
                "but it has no name on any Docker network."
            ),
        }

    first = container.networks[0] if container.networks else None
    if first is None:
        return Vantage("host", None, "self"), {
            "detection": "no-network-attachment",
            "container": name,
            "hostname": hostname,
        }

    return Vantage("network", first, "self"), {
        "detection": "bridge-container",
        "container": name,
        "networks": list(container.networks),
        "note": (
            f"Resolved to network {first!r}: the first of "
            f"{len(container.networks)} attachments, in sorted order. A bridge "
            "container genuinely has several vantages, so this choice is "
            "arbitrary but deterministic -- pass 'network:<name>' explicitly "
            "when the particular one matters."
        ),
    }
