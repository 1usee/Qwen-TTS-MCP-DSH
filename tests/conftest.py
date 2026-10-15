"""Shared pytest fixtures for the qwen-voice-mcp test suite.

Windows/DSH sandbox note — why ``os.mkdir`` is patched here
==========================================================
pytest creates its temporary directories by calling ``os.mkdir(path, 0o700)``
(see ``_pytest.tmpdir.TempPathFactory.getbasetemp`` and ``mktemp`` ->
``make_numbered_dir(..., mode=0o700)``).

On Windows, passing a ``mode`` to ``os.mkdir`` makes CPython set an *explicit
protected DACL* on the new directory instead of letting it inherit the parent's
permissions. Under the DSH file sandbox the process token is not in that DACL,
so the directory pytest just created is unusable to it — ``listdir``, ``write``
and ``rmtree`` all fail with ``PermissionError: [WinError 5]``. That is why
every test using ``tmp_path`` errored during fixture setup.

Calling ``os.mkdir`` without a mode inherits the workspace grant and works fine.
So the fix is to drop the ``mode`` argument on Windows, at the one place that
does the damage, and leave the rest of pytest's temp-directory machinery
(basetemp, numbering, retention, cleanup) untouched.

This is test-harness plumbing only; it changes no product code.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Keep every temporary file inside the workspace: the DSH-managed temp root is
# not reliably writable, while a workspace-local directory is guaranteed usable.
_WORKSPACE_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"
_WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
for _var in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_var] = str(_WORKSPACE_TMP)
tempfile.tempdir = str(_WORKSPACE_TMP)


def _install_inheritable_mkdir() -> None:
    """Make ``os.mkdir`` ignore its ``mode`` argument on Windows.

    Without this, CPython gives the new directory a protected DACL that the
    sandbox token is not in, making the directory unusable immediately after
    creation. Ignoring ``mode`` restores normal inheritance.
    """
    if sys.platform != "win32":
        return
    real_mkdir = os.mkdir
    if getattr(real_mkdir, "_qv_inheritable", False):
        return

    def mkdir_inheriting(path, mode=0o777, *, dir_fd=None):  # noqa: ANN001
        return real_mkdir(path) if dir_fd is None else real_mkdir(path, dir_fd=dir_fd)

    mkdir_inheriting._qv_inheritable = True  # type: ignore[attr-defined]
    mkdir_inheriting._qv_real = real_mkdir  # type: ignore[attr-defined]
    os.mkdir = mkdir_inheriting


_install_inheritable_mkdir()


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Isolate ``QWEN_VOICE_HOME`` so config/vault tests never touch the real profile."""
    home = tmp_path / "qvhome"
    home.mkdir()
    monkeypatch.setenv("QWEN_VOICE_HOME", str(home))
    return home
