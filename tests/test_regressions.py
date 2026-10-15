"""Regression guards for the defects the adversarial review found.

Each test here fails against the code as it was before the fix, so it genuinely
pins the behaviour rather than describing it.
"""

from __future__ import annotations

import httpx
import pytest

from qwen_voice_mcp.core import errors, qwen_client
from qwen_voice_mcp.core.balance_checker import validate_credential


# --------------------------------------------------------------------------- #
# DEFECT 1 (critical): the SDK import pointed at a module that does not exist,
# and one bare `except` nulled SpeechSynthesizer/AudioFormat/Files as well, so
# TTS and voice cloning failed 100% of the time. No test could detect it because
# every test monkeypatched the bindings.
# --------------------------------------------------------------------------- #


def test_dashscope_sdk_is_actually_importable():
    assert qwen_client.SDK_AVAILABLE is True, (
        f"DashScope SDK failed to import: {qwen_client.SDK_IMPORT_ERROR}"
    )


@pytest.mark.parametrize(
    "binding",
    ["SpeechSynthesizer", "VoiceEnrollmentService", "AudioFormat", "Files"],
)
def test_every_sdk_binding_is_populated(binding):
    assert getattr(qwen_client, binding) is not None


def test_voice_enrollment_service_comes_from_tts_v2():
    """The module the real SDK exposes it in (there is no voice_enrollment module)."""
    import dashscope.audio.tts_v2 as tts_v2

    assert qwen_client.VoiceEnrollmentService is tts_v2.VoiceEnrollmentService


def test_format_map_resolves_real_audio_format_members():
    """With a healthy SDK the format map holds AudioFormat members, not fallbacks."""
    assert qwen_client.FORMAT_MAP["mp3"] is not None
    assert isinstance(qwen_client.FORMAT_MAP["mp3"], type(qwen_client.AudioFormat.MP3_24000HZ_MONO_256KBPS))


def test_sdk_bindings_are_imported_independently():
    """The four SDK names must not share one try block.

    Before the fix they did, so a single wrong import path (`voice_enrollment`)
    raised on the first statement and left ``SpeechSynthesizer``, ``AudioFormat``
    and ``Files`` unbound too — disabling TTS and cloning entirely.
    """
    import ast
    import pathlib

    source = pathlib.Path(qwen_client.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    body = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in node.body)
    ]
    assert body, "the SDK import block must still exist"

    # Exactly one try block imports the SDK, and it has >1 import statement:
    # each `from dashscope... import ...` is a separate statement, so a failure
    # in one cannot prevent the others from binding.
    sdk_imports = [
        stmt
        for block in body
        for stmt in block.body
        if isinstance(stmt, (ast.Import, ast.ImportFrom))
        and "dashscope" in ast.unparse(stmt)
    ]
    assert len(sdk_imports) >= 4, (
        "SpeechSynthesizer, VoiceEnrollmentService, AudioFormat and Files must be "
        f"imported as separate statements; found {len(sdk_imports)}"
    )


def test_no_import_targets_a_nonexistent_dashscope_module():
    """Every ``dashscope.*`` module the client imports must actually resolve."""
    import ast
    import importlib
    import pathlib

    source = pathlib.Path(qwen_client.__file__).read_text(encoding="utf-8")
    modules = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("dashscope")
    }
    assert modules
    for module in sorted(modules):
        importlib.import_module(module)  # raises if the module does not exist


# --------------------------------------------------------------------------- #
# DEFECT 2: safe_message ran whole sentences through the value-redactor, turning
# each diagnostic into a 10-character fragment.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "InvalidParameter: text must not be empty",
        "Throttling.RateQuota: Requests rate limit exceeded, retry after 3s",
        "ModelNotExist: model cosyvoice-v9 does not exist",
        "plain message with no secret at all",
    ],
)
def test_safe_message_preserves_readable_diagnostics(text):
    assert errors.safe_message(text) == text


