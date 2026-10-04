"""Read-only Docker Engine API client.

Exactly three endpoints are used, all GETs:

    /containers/json     running containers: published ports, labels, network mode
    /networks            network *summaries* -- membership is always EMPTY here
    /networks/{name}     one network WITH its container membership

The distinction between the last two is load-bearing and was measured, not
assumed: the summary endpoint returns ``"Containers": {}`` for every network,
including ones that demonstrably have members. Membership is therefore only
ever read from the per-name detail endpoint. See ``inventory.load``.

Nothing here writes, and nothing here reads a config file. Runtime state comes
from Docker; config files are explicitly not a source of truth (design memo
section 5).
"""

from __future__ import annotations

from typing import Any, Self

import httpx

DEFAULT_SOCKET = "/var/run/docker.sock"
API_VERSION = "v1.43"


class DockerAPIError(RuntimeError):
    """Raised when the Docker API cannot be read."""


class DockerClient:
    """Minimal read-only client for the Docker Engine API over a unix socket."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float = 10.0) -> None:
        transport = httpx.HTTPTransport(uds=socket_path)
        self._client = httpx.Client(
            transport=transport,
            base_url=f"http://docker/{API_VERSION}",
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get(self, path: str) -> Any:
        try:
            response = self._client.get(path)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - transport failure
            raise DockerAPIError(f"GET {path} failed: {exc}") from exc
        return response.json()

    def containers(self) -> list[dict[str, Any]]:
        """Running containers (the same payload ``docker ps`` renders)."""
        return self._get("/containers/json")

    def networks(self) -> list[dict[str, Any]]:
        """Network *summaries*. Membership is empty here -- by design of the API."""
        return self._get("/networks")

    def network(self, name: str) -> dict[str, Any]:
        """One network in detail, including its ``Containers`` map."""
        return self._get(f"/networks/{name}")

    def container(self, name_or_id: str) -> dict[str, Any]:
        """One container in full detail (``docker inspect``).

        Deliberately unused by the tools: it exposes ``Config.Env``, which means
        secrets. It exists only so a caller can be told to use it explicitly.
        """
        return self._get(f"/containers/{name_or_id}/json")
