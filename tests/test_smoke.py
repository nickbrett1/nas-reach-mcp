"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import nas_reach_mcp

    assert nas_reach_mcp.__version__