@pytest.mark.parametrize(
    "secret",
    [
        "sk-1234567890abcdefghijklmnop",
        "LTAI5tAbCdEfGhIjKlMnOpQr",
    ],
)
def test_safe_message_scrubs_secret_tokens_but_keeps_context(secret):
    out = errors.safe_message(f"Authentication failed for {secret} at https://dashscope.aliyuncs.com")
    assert secret not in out
    assert "[REDACTED]" in out
    # Context survives, which is the whole point of the fix.
    assert "Authentication failed" in out
    assert "dashscope.aliyuncs.com" in out


def test_safe_message_scrubs_assignments_and_bearer_headers():
    for text in (
        "api_key=abcdef1234567890 rejected",
        "token: abcdef1234567890 rejected",
        "Authorization: Bearer abcdef1234567890",
    ):
        out = errors.safe_message(text)
        assert "abcdef1234567890" not in out
        assert "[REDACTED]" in out


def test_classify_keeps_the_message_readable_and_key_free():
    status, message = errors.classify(RuntimeError("InvalidParameter sk-secret1234567890xyz"))
    assert "InvalidParameter" in message
    assert "sk-secret1234567890xyz" not in message


# --------------------------------------------------------------------------- #
# DEFECT 3: the GUI stored the key with no validation (spec §13).
# --------------------------------------------------------------------------- #


