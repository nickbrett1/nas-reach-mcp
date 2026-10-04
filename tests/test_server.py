"""Smoke test: the server wires up and exposes exactly the tools it should.

No Docker and no network: this only checks that the FastMCP registration is
intact, which is the class of mistake a unit test can catch and a live probe
cannot.
"""

from __future__ import annotations

import asyncio

from nas_reach_mcp.server import mcp


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
