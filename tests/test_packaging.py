"""Packaging and portability guards.

These tests encode two failures that are invisible to the unit suite but fatal to
a first-time user:

1. ``mcp`` had no upper bound, so a fresh ``pip install`` pulled mcp 2.x, where
   ``mcp.server.fastmcp`` was renamed to ``mcp.server.mcpserver``. The server then
   died on import before registering a single tool.
2. ``.mcp.json`` hard-coded this developer's conda interpreter and checkout path,
   so every other machine started a non-existent process.

Both are configuration-level regressions, hence this dedicated module.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mcp_requirement() -> str:
    """Return the ``mcp`` entry from ``[project].dependencies``."""
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in payload["project"]["dependencies"]:
        if re.match(r"mcp\b", requirement):
            return requirement
    raise AssertionError("pyproject.toml declares no `mcp` dependency")


def test_mcp_dependency_excludes_the_incompatible_v2_line():
    """A fresh install must resolve mcp 1.x, the API the server is written against."""
    requirement = _mcp_requirement()
    upper_bound = re.search(r"<\s*(\d+)", requirement)
    assert upper_bound, f"`{requirement}` has no upper bound; pip will pull mcp 2.x"
    assert int(upper_bound.group(1)) <= 2, f"`{requirement}` allows an mcp 2.x release"


def test_project_mcp_json_contains_no_machine_specific_paths():
    """The checked-in client config must work on any machine after `pipx install .`."""
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["qwen-voice"]

    assert server["command"] == "qwen-voice-mcp", (
        "command must be the installed console script, not an interpreter path"
    )
    joined = " ".join([server["command"], *server.get("args", [])])
    assert not re.search(r"[A-Za-z]:[\\/]", joined), f"absolute Windows path in {joined!r}"
    assert "/" not in server["command"] and "\\" not in server["command"]

    # The key lives in the encrypted vault, never in a client config file.
    assert "DASHSCOPE_API_KEY" not in server.get("env", {})


def test_readme_quickstart_does_not_require_this_machines_conda_environment():
    """The documented install path must be `pipx`/`uv`, not a personal interpreter."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pipx install" in readme or "uv tool install" in readme
    assert "Conda_envs" not in readme, "README still points at a local conda env"