def _checker(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_validate_credential_accepts_a_working_key():
    result = validate_credential("sk-good", client=_checker(lambda r: httpx.Response(200, json={"balance": 12.5})))
    assert result.ok is True
    assert result.balance == 12.5
    assert "12.5" in result.detail


def test_validate_credential_rejects_a_missing_key_without_network():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no network call for an empty key")

    assert validate_credential("", client=_checker(handler)).ok is False
    assert validate_credential(None, client=_checker(handler)).ok is False


def test_validate_credential_rejects_a_rejected_key():
    result = validate_credential("sk-bad", client=_checker(lambda r: httpx.Response(401)))
    assert result.ok is False
    assert "失败" in result.detail


def test_validate_credential_never_raises_on_transport_failure():
    def handler(request):
        raise httpx.ConnectError("boom")

    result = validate_credential("sk-x", client=_checker(handler))
    assert result.ok is False and result.detail


def test_gui_save_validates_before_storing(monkeypatch):
    """``_on_save`` must not persist a key that failed validation."""
    from qwen_voice_mcp.gui import config_window as cw

    window = object.__new__(cw.ConfigWindow)  # no Tk needed for this decision

    stored: list[str] = []
    statuses: list[str] = []
    answers: list[bool] = []

    class _Var:
        def __init__(self, value): self._v = value
        def get(self): return self._v
        def set(self, v): statuses.append(v)

    class _MissingTk:
        class TclError(Exception):
            pass

    class _Secret:
        def store(self, name, value): stored.append(value)

    class _Box:
        def showerror(self, *a, **k): pytest.fail(f"unexpected error dialog: {a}")
        def askyesno(self, *a, **k):
            answers.append(True)
            return False  # operator declines to keep an unvalidated key

    window._key_var = _Var("sk-typo")
    window._status_var = _Var("")
    window._port_var = _Var("8765")
    window._threshold_var = _Var("1.00")
    window._transport_var = _Var("stdio")
    window._voice_var = _Var("longanyang")
    window._strict_var = _Var(False)
    window.secret = _Secret()
    window.messagebox = _Box()
    window.options = cw.load_options()
    window._collect_options = lambda: window.options

    from qwen_voice_mcp.core.balance_checker import CredentialCheck

    monkeypatch.setattr(cw.ConfigWindow, "_validate",
                        staticmethod(lambda key: CredentialCheck(False, "密钥验证失败：接口拒绝")))

    window._on_save()

    assert stored == [], "an unvalidated key must not be stored"
    assert answers == [True], "the operator must be asked"
    assert any("未保存" in s for s in statuses)


def test_gui_save_stores_a_validated_key(monkeypatch):
    from qwen_voice_mcp.gui import config_window as cw
    from qwen_voice_mcp.core.balance_checker import CredentialCheck

    window = object.__new__(cw.ConfigWindow)
    stored: list[str] = []
    saved: list[object] = []

    class _Var:
        def __init__(self, value): self._v = value
        def get(self): return self._v
        def set(self, v): pass

    class _Secret:
        def store(self, name, value): stored.append(value)

    class _Box:
        def showerror(self, *a, **k): pytest.fail(f"unexpected error dialog: {a}")
        def askyesno(self, *a, **k):
            pytest.fail("a validated key must not prompt")

    window._key_var = _Var("sk-good")
    window._status_var = _Var("")
    window._port_var = _Var("8765")
    window._threshold_var = _Var("1.00")
    window._transport_var = _Var("stdio")
    window._voice_var = _Var("longyang")
    window._strict_var = _Var(False)
    window.secret = _Secret()
    window.messagebox = _Box()
    window.options = cw.load_options()
    window._collect_options = lambda: window.options

    monkeypatch.setattr(cw.ConfigWindow, "_validate",
                        staticmethod(lambda key: CredentialCheck(True, "已验证")))
    monkeypatch.setattr(cw, "save_options", lambda opts: saved.append(opts) or "saved.json")

    window._on_save()

    assert stored == ["sk-good"]
    assert saved, "options must be persisted"


# --------------------------------------------------------------------------- #
# DEFECT 4: the vault ignored QWEN_VOICE_HOME, so config and secrets split.
# --------------------------------------------------------------------------- #


def test_vault_lives_under_qwen_voice_home(config_home, monkeypatch):
    from qwen_voice_mcp.core.secret_manager import default_vault_path

    monkeypatch.delenv("QWEN_VOICE_VAULT", raising=False)
    assert default_vault_path() == config_home / "vault.enc"


def test_explicit_vault_env_still_wins(config_home, monkeypatch, tmp_path):
    from qwen_voice_mcp.core.secret_manager import default_vault_path

    explicit = tmp_path / "custom.enc"
    monkeypatch.setenv("QWEN_VOICE_VAULT", str(explicit))
    assert default_vault_path() == explicit


# --------------------------------------------------------------------------- #
# DEFECT 5: the CLI's catch-all printed only the exception *class* name, so the
# user saw "错误: PermissionError" with no reason — the same unreadable-diagnostic
# problem already fixed in safe_message. Chinese output also went through the GBK
# code page and was mangled on a stock Windows console.
# --------------------------------------------------------------------------- #


def _raise_via_load_options(monkeypatch, exc):
    """Make ``cli.load_options`` raise, exercising the CLI's catch-all path."""
    import qwen_voice_mcp.cli as cli

    def boom(**kwargs):
        raise exc

    monkeypatch.setattr(cli, "load_options", boom)
    return cli.main(["config", "show"])


def test_cli_failure_message_includes_the_reason(config_home, capsys, monkeypatch):
    """A failure must say *why*, not just which exception class was raised."""

    class Boom(RuntimeError):
        exit_code = None

    assert _raise_via_load_options(monkeypatch, Boom("磁盘已满，无法写入配置")) == 1
    stderr = capsys.readouterr().err
    assert "磁盘已满" in stderr, f"the reason must be shown, got: {stderr!r}"
    assert "Boom" in stderr


def test_cli_failure_message_never_leaks_a_key(config_home, capsys, monkeypatch):
    """Widening the message must not widen the leak surface."""
    secret = "sk-verysecretvalue0123456789"

    class Boom(RuntimeError):
        exit_code = None

    _raise_via_load_options(monkeypatch, Boom(f"auth failed for {secret}"))
    stderr = capsys.readouterr().err
    assert secret not in stderr
    assert "[REDACTED]" in stderr


def test_cli_decided_exit_codes_are_not_treated_as_crashes(config_home, capsys):
    """A deliberate exit code must survive, not be reported as a generic error."""
    import qwen_voice_mcp.cli as cli

    assert cli.main(["config", "set", "not_a_field", "1"]) == 2
    assert "未知配置项" in capsys.readouterr().err


def test_cli_streams_are_utf8_capable():
    """Chinese output must survive a GBK console code page."""
    import sys

    import qwen_voice_mcp.cli  # noqa: F401 - importing performs the reconfiguration

    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding:
            assert encoding.replace("-", "") == "utf8", f"{stream} is {encoding}"
