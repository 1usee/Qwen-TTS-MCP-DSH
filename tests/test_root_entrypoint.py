"""Root entry point tests (``python server.py ...``, spec §9.3 / deviation D8).

Regression cover: the shim used to forward ``--transport`` to the CLI first, and
because ``--transport`` is only valid on the CLI's ``run`` subcommand the CLI
printed help and exited 2 — so the documented command never started the server.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_root_server():
    """Import the root ``server.py`` as a standalone module."""
    spec = importlib.util.spec_from_file_location("qv_root_server", _ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def root_server():
    return _load_root_server()


@pytest.mark.parametrize("flag", ["--transport", "--host", "--port", "--allow-non-loopback"])
def test_documented_flags_are_owned_by_the_shim(root_server, flag):
    assert root_server._owns_argv([flag, "stdio"])
    assert root_server._owns_argv([f"{flag}=stdio"])


def test_cli_style_invocations_are_not_owned_by_the_shim(root_server):
    for argv in (["config", "show"], ["version"], ["balance"], ["--help"]):
        assert not root_server._owns_argv(argv)


def test_transport_flag_routes_to_run_server_not_the_cli(root_server, monkeypatch):
    """The bug this pins: ``--transport stdio`` must not be delegated to the CLI.

    Before the fix, ``_delegate_to_cli`` was consulted first and returned 2 (the
    CLI printed help because ``--transport`` is not a top-level option), so the
    documented command never started the server.
    """
    import qwen_voice_mcp.server as real_server

    calls: dict[str, object] = {}

    def fake_run_server(options):  # noqa: ANN001
        calls["run_server_options"] = options
        return 0

    def fake_delegate(argv):  # noqa: ANN001
        calls["delegated"] = list(argv)
        return 2  # exactly what the CLI returned for `--transport stdio`

    monkeypatch.setattr(real_server, "run_server", fake_run_server)
    monkeypatch.setattr(root_server, "_delegate_to_cli", fake_delegate)

    assert root_server.main(["--transport", "stdio"]) == 0
    assert "delegated" not in calls, "CLI must not receive --transport"
    assert calls["run_server_options"].transport == "stdio"


def test_cli_style_invocation_is_delegated_to_the_cli(root_server, monkeypatch):
    """The complementary case: ``config show`` belongs to the CLI."""
    import qwen_voice_mcp.server as real_server

    calls: dict[str, object] = {}

    def fake_run_server(options):  # pragma: no cover - must not be reached
        calls["run_server_options"] = options
        return 0

    def fake_delegate(argv):  # noqa: ANN001
        calls["delegated"] = list(argv)
        return 0

    monkeypatch.setattr(real_server, "run_server", fake_run_server)
    monkeypatch.setattr(root_server, "_delegate_to_cli", fake_delegate)

    assert root_server.main(["config", "show"]) == 0
    assert calls["delegated"] == ["config", "show"]
    assert "run_server_options" not in calls


def test_selftest_flag_is_recognised_by_the_parser(root_server):
    parsed = root_server.build_parser().parse_args(["--selftest"])
    assert parsed.selftest is True


def test_parser_accepts_the_documented_invocation(root_server):
    parsed = root_server.build_parser().parse_args(["--transport", "stdio"])
    assert parsed.transport == "stdio"
    assert root_server._overrides(parsed) == {"transport": "stdio"}


def test_port_override_is_an_int(root_server):
    parsed = root_server.build_parser().parse_args(["--transport", "streamable-http", "--port", "8791"])
    assert root_server._overrides(parsed)["port"] == 8791


def test_unknown_flag_is_rejected_by_argparse(root_server):
    with pytest.raises(SystemExit) as exc:
        root_server.build_parser().parse_args(["--nonsense"])
    assert exc.value.code == 2


def test_main_routes_selftest_and_exits_zero(root_server, config_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["server.py"])
    assert root_server.main(["--selftest"]) == 0
    assert "SELFTEST OK" in capsys.readouterr().out
