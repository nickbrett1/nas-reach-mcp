"""Shared fixtures: the synthetic Docker API payloads, loaded as an Inventory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nas_reach_mcp.inventory import Inventory, build

FIXTURES = Path(__file__).parent / "fixtures"
STAMP = "2026-10-04T00:00:00+00:00"


def _load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text())


@pytest.fixture
def containers_json() -> list[dict[str, Any]]:
    return _load("containers.json")


@pytest.fixture
def network_summary() -> list[dict[str, Any]]:
    return _load("networks_summary.json")


@pytest.fixture
def network_details() -> dict[str, dict[str, Any]]:
    return {path.stem: _load(f"networks/{path.name}") for path in sorted((FIXTURES / "networks").glob("*.json"))}


@pytest.fixture
def inventory(containers_json: list[dict[str, Any]], network_details: dict[str, dict[str, Any]]) -> Inventory:
    return build(containers_json, network_details, verified_at=STAMP)
