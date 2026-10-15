# Qwen Voice MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-quality Python MCP server (`qwen_voice_mcp`) that exposes Qwen/DashScope speech models to DeepseekHarness as four MCP tools, with an encrypted-key Tkinter config center.

**Architecture:** Documented layers (`config` / `core` / `tools` / `gui`) plus one added seam — `core/services.py` — that composes the core modules and owns the single balance-gate implementation. Tools are thin adapters returning one typed `ToolResult`; they never import `dashscope`.

**Tech Stack:** Python 3.11, `mcp` 1.30 (FastMCP), `dashscope` 1.27.5, `cryptography` (AES-256-GCM), `keyring`, `soundfile` 0.14 (libsndfile 1.2.2), `pydantic`, `httpx`, `filelock`, `pytest`, `typer`, `rich`, Tkinter.

**Spec:** `docs/superpowers/specs/2026-10-14-qwen-voice-mcp-design.md`

## Global Constraints

- Interpreter for every command: `D:\Conda_envs\mcp-dev\python.exe` (session env `mcp-dev`). Never rely on bare `python`.
- Project root / cwd: `F:\MCP_Server\Qwen-TTS-MCP-DSH`. All paths below are relative to it.
- **Not a git repository.** `git init` was NOT run. Therefore every "Commit" step in the task template is replaced by a **Verify** step. Do not run `git` commands in any task.
- Package version string: `__version__ = "1.0.0"`.
- `FastMCP(...)` must NOT receive a `version=` argument — the installed `mcp` 1.30 `FastMCP.__init__` has no such parameter. Pass only `name`, `instructions`, `host`, `port`.
- `tools/**` must never `import dashscope`. Only `core/qwen_client.py` may.
- `core/models.py` and `config.py` are leaf modules: no imports from other project modules.
- Balance check is mandatory before every billable call, implemented once in `core/services.py`.
- Secrets: AES-256-GCM only; plaintext key never written to disk, never logged, never placed in an error message. stdio mode logs to **stderr only**.
- Defaults copied verbatim from the spec: `host="127.0.0.1"`, `port=8765`, `default_voice="longanyang"`, `balance_threshold=1.00`, `strict_balance=False`, `balance_timeout_seconds=8.0`, `audio_min_seconds=5.0`, `audio_max_seconds=60.0`, `audio_max_bytes=20*1024*1024`, `log_level="INFO"`.
- Config home: `%APPDATA%\qwen-voice-mcp`; overridable by env `QWEN_VOICE_HOME` (tests MUST use it).
- Env var prefix for config overrides: `QWEN_VOICE_`.
- API key env var name: `DASHSCOPE_API_KEY`. Keyring service name `qwen-voice-mcp`, keyring username `vault-master-key`.
- All tests must run **offline** with `dashscope` and `httpx` network paths mocked.

## Review Focus

Input classes and failure modes the spec implies but no single task's happy-path test covers. Each gets a test in the owning task.

1. **`audio_path` given as base64 or `data:` URI instead of a filesystem path** — must be decoded to a temp file and processed, not crash on `Path(...)` with a 100 KB string (Task 5).
2. **Balance endpoint returns HTTP 200 with an unexpected JSON shape** — must report balance `None`/unknown rather than silently treating it as `0.00` (which would wrongly block every call) (Task 6).
3. **`voice_name` containing characters illegal in a DashScope `prefix`** (Chinese, spaces, uppercase, >10 chars) — must produce a legal `<10` lowercase-alphanumeric prefix and still round-trip the user's name (Task 7).
4. **A TTS request whose text is empty or whitespace-only** — must return `invalid_input` and must NOT call `ensure_balance`/the API (Task 8).
5. **Two concurrent writers to the vault** — must not corrupt or lose a key (Task 3).

---

### Task 1: Project scaffold, constants, and typed result model

**Files:**
- Create: `pyproject.toml`
- Create: `qwen_voice_mcp/__init__.py`
- Create: `config.py`
- Create: `qwen_voice_mcp/core/__init__.py`
- Create: `qwen_voice_mcp/core/models.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `qwen_voice_mcp.__version__: str`
  - `config.DEFAULT_MODELS: list[dict]` — each `{"name","type","description","capabilities","pricing","pricing_per_1k_chars"}`
  - `config.CLONE_UNIT_PRICE: float`, `config.BALANCE_ENDPOINTS: list[str]`, `config.DASHSCOPE_BASE_URL: str`, `config.DEFAULT_*` constants listed in Global Constraints
  - `core.models.Status` (str Enum), `core.models.VoiceModel` (pydantic BaseModel), `core.models.ToolResult` (frozen dataclass), `core.models.BalanceState` (frozen dataclass with `balance: float|None`, `currency: str|None`, `source: str|None`, `last_error: str|None`), `core.models.AudioProbe` (frozen dataclass: `path: Path`, `format: str`, `duration_seconds: float|None`, `sample_rate: int|None`, `channels: int|None`, `size_bytes: int`, `backend: str`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
import json
from qwen_voice_mcp.core.models import Status, ToolResult, VoiceModel, BalanceState


def test_tool_result_ok_only_for_success():
    assert ToolResult(status=Status.SUCCESS, message="ok").ok is True
    assert ToolResult(status=Status.INSUFFICIENT_BALANCE, message="no").ok is False


def test_tool_result_to_dict_always_has_status_and_message():
    d = ToolResult(status=Status.SUCCESS, message="done", data={"a": 1}).to_dict()
    assert d["status"] == "success"
    assert d["message"] == "done"
    assert d["a"] == 1
    assert "warnings" not in d


def test_tool_result_includes_warnings_when_present():
    d = ToolResult(status=Status.SUCCESS, message="done", warnings=["w"]).to_dict()
    assert d["warnings"] == ["w"]


def test_tool_result_is_json_serialisable():
    d = ToolResult(status=Status.SUCCESS, message="m", data={"n": None}).to_dict()
    assert json.loads(json.dumps(d))["n"] is None


def test_status_enum_values_are_the_spec_strings():
    assert Status.INSUFFICIENT_BALANCE.value == "insufficient_balance"
    assert Status.BALANCE_UNAVAILABLE.value == "balance_unavailable"
    assert Status.MODEL_NOT_SUPPORTED.value == "model_not_supported"
    assert Status.NETWORK_ERROR.value == "network_error"


def test_voicemodel_requires_name_and_capabilities():
    m = VoiceModel(name="cosyvoice-v2", type="voice_cloning_and_tts",
                   description="d", capabilities=["text_to_speech"], pricing="p")
    assert m.verified is None
    assert m.source == "builtin"


def test_balance_state_unknown_shape():
    b = BalanceState(balance=None, currency=None, source=None, last_error="all failed")
    assert b.balance is None
    assert b.last_error == "all failed"


def test_default_models_have_required_keys_and_unique_names():
    import config
    names = [m["name"] for m in config.DEFAULT_MODELS]
    assert len(names) == len(set(names))
    for m in config.DEFAULT_MODELS:
        for key in ("name", "type", "description", "capabilities", "pricing", "pricing_per_1k_chars"):
            assert key in m, key


def test_balance_endpoints_are_https_and_ordered():
    import config
    assert config.BALANCE_ENDPOINTS
    assert all(u.startswith("https://") for u in config.BALANCE_ENDPOINTS)
    assert config.BALANCE_ENDPOINTS[0].endswith("/api/v1/account/quota")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp'`

- [ ] **Step 3: Implement constants and the result model**

`config.py` defines the leaf constants. `DEFAULT_MODELS` carries the four models from the spec with a `pricing_per_1k_chars` cost used by `estimating cost`:

```python
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com"
BALANCE_ENDPOINTS = [
    f"{DASHSCOPE_BASE_URL}/api/v1/account/quota",
    f"{DASHSCOPE_BASE_URL}/api/v1/billing/query",
    f"{DASHSCOPE_BASE_URL}/api/v1/account/balance",
]
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "cosyvoice-v2"
DEFAULT_VOICE = "longanyang"
DEFAULT_OUTPUT_FORMAT = "mp3"
BALANCE_THRESHOLD = 1.00
STRICT_BALANCE = False
BALANCE_TIMEOUT_SECONDS = 8.0
AUDIO_MIN_SECONDS = 5.0
AUDIO_MAX_SECONDS = 60.0
AUDIO_MAX_BYTES = 20 * 1024 * 1024
AUDIO_ALLOWED_EXTENSIONS = ("wav", "mp3", "m4a", "flac", "ogg", "aac")
CLONE_UNIT_PRICE = 0.20
LOG_LEVEL = "INFO"
CONFIG_HOME_ENV = "QWEN_VOICE_HOME"
ENV_PREFIX = "QWEN_VOICE_"
API_KEY_ENV = "DASHSCOPE_API_KEY"
KEYRING_SERVICE = "qwen-voice-mcp"
KEYRING_USERNAME = "vault-master-key"
RECHARGE_URL = "https://bailian.console.aliyun.com/"
```

`DEFAULT_MODELS` entries: `cosyvoice-v2`, `cosyvoice-v1`, `qwen-tts`, `sambert-zhichu-v1` — each with `type`, Chinese `description`, `capabilities`, human `pricing`, and numeric `pricing_per_1k_chars`. Set `pricing_per_1k_chars` to the per-1k-character CNY price (e.g. `0.26` for `cosyvoice-v2`); the spec's D5 requires these be treated as **declared, not verified** — the registry attaches `verified=None`.

`core/models.py`: `Status(str, Enum)` with exactly the ten values in spec §6.1 (`success`, `insufficient_balance`, `balance_unavailable`, `auth_error`, `invalid_input`, `model_not_supported`, `rate_limited`, `network_error`, `upstream_error`, `internal_error`). `ToolResult` frozen dataclass whose `to_dict` returns `{"status": self.status.value, "message": self.message, **self.data}` plus `"warnings"` only when non-empty. `VoiceModel` pydantic model with fields above, `verified: bool | None = None`, `source: str = "builtin"`, and `to_dict()` delegating to `model_dump()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_models.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Verify importability of the package**

Run: `D:\Conda_envs\mcp-dev\python.exe -c "import config, qwen_voice_mcp; print(config.DEFAULT_MODEL, qwen_voice_mcp.__version__)"`
Expected: `cosyvoice-v2 1.0.0`

---

### Task 2: Options model and layered config store

**Files:**
- Create: `qwen_voice_mcp/core/config_store.py`
- Test: `tests/test_config_store.py`
- Modify: `tests/conftest.py` (add the `config_home` fixture)

**Interfaces:**
- Consumes: `config.*` constants from Task 1.
- Produces:
  - `core.config_store.Options` — pydantic `BaseModel` with `transport: Literal["stdio","streamable-http","sse"] = "stdio"`, `host: str = "127.0.0.1"`, `port: int = 8765`, `default_model: str`, `default_voice: str`, `default_output_format: str = "mp3"`, `balance_threshold: float = 1.0`, `strict_balance: bool = False`, `balance_timeout_seconds: float = 8.0`, `http_auth_token: str | None = None`, `output_dir: Path`, `temp_dir: Path`, `audio_min_seconds: float = 5.0`, `audio_max_seconds: float = 60.0`, `audio_max_bytes: int = 20971520`, `log_level: str = "INFO"`, `allow_non_loopback: bool = False`
  - `core.config_store.config_home() -> Path` (honours `QWEN_VOICE_HOME`)
  - `core.config_store.load_options(**overrides) -> Options` — precedence CLI kwargs > env > `config.json` > defaults
  - `core.config_store.save_options(opts: Options) -> Path`
  - `core.config_store.Options.reject_non_loopback()` — raises `ValueError` when `host` is not loopback and `allow_non_loopback` is False

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_store.py
import json
import pytest
from pathlib import Path
from qwen_voice_mcp.core.config_store import Options, config_home, load_options, save_options


def test_defaults_match_spec(config_home):
    o = load_options()
    assert (o.host, o.port, o.default_voice) == ("127.0.0.1", 8765, "longanyang")
    assert (o.balance_threshold, o.strict_balance) == (1.00, False)
    assert (o.audio_min_seconds, o.audio_max_seconds) == (5.0, 60.0)
    assert o.audio_max_bytes == 20 * 1024 * 1024


def test_file_beats_defaults(config_home):
    (config_home / "config.json").write_text(json.dumps({"port": 9999}), encoding="utf-8")
    assert load_options().port == 9999


def test_env_beats_file(config_home, monkeypatch):
    (config_home / "config.json").write_text(json.dumps({"port": 9999}), encoding="utf-8")
    monkeypatch.setenv("QWEN_VOICE_PORT", "7777")
    assert load_options().port == 7777


def test_cli_overrides_beat_env(config_home, monkeypatch):
    monkeypatch.setenv("QWEN_VOICE_PORT", "7777")
    assert load_options(port=6666).port == 6666


def test_roundtrip_save_then_load(config_home):
    save_options(load_options(default_voice="longxiaochun", port=8123))
    o = load_options()
    assert (o.default_voice, o.port) == ("longxiaochun", 8123)
    assert (config_home / "config.json").exists()


def test_bad_range_rejected(config_home):
    with pytest.raises(Exception):
        Options(audio_min_seconds=90.0, audio_max_seconds=10.0)


def test_port_out_of_range_rejected(config_home):
    with pytest.raises(Exception):
        Options(port=70000)


def test_non_loopback_rejected_by_default(config_home):
    with pytest.raises(ValueError):
        load_options(host="0.0.0.0").reject_non_loopback()


def test_non_loopback_allowed_when_opted_in(config_home):
    load_options(host="0.0.0.0", allow_non_loopback=True).reject_non_loopback()


def test_config_home_honours_env(config_home):
    assert config_home() == config_home
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_config_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.config_store'`

- [ ] **Step 3: Implement the store**

Add to `tests/conftest.py` a fixture that points `QWEN_VOICE_HOME` at `tmp_path` and returns it:

```python
@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "qvhome"
    home.mkdir()
    monkeypatch.setenv("QWEN_VOICE_HOME", str(home))
    return home
```

`config_store.py` approach: `Options` is a pydantic model with a `model_validator(mode="after")` asserting `audio_min_seconds < audio_max_seconds`, `1 <= port <= 65535`, `balance_threshold >= 0`. `load_options` reads env keys `QWEN_VOICE_<FIELD_UPPER>` for the fields listed above, merges `config.json` (missing/torn file → ignored, not fatal), then applies non-`None` `**overrides`. `output_dir`/`temp_dir` default to `config_home()/"output"` and `config_home()/"tmp"`, created with `mkdir(parents=True, exist_ok=True)`. `save_options` writes UTF-8 JSON via `os.replace` for atomicity. `reject_non_loopback` treats `127.0.0.1`, `localhost`, `::1` as loopback.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_config_store.py -q`
Expected: PASS (10 passed)

---

### Task 3: Encrypted secret vault

**Files:**
- Create: `qwen_voice_mcp/core/secret_manager.py`
- Test: `tests/test_secret_manager.py`

**Interfaces:**
- Consumes: `config.API_KEY_ENV`, `config.KEYRING_SERVICE`, `config.KEYRING_USERNAME`.
- Produces: `core.secret_manager.SecretManager(path: Path | None = None, *, prefer_keyring: bool = True, passphrase: str | None = None)` with `store(name, value) -> None`, `retrieve(name) -> str | None`, `delete(name) -> bool`, `list_names() -> list[str]`, `load_into_env(name=API_KEY_ENV) -> bool`, and `@staticmethod redact(value: str | None) -> str`. Also `VaultError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_secret_manager.py
import json
import threading
import pytest
from qwen_voice_mcp.core.secret_manager import SecretManager, VaultError

KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def sm(tmp_path):
    return SecretManager(path=tmp_path / "vault.enc", prefer_keyring=False, passphrase="pw")


def test_store_then_retrieve_roundtrip(sm):
    sm.store("DASHSCOPE_API_KEY", KEY)
    assert sm.retrieve("DASHSCOPE_API_KEY") == KEY


def test_ciphertext_on_disk_does_not_contain_plaintext(sm):
    sm.store("DASHSCOPE_API_KEY", KEY)
    raw = (sm.path).read_bytes()
    assert KEY.encode() not in raw
    assert b"DASHSCOPE_API_KEY" not in raw  # names are encrypted too


def test_missing_name_returns_none(sm):
    assert sm.retrieve("nope") is None


def test_delete_removes_key(sm):
    sm.store("k", KEY)
    assert sm.delete("k") is True
    assert sm.retrieve("k") is None
    assert sm.delete("k") is False


def test_list_names_excludes_values(sm):
    sm.store("b", "2")
    sm.store("a", "1")
    assert sm.list_names() == ["a", "b"]


def test_wrong_passphrase_fails_closed(tmp_path):
    p = tmp_path / "vault.enc"
    SecretManager(path=p, prefer_keyring=False, passphrase="right").store("k", KEY)
    with pytest.raises(VaultError):
        SecretManager(path=p, prefer_keyring=False, passphrase="wrong").retrieve("k")


def test_tampered_ciphertext_is_detected(tmp_path):
    p = tmp_path / "vault.enc"
    sm = SecretManager(path=p, prefer_keyring=False, passphrase="pw")
    sm.store("k", KEY)
    blob = json.loads(p.read_text(encoding="utf-8"))
    ct = bytearray(__import__("base64").b64decode(blob["ct"]))
    ct[0] ^= 0x01
    blob["ct"] = __import__("base64").b64encode(bytes(ct)).decode()
    p.write_text(json.dumps(blob), encoding="utf-8")
    with pytest.raises(VaultError):
        SecretManager(path=p, prefer_keyring=False, passphrase="pw").retrieve("k")


def test_each_write_uses_a_fresh_nonce(sm):
    sm.store("k", "1")
    n1 = json.loads(sm.path.read_text(encoding="utf-8"))["nonce"]
    sm.store("k", "1")
    n2 = json.loads(sm.path.read_text(encoding="utf-8"))["nonce"]
    assert n1 != n2


def test_load_into_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "from-env")
    sm = SecretManager(path=tmp_path / "v.enc", prefer_keyring=False, passphrase="pw")
    sm.store("DASHSCOPE_API_KEY", KEY)
    assert sm.load_into_env() is False
    assert __import__("os").environ["DASHSCOPE_API_KEY"] == "from-env"


def test_load_into_env_injects_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    sm = SecretManager(path=tmp_path / "v.enc", prefer_keyring=False, passphrase="pw")
    sm.store("DASHSCOPE_API_KEY", KEY)
    assert sm.load_into_env() is True
    assert __import__("os").environ["DASHSCOPE_API_KEY"] == KEY


def test_concurrent_writers_do_not_lose_keys(tmp_path):
    sm = SecretManager(path=tmp_path / "v.enc", prefer_keyring=False, passphrase="pw")
    sm.store("seed", "0")
    errors = []

    def worker(i):
        try:
            SecretManager(path=tmp_path / "v.enc", prefer_keyring=False,
                          passphrase="pw").store(f"k{i}", str(i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    names = SecretManager(path=tmp_path / "v.enc", prefer_keyring=False,
                          passphrase="pw").list_names()
    for i in range(8):
        assert f"k{i}" in names


def test_redact_hides_middle_and_keeps_hint():
    assert SecretManager.redact(None) == "<unset>"
    assert SecretManager.redact("") == "<empty>"
    r = SecretManager.redact(KEY)
    assert KEY not in r and r.startswith("sk-abc") and r.endswith("6789")


def test_redact_of_short_secret_is_all_masked():
    assert SecretManager.redact("abc") == "***"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_secret_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.secret_manager'`

- [ ] **Step 3: Implement the vault**

Approach: one JSON document `{"v":1,"mode":...,"salt":b64,"nonce":b64,"ct":b64}` where the **entire name→value mapping** is encrypted as one UTF-8 JSON blob with AES-256-GCM, so key names are not readable on disk. KEK: with `prefer_keyring=True`, get-or-create 32 random bytes stored via `keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, b64)`; on any `keyring` error fall back to passphrase mode. Passphrase KEK: `PBKDF2HMAC(hashes.SHA256(), length=32, salt=salt, iterations=600_000)`. Every read/write takes an exclusive `filelock.FileLock(str(path) + ".lock")`. Writes go to `path.with_suffix(".tmp")` then `os.replace`. Decryption failure (`InvalidTag`) → `VaultError("vault 解密失败：主密钥或口令不匹配，或文件已被篡改")`. `redact`: `None` → `"<unset>"`, `""` → `"<empty>"`, `len < 8` → `"***"`, else first 6 + `"…"` + last 4.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_secret_manager.py -q`
Expected: PASS (13 passed)

---

### Task 4: Model registry with opt-in verification

**Files:**
- Create: `qwen_voice_mcp/core/model_registry.py`
- Test: `tests/test_model_registry.py`

**Interfaces:**
- Consumes: `config.DEFAULT_MODELS`, `config.DEFAULT_MODEL`; `core.models.VoiceModel`.
- Produces: `core.model_registry.ModelRegistry(models: list[dict] | None = None, *, default_model: str | None = None)` with `all() -> list[VoiceModel]`, `get(name) -> VoiceModel | None`, `require(name) -> VoiceModel` (raises `UnknownModelError`), `default() -> VoiceModel`, `names() -> list[str]`, `supports(name, capability: str) -> bool`, `estimated_cost(name, text_length: int) -> float`, `clone_cost() -> float`, `mark_verified(name, verified: bool) -> None`, and `UnknownModelError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model_registry.py
import pytest
from qwen_voice_mcp.core.model_registry import ModelRegistry, UnknownModelError


def test_builtin_registry_is_non_empty_and_has_default():
    reg = ModelRegistry()
    assert reg.all()
    assert reg.default().name in reg.names()


def test_default_is_first_model_when_unspecified():
    reg = ModelRegistry([{"name": "a", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p", "pricing_per_1k_chars": 1.0}])
    assert reg.default().name == "a"


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        ModelRegistry().require("no-such-model")


def test_get_returns_none_for_unknown():
    assert ModelRegistry().get("no-such-model") is None


def test_verified_defaults_to_none():
    assert ModelRegistry().default().verified is None


def test_mark_verified_sets_flag():
    reg = ModelRegistry()
    name = reg.default().name
    reg.mark_verified(name, True)
    assert reg.get(name).verified is True


def test_declared_price_used_for_cost_estimate():
    reg = ModelRegistry([{"name": "m", "type": "t", "description": "d",
                          "capabilities": ["text_to_speech"], "pricing": "p",
                          "pricing_per_1k_chars": 0.26}])
    assert reg.estimated_cost("m", 1000) == pytest.approx(0.26)
    assert reg.estimated_cost("m", 0) == 0.0


def test_cost_rounds_to_four_decimals():
    reg = ModelRegistry([{"name": "m", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p", "pricing_per_1k_chars": 0.26}])
    assert reg.estimated_cost("m", 42) == 0.0109


def test_unknown_price_is_zero_not_an_error():
    reg = ModelRegistry([{"name": "m", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p"}])
    assert reg.estimated_cost("m", 500) == 0.0


def test_supports_reports_capability():
    reg = ModelRegistry()
    clone = [m for m in reg.all() if "voice_clone" in m.capabilities]
    assert clone, "at least one builtin model must declare voice_clone"
    assert reg.supports(clone[0].name, "voice_clone") is True


def test_clone_cost_is_positive_constant():
    assert ModelRegistry().clone_cost() > 0


def test_custom_models_replace_builtins_entirely():
    reg = ModelRegistry([{"name": "only", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p"}])
    assert reg.names() == ["only"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_model_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.model_registry'`

- [ ] **Step 3: Implement the registry**

Approach: build `VoiceModel` objects from dicts via `VoiceModel(**d)`, keeping declaration order (pydantic tolerates a missing `verified`). Store in a `dict[str, VoiceModel]` preserving insertion order. `estimated_cost` = `round(pricing_per_1k_chars * length / 1000, 4)` with missing price → `0.0`. `clone_cost()` returns `config.CLONE_UNIT_PRICE`. `mark_verified` rebuilds the model with `model_copy(update={"verified": verified})`. Registry is mutable at runtime (`models` list is *not* frozen) so `--verify-models` can annotate it, per spec §D5.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_model_registry.py -q`
Expected: PASS (12 passed)

---

### Task 5: Audio probing, validation, and temp-file management

**Files:**
- Create: `qwen_voice_mcp/core/audio_utils.py`
- Test: `tests/test_audio_utils.py`

**Interfaces:**
- Consumes: `config.AUDIO_*`, `core.models.AudioProbe`, `core.models.Status`/`ToolResult`.
- Produces:
  - `core.audio_utils.probe(path: Path) -> AudioProbe` — never raises for unreadable audio; returns best-effort probe with `duration_seconds=None`.
  - `core.audio_utils.detect_backend() -> str` — `"soundfile" | "pydub" | "stdlib"`
  - `core.audio_utils.materialise_audio(value: str, temp_dir: Path) -> Path` — accepts a filesystem path, a `data:` URI, or bare base64; writes decoded bytes to `temp_dir` and returns the path.
  - `core.audio_utils.validate_reference(path: Path, *, min_seconds=None, max_seconds=None, max_bytes=None) -> ToolResult`
  - `core.audio_utils.new_output_path(output_dir: Path, fmt: str) -> Path`
  - `core.audio_utils.cleanup_temp_files(directory: Path, max_age_hours: float = 24.0) -> int`
  - `core.audio_utils.make_tone_wav(path: Path, seconds: float, sample_rate: int = 16000) -> Path` — stdlib-only WAV generator used by tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_utils.py
import base64
import os
import time
import pytest
from pathlib import Path
from qwen_voice_mcp.core import audio_utils as au
from qwen_voice_mcp.core.models import Status


def test_make_tone_wav_then_probe_reports_duration(tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=2.5)
    p = au.probe(wav)
    assert p.format == "wav"
    assert p.duration_seconds == pytest.approx(2.5, abs=0.05)
    assert p.sample_rate == 16000
    assert p.channels == 1


def test_probe_missing_file_returns_duration_none(tmp_path):
    p = au.probe(tmp_path / "nope.wav")
    assert p.duration_seconds is None


def test_probe_of_non_audio_bytes_returns_duration_none(tmp_path):
    f = tmp_path / "fake.wav"
    f.write_bytes(b"not audio at all" * 10)
    assert au.probe(f).duration_seconds is None


def test_detect_backend_is_a_known_name():
    assert au.detect_backend() in {"soundfile", "pydub", "stdlib"}


def test_validate_accepts_in_range_audio(tmp_path):
    wav = au.make_tone_wav(tmp_path / "ok.wav", seconds=10)
    r = au.validate_reference(wav)
    assert r.status is Status.SUCCESS


def test_validate_rejects_too_short(tmp_path):
    wav = au.make_tone_wav(tmp_path / "short.wav", seconds=4.9)
    r = au.validate_reference(wav)
    assert r.status is Status.INVALID_INPUT
    assert "5" in r.message


def test_validate_rejects_too_long(tmp_path):
    wav = au.make_tone_wav(tmp_path / "long.wav", seconds=60.5)
    r = au.validate_reference(wav)
    assert r.status is Status.INVALID_INPUT


def test_validate_accepts_exact_boundaries(tmp_path):
    lo = au.make_tone_wav(tmp_path / "lo.wav", seconds=5.0)
    hi = au.make_tone_wav(tmp_path / "hi.wav", seconds=60.0)
    assert au.validate_reference(lo).status is Status.SUCCESS
    assert au.validate_reference(hi).status is Status.SUCCESS


def test_validate_rejects_disallowed_extension(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"hi")
    r = au.validate_reference(f)
    assert r.status is Status.INVALID_INPUT


def test_validate_rejects_oversized_file(tmp_path):
    wav = au.make_tone_wav(tmp_path / "big.wav", seconds=10)
    r = au.validate_reference(wav, max_bytes=100)
    assert r.status is Status.INVALID_INPUT
    assert "大小" in r.message or "MB" in r.message


def test_validate_rejects_missing_file(tmp_path):
    assert au.validate_reference(tmp_path / "gone.wav").status is Status.INVALID_INPUT


def test_validate_unknown_duration_is_warning_not_failure(tmp_path, monkeypatch):
    f = tmp_path / "m.m4a"
    f.write_bytes(b"\x00\x00\x00\x20ftypM4A " + os.urandom(64))
    r = au.validate_reference(f)
    assert r.status is Status.SUCCESS
    assert r.warnings


def test_materialise_accepts_plain_path(tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=6)
    assert au.materialise_audio(str(wav), tmp_path) == wav


def test_materialise_decodes_data_uri(tmp_path):
    raw = (au.make_tone_wav(tmp_path / "s.wav", seconds=6)).read_bytes()
    uri = "data:audio/wav;base64," + base64.b64encode(raw).decode()
    out = au.materialise_audio(uri, tmp_path)
    assert out.suffix == ".wav" and out.read_bytes() == raw


def test_materialise_decodes_bare_base64(tmp_path):
    raw = (au.make_tone_wav(tmp_path / "s.wav", seconds=6)).read_bytes()
    out = au.materialise_audio(base64.b64encode(raw).decode(), tmp_path)
    assert out.read_bytes() == raw


def test_materialise_rejects_garbage_without_raising(tmp_path):
    with pytest.raises(ValueError):
        au.materialise_audio("!!!not base64 and not a path!!!", tmp_path)


def test_new_output_path_is_unique_and_keeps_format(tmp_path):
    a = au.new_output_path(tmp_path, "mp3")
    b = au.new_output_path(tmp_path, "mp3")
    assert a != b
    assert a.suffix == ".mp3" and a.parent == tmp_path


def test_cleanup_removes_only_stale_files(tmp_path):
    old = tmp_path / "old.mp3"; old.write_bytes(b"x")
    new = tmp_path / "new.mp3"; new.write_bytes(b"x")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    assert au.cleanup_temp_files(tmp_path, max_age_hours=24) == 1
    assert new.exists() and not old.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_audio_utils.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.audio_utils'`

- [ ] **Step 3: Implement the module**

Approach, in priority order:

- `detect_backend()` imports `soundfile` in a `try`, then `pydub` (+ `shutil.which("ffmpeg")` must be truthy), else `"stdlib"`.
- `make_tone_wav` uses **only** `wave` + `math.sin` + `struct.pack`, mono 16-bit — so tests never depend on a backend.
- `probe`: `soundfile.info(path)` → duration/rate/channels. Fallback: for `wav`, `wave.open`. Fallback: naive MP3 duration from the first frame header's bitrate over file size. Every failure path is swallowed and yields `None` fields (test 3).
- `materialise_audio`: `data:` prefix → split on `,`; otherwise if `Path(value).is_file()` → return it; otherwise `base64.b64decode(value, validate=True)`; on `binascii.Error`/`ValueError` and not a path → raise `ValueError("audio_path 既不是存在的文件，也不是合法的 base64 音频数据")`. Decoded bytes get a magic-number sniff for the extension (`RIFF`→wav, `ID3`/`\xff\xfb`→mp3, `ftyp`→m4a, `fLaC`→flac, `OggS`→ogg).
- `validate_reference` order: exists → size → extension whitelist → magic number agreement → duration (if known) bounds. Missing/unknown duration → `SUCCESS` **with a warning** (test 12), never a failure.
- `new_output_path`: `qwen_tts_{UTC:%Y%m%d_%H%M%S}_{secrets.token_hex(2)}.{fmt}`, `mkdir(parents=True, exist_ok=True)`.
- `cleanup_temp_files`: delete regular files older than `max_age_hours` by `st_mtime`, return count.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_audio_utils.py -q`
Expected: PASS (19 passed)

- [ ] **Step 5: Verify the real backend is the one detected**

Run: `D:\Conda_envs\mcp-dev\python.exe -c "from qwen_voice_mcp.core.audio_utils import detect_backend; print(detect_backend())"`
Expected: `soundfile`

---

### Task 6: Balance checker with endpoint probing and threshold tiers

**Files:**
- Create: `qwen_voice_mcp/core/balance_checker.py`
- Test: `tests/test_balance_checker.py`

**Interfaces:**
- Consumes: `config.BALANCE_ENDPOINTS`, `config.RECHARGE_URL`; `core.models.BalanceState`, `Status`, `ToolResult`.
- Produces:
  - `core.balance_checker.BalanceChecker(api_key: str | None, *, endpoints=None, timeout=8.0, threshold=1.0, client: httpx.Client | None = None)` with `query() -> BalanceState`, `as_tool_result() -> ToolResult`, `gate(estimated_cost: float, strict: bool) -> ToolResult | None` (returns `None` to allow, else the blocking `ToolResult`), and `warn_if_tight(estimated_cost: float) -> list[str]`.
  - Module-level `extract_balance(payload: dict) -> tuple[float | None, str | None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_balance_checker.py
import httpx
import pytest
from qwen_voice_mcp.core.balance_checker import BalanceChecker, extract_balance
from qwen_voice_mcp.core.models import Status

ENDPOINTS = ["https://x.test/a", "https://x.test/b"]


def make(handler, **kw):
    return BalanceChecker("sk-test", endpoints=ENDPOINTS, timeout=1.0,
                          client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_extract_balance_reads_top_level_field():
    assert extract_balance({"balance": "12.50", "currency": "CNY"}) == (12.5, "CNY")


@pytest.mark.parametrize("key", ["total_balance", "available_balance", "remaining", "amount"])
def test_extract_balance_reads_alternate_field_names(key):
    assert extract_balance({key: 3})[0] == 3.0


def test_extract_balance_reads_nested_data():
    assert extract_balance({"data": {"balance": 7}})[0] == 7.0


def test_extract_balance_unrecognised_shape_returns_none():
    assert extract_balance({"unexpected": "shape"}) == (None, None)


def test_extract_balance_never_coerces_garbage_to_zero():
    assert extract_balance({"balance": "not-a-number"})[0] is None


def test_first_endpoint_success_is_used():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"balance": 12.5, "currency": "CNY"})

    state = make(handler).query()
    assert state.balance == 12.5 and state.currency == "CNY"
    assert len(seen) == 1


def test_falls_back_to_second_endpoint_on_http_error():
    def handler(req):
        if str(req.url).endswith("/a"):
            return httpx.Response(500)
        return httpx.Response(200, json={"balance": 5})

    assert make(handler).query().balance == 5.0


def test_falls_back_when_first_returns_unparsable_shape():
    def handler(req):
        if str(req.url).endswith("/a"):
            return httpx.Response(200, json={"unexpected": 1})
        return httpx.Response(200, json={"balance": 5})

    assert make(handler).query().balance == 5.0


def test_all_endpoints_failing_yields_unknown_with_last_error():
    def handler(req):
        raise httpx.ConnectError("boom")

    state = make(handler).query()
    assert state.balance is None and state.source is None and state.last_error


def test_all_endpoints_bad_shape_yields_unknown_not_zero():
    def handler(req):
        return httpx.Response(200, json={"unexpected": 1})

    assert make(handler).query().balance is None


def test_missing_api_key_does_not_call_network():
    def handler(req):  # pragma: no cover
        raise AssertionError("must not be called")

    state = BalanceChecker(None, endpoints=ENDPOINTS,
                           client=httpx.Client(transport=httpx.MockTransport(handler))).query()
    assert state.balance is None and state.last_error


def test_gate_blocks_when_balance_at_or_below_threshold():
    b = make(lambda r: httpx.Response(200, json={"balance": 1.0}), threshold=1.0)
    r = b.gate(estimated_cost=0.1, strict=False)
    assert r is not None and r.status is Status.INSUFFICIENT_BALANCE
    assert r.data["current_balance"] == 1.0 and r.data["threshold"] == 1.0


def test_gate_allows_when_balance_well_above_threshold_plus_cost():
    b = make(lambda r: httpx.Response(200, json={"balance": 12.5}), threshold=1.0)
    assert b.gate(estimated_cost=0.1, strict=False) is None
    assert b.warn_if_tight(0.1) == []


def test_gate_allows_but_warns_in_tight_band():
    b = make(lambda r: httpx.Response(200, json={"balance": 1.05}), threshold=1.0)
    assert b.gate(estimated_cost=0.1, strict=False) is None
    assert b.warn_if_tight(0.1)


def test_gate_blocks_unknown_balance_only_in_strict_mode():
    b = make(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert b.gate(0.1, strict=True).status is Status.BALANCE_UNAVAILABLE
    r = b.gate(0.1, strict=False)
    assert r is None and b.warn_if_tight(0.1)


def test_as_tool_result_success_shape():
    b = make(lambda r: httpx.Response(200, json={"balance": 12.5, "currency": "CNY"}), threshold=1.0)
    d = b.as_tool_result().to_dict()
    assert d["status"] == "success" and d["balance"] == 12.5
    assert d["is_sufficient"] is True and d["threshold"] == 1.0


def test_as_tool_result_unknown_shape_is_success_with_warning():
    b = make(lambda r: httpx.Response(500))
    d = b.as_tool_result().to_dict()
    assert d["status"] == "success" and d["balance"] is None
    assert d["is_sufficient"] is None and d["warnings"]


def test_insufficient_balance_message_contains_recharge_advice():
    b = make(lambda r: httpx.Response(200, json={"balance": 0.5}), threshold=1.0)
    msg = b.gate(0.11, strict=False).message
    assert "充值" in msg and "0.5" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_balance_checker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.balance_checker'`

- [ ] **Step 3: Implement the checker**

Approach: `query()` iterates endpoints, GET with `Authorization: Bearer <key>` and the configured timeout; treats non-2xx, transport errors, and `extract_balance` returning `None` all as "try next"; caches the last `BalanceState` for one call so `gate` and `warn_if_tight` share one network round trip (re-query only if no cache). `extract_balance` walks the candidate field names with `float()` inside `try`, and for values like `"¥12.50"` strips non-numeric prefixes; `"not-a-number"` → `None` (test 5) — **never** coerced to `0.0`, because that would block every call. `gate` returns `None` when allowed, otherwise a `ToolResult` with `status=INSUFFICIENT_BALANCE`/`BALANCE_UNAVAILABLE` and `data` carrying `current_balance`, `threshold`, `estimated_cost`, `recharge_url`. `as_tool_result` always returns `Status.SUCCESS` (it is a read-only tool) with `is_sufficient` possibly `None` and a warning when unknown.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_balance_checker.py -q`
Expected: PASS (21 passed)

---

### Task 7: DashScope client wrapper and error classification

**Files:**
- Create: `qwen_voice_mcp/core/errors.py`
- Create: `qwen_voice_mcp/core/qwen_client.py`
- Test: `tests/test_qwen_client.py`

**Interfaces:**
- Consumes: `core.model_registry.ModelRegistry`; `config.DASHSCOPE_*`; `dashscope` SDK.
- Produces:
  - `core.errors.classify(exc: BaseException) -> tuple[Status, str]` and `core.errors.safe_message(exc: BaseException) -> str` (always `redact`-ed).
  - `core.qwen_client.QwenClient(api_key: str | None, registry: ModelRegistry, *, timeout: float = 60.0)` with `resolve_api_key() -> str | None`, `synthesize(text, model, voice, output_format, output_path) -> ToolResult`, `upload_reference(path) -> str`, `clone_voice(url, target_model, prefix, language_hints=None) -> ToolResult`, `query_voice(voice_id) -> dict | None`, `verify_model(model) -> bool`.
  - `core.qwen_client.make_voice_prefix(voice_name: str) -> str` — **must** return a `<10`-char lowercase-alphanumeric string.
  - `core.qwen_client.FORMAT_MAP: dict[str, object]` mapping `"mp3"|"wav"|"pcm"|"opus"` to `dashscope.audio.tts_v2.AudioFormat` members.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qwen_client.py
import re
import pytest
from pathlib import Path
from qwen_voice_mcp.core import qwen_client as qc
from qwen_voice_mcp.core.errors import classify
from qwen_voice_mcp.core.model_registry import ModelRegistry
from qwen_voice_mcp.core.models import Status


@pytest.mark.parametrize("name", [
    "用户自定义音色", "My Voice", "UPPER", "a" * 40, "音色-2026", "", "  ", "voice_with_underscore",
])
def test_make_voice_prefix_is_always_legal(name):
    p = qc.make_voice_prefix(name)
    assert re.fullmatch(r"[a-z0-9]+", p), p
    assert len(p) < 10, p


def test_make_voice_prefix_is_deterministic_for_same_name():
    assert qc.make_voice_prefix("Long Xiao Chun") == qc.make_voice_prefix("Long Xiao Chun")


def test_make_voice_prefix_differs_across_similar_names():
    assert qc.make_voice_prefix("voice one") != qc.make_voice_prefix("voice two")


def test_format_map_covers_documented_formats():
    for fmt in ("mp3", "wav", "pcm", "opus"):
        assert fmt in qc.FORMAT_MAP


def test_unknown_output_format_is_invalid_input(tmp_path):
    c = qc.QwenClient("sk-x", ModelRegistry())
    r = c.synthesize("hi", "cosyvoice-v2", "longanyang", "zzz", tmp_path / "o.zzz")
    assert r.status is Status.INVALID_INPUT


def test_classify_recognises_auth_error_by_status_code():
    class E(Exception):
        status_code = 401
    assert classify(E("nope"))[0] is Status.AUTH_ERROR


def test_classify_recognises_rate_limit():
    class E(Exception):
        status_code = 429
    assert classify(E("slow down"))[0] is Status.RATE_LIMITED


def test_classify_recognises_timeout():
    import httpx
    assert classify(httpx.ConnectTimeout("t"))[0] is Status.NETWORK_ERROR


def test_classify_unknown_exception_is_upstream_error():
    assert classify(ValueError("???"))[0] is Status.UPSTREAM_ERROR


def test_classify_never_leaks_the_api_key():
    key = "sk-secretsecretsecret1234567890"
    status, msg = classify(RuntimeError(f"failed with {key}"))
    assert key not in msg


def test_synthesize_success_writes_file_and_reports_duration(tmp_path, monkeypatch):
    class FakeSynth:
        def __init__(self, **kw): self.kw = kw
        def call(self, text, timeout_millis=None): return b"\xff\xfb\x90\x00" + b"\x00" * 4000
    monkeypatch.setattr(qc, "SpeechSynthesizer", FakeSynth, raising=False)
    monkeypatch.setattr(qc, "_audio_format_for", lambda fmt: "MP3", raising=False)
    out = tmp_path / "o.mp3"
    r = qc.QwenClient("sk-x", ModelRegistry()).synthesize("你好", "cosyvoice-v2", "longanyang", "mp3", out)
    assert r.status is Status.SUCCESS
    assert out.exists() and r.data["audio_path"] == str(out)
    assert r.data["text_length"] == 2 and r.data["model_used"] == "cosyvoice-v2"


def test_synthesize_empty_result_is_upstream_error(tmp_path, monkeypatch):
    class FakeSynth:
        def __init__(self, **kw): pass
        def call(self, text, timeout_millis=None): return b""
    monkeypatch.setattr(qc, "SpeechSynthesizer", FakeSynth, raising=False)
    monkeypatch.setattr(qc, "_audio_format_for", lambda fmt: "MP3", raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).synthesize("hi", "cosyvoice-v2", "v", "mp3", tmp_path / "o.mp3")
    assert r.status is not Status.SUCCESS


def test_synthesize_maps_sdk_exception(tmp_path, monkeypatch):
    class Boom(Exception):
        status_code = 429
    class FakeSynth:
        def __init__(self, **kw): pass
        def call(self, text, timeout_millis=None): raise Boom("throttled")
    monkeypatch.setattr(qc, "SpeechSynthesizer", FakeSynth, raising=False)
    monkeypatch.setattr(qc, "_audio_format_for", lambda fmt: "MP3", raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).synthesize("hi", "cosyvoice-v2", "v", "mp3", tmp_path / "o.mp3")
    assert r.status is Status.RATE_LIMITED


def test_missing_api_key_reports_auth_error(tmp_path):
    r = qc.QwenClient(None, ModelRegistry()).synthesize("hi", "cosyvoice-v2", "v", "mp3", tmp_path / "o.mp3")
    assert r.status is Status.AUTH_ERROR


def test_resolve_api_key_prefers_explicit_then_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    assert qc.QwenClient("sk-arg", ModelRegistry()).resolve_api_key() == "sk-arg"
    assert qc.QwenClient(None, ModelRegistry()).resolve_api_key() == "sk-env"


def test_clone_voice_success_shape(tmp_path, monkeypatch):
    class FakeVES:
        def __init__(self, **kw): pass
        def create_voice(self, target_model, prefix, url, language_hints=None, **kw): return "voice-abc123"
        def query_voice(self, voice_id): return ["OK"]
    monkeypatch.setattr(qc, "VoiceEnrollmentService", FakeVES, raising=False)
    monkeypatch.setattr(qc.QwenClient, "upload_reference", lambda self, p: "https://f/x.wav", raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).clone_voice("https://f/x.wav", "cosyvoice-v2", "qwenvoic")
    assert r.status is Status.SUCCESS
    assert r.data["voice_id"] == "voice-abc123" and r.data["cloning_status"] == "completed"


def test_clone_voice_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    class Boom(Exception):
        status_code = 403
    class FakeVES:
        def __init__(self, **kw): pass
        def create_voice(self, *a, **k): raise Boom("denied")
    monkeypatch.setattr(qc, "VoiceEnrollmentService", FakeVES, raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).clone_voice("https://f/x.wav", "cosyvoice-v2", "qwenvoic")
    assert r.status is Status.AUTH_ERROR


def test_clone_voice_pending_status_is_reported_honestly(monkeypatch):
    class FakeVES:
        def __init__(self, **kw): pass
        def create_voice(self, *a, **k): return "voice-pending"
        def query_voice(self, voice_id): raise RuntimeError("still processing")
    monkeypatch.setattr(qc, "VoiceEnrollmentService", FakeVES, raising=False)
    monkeypatch.setattr(qc, "ENROLL_POLL_INTERVAL_SECONDS", 0, raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).clone_voice("https://f/x.wav", "cosyvoice-v2", "qwenvoic")
    assert r.data.get("cloning_status") in {"pending", "processing", "unknown"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_qwen_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.errors'`

- [ ] **Step 3: Implement `errors.py`**

`classify` inspects, in order: `dashscope` `VoiceEnrollmentException`/`DashScopeAPIError`-style attributes (`status_code`, `code`), `httpx.TimeoutException`/`ConnectError` → `NETWORK_ERROR`, `httpx.HTTPStatusError` → by status, `OSError` → `NETWORK_ERROR`, then a `code`/message keyword scan (`InvalidApiKey`, `AccessDenied`, `Unauthorized` → `AUTH_ERROR`; `Throttling`, `RateLimit` → `RATE_LIMITED`; `ModelNotExist`, `InvalidParameter`, `DataInspectionFailed` → `INVALID_INPUT`; else `UPSTREAM_ERROR`). Codes: 401/403 → `AUTH_ERROR`, 429 → `RATE_LIMITED`, 400/422 → `INVALID_INPUT`, 5xx → `UPSTREAM_ERROR`. `safe_message` runs the text through `SecretManager.redact`-equivalent masking **and** scrubs any `sk-[A-Za-z0-9]{8,}` occurrence, so a key embedded in an SDK message can never surface.

- [ ] **Step 4: Implement `qwen_client.py`**

Import `dashscope` lazily inside a module-level `try` so that `import qwen_voice_mcp.core.qwen_client` never hard-fails, binding `SpeechSynthesizer`, `VoiceEnrollmentService`, `AudioFormat`, and `Files` to module globals (tests monkeypatch these exact names). `make_voice_prefix`: `re.sub(r"[^a-z0-9]", "", voice_name.lower())[:6]`, then append `hashlib.sha256(voice_name.encode()).hexdigest()[:3]`; if the result is empty or `< 3` chars, use `"qv" + sha256[:7]`; always truncate to 9. `synthesize`: guard unknown format → `INVALID_INPUT`; guard empty text → `INVALID_INPUT`; no key → `AUTH_ERROR`; call `SpeechSynthesizer(model=model, voice=voice, format=fmt).call(text, timeout_millis=int(timeout*1000))`; empty/`None` audio → `UPSTREAM_ERROR`; write bytes, `probe()` for duration, return the spec §6.3 success dict. `clone_voice`: `create_voice(...)` → `voice_id`, then up to 3 `query_voice` polls with `ENROLL_POLL_INTERVAL_SECONDS = 1.0`; a failing poll yields `cloning_status` `"pending"`/`"unknown"` **with** `Status.SUCCESS` only when a `voice_id` exists — an exception from `create_voice` maps through `classify` and is never reported as success.

- [ ] **Step 5: Run test to verify it passes**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_qwen_client.py -q`
Expected: PASS (22 passed)

---

### Task 8: Service container, balance gate, and the four MCP tools

**Files:**
- Create: `qwen_voice_mcp/core/services.py`
- Create: `qwen_voice_mcp/tools/__init__.py`
- Create: `qwen_voice_mcp/tools/list_models.py`
- Create: `qwen_voice_mcp/tools/tts.py`
- Create: `qwen_voice_mcp/tools/voice_clone.py`
- Create: `qwen_voice_mcp/tools/balance.py`
- Create: `qwen_voice_mcp/server.py`
- Create: `server.py` (root shim)
- Test: `tests/test_services.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces:
  - `core.services.VoiceServices(options, registry=None, secret_manager=None, client=None, checker=None)` with `list_voice_models() -> ToolResult`, `synthesize_speech(text, model=None, voice=None, output_format=None) -> ToolResult`, `clone_voice(audio_path, voice_name, model=None, language_hints=None) -> ToolResult`, `check_balance() -> ToolResult`, and `ensure_balance(estimated_cost, strict) -> ToolResult | None`.
  - `tools.register_all(mcp: FastMCP, services: VoiceServices) -> None`
  - `qwen_voice_mcp.server.build_server(options: Options | None = None) -> FastMCP`
  - Root `server.py`: `main()` parsing `--transport/--port/--host/--selftest`, calling `qwen_voice_mcp.cli`-equivalent behaviour so `python server.py --transport stdio` works.

- [ ] **Step 1: Write the failing test for the gate and tools**

```python
# tests/test_services.py
import pytest
from qwen_voice_mcp.core import services as sv
from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.config_store import load_options


class FakeChecker:
    def __init__(self, block=None, warnings=None):
        self.block, self.warnings = block, warnings or []
    def gate(self, estimated_cost, strict): return self.block
    def warn_if_tight(self, estimated_cost): return list(self.warnings)


class FakeClient:
    def __init__(self, result=None, clone=None):
        self.calls = []
        self.result = result or ToolResult(Status.SUCCESS, "合成完成", {"audio_path": "x.mp3"})
        self.clone = clone or ToolResult(Status.SUCCESS, "克隆完成", {"voice_id": "v1"})
        self.uploaded = []
    def synthesize(self, text, model, voice, fmt, path):
        self.calls.append(("synthesize", text, model, voice, fmt))
        return self.result
    def clone_voice(self, url, target_model, prefix, language_hints=None):
        self.calls.append(("clone", url, target_model, prefix, language_hints))
        return self.clone
    def upload_reference(self, path):
        self.uploaded.append(path)
        return "https://f/ref.wav"


def build(config_home, checker=None, client=None):
    return sv.VoiceServices(load_options(), client=client or FakeClient(),
                            checker=checker or FakeChecker(), secret_manager=None)


def test_billable_call_is_blocked_without_touching_the_api(config_home):
    blk = ToolResult(Status.INSUFFICIENT_BALANCE, "余额不足")
    client = FakeClient()
    r = build(config_home, FakeChecker(block=blk), client).synthesize_speech("你好")
    assert r.status is Status.INSUFFICIENT_BALANCE
    assert client.calls == [], "the API must not be called when the gate blocks"


def test_clone_is_blocked_without_uploading(config_home, tmp_path):
    from qwen_voice_mcp.core import audio_utils as au
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    blk = ToolResult(Status.INSUFFICIENT_BALANCE, "余额不足")
    client = FakeClient()
    r = build(config_home, FakeChecker(block=blk), client).clone_voice(str(wav), "音色一")
    assert r.status is Status.INSUFFICIENT_BALANCE
    assert client.uploaded == []


def test_empty_text_is_invalid_input_and_never_bills(config_home):
    client = FakeClient()
    for bad in ("", "   ", "\n\t"):
        r = build(config_home, FakeChecker(), client).synthesize_speech(bad)
        assert r.status is Status.INVALID_INPUT
    assert client.calls == []


def test_unknown_model_is_model_not_supported(config_home):
    r = build(config_home).synthesize_speech("hi", model="does-not-exist")
    assert r.status is Status.MODEL_NOT_SUPPORTED


def test_warnings_from_checker_are_attached(config_home):
    r = build(config_home, FakeChecker(warnings=["余额偏低"])).synthesize_speech("你好")
    assert r.status is Status.SUCCESS and "余额偏低" in r.warnings


def test_clone_rejects_illegal_audio_before_upload(config_home, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"nope" * 100)
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(str(bad), "音色一")
    assert r.status is Status.INVALID_INPUT
    assert client.uploaded == []


def test_clone_accepts_base64_input(config_home, tmp_path):
    import base64
    from qwen_voice_mcp.core import audio_utils as au
    raw = au.make_tone_wav(tmp_path / "s.wav", seconds=10).read_bytes()
    r = build(config_home).clone_voice(base64.b64encode(raw).decode(), "音色一")
    assert r.status is Status.SUCCESS


def test_list_models_reports_declared_not_verified(config_home):
    d = build(config_home).list_voice_models().to_dict()
    assert d["status"] == "success" and d["count"] == len(d["models"])
    assert all(m["verified"] is None for m in d["models"])
    assert d["default_model"]


def test_check_balance_is_read_only_success(config_home):
    class C:
        def as_tool_result(self): return ToolResult(Status.SUCCESS, "余额 ¥12.50", {"balance": 12.5})
    s = sv.VoiceServices(load_options(), client=FakeClient(), checker=C(), secret_manager=None)
    assert s.check_balance().status is Status.SUCCESS


def test_internal_exception_becomes_internal_error_not_a_crash(config_home):
    class Boom(FakeClient):
        def synthesize(self, *a, **k): raise RuntimeError("kaboom")
    r = build(config_home, client=Boom()).synthesize_speech("你好")
    assert r.status is Status.INTERNAL_ERROR
    assert "kaboom" not in r.message
```

```python
# tests/test_mcp_tools.py
import pytest
from qwen_voice_mcp.core.config_store import load_options
from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.services import VoiceServices
from qwen_voice_mcp.server import build_server

EXPECTED = {"list_voice_models", "synthesize_speech", "clone_voice", "check_balance"}


class FakeChecker:
    def gate(self, estimated_cost, strict): return None
    def warn_if_tight(self, estimated_cost): return []
    def as_tool_result(self): return ToolResult(Status.SUCCESS, "余额 ¥12.50", {"balance": 12.5})


class FakeClient:
    def synthesize(self, text, model, voice, fmt, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfb\x90\x00")
        return ToolResult(Status.SUCCESS, "合成完成",
                          {"audio_path": str(path), "audio_format": fmt, "model_used": model,
                           "voice_used": voice, "text_length": len(text), "duration_seconds": 1.0})
    def clone_voice(self, url, target_model, prefix, language_hints=None):
        return ToolResult(Status.SUCCESS, "克隆完成",
                          {"voice_id": "v1", "voice_prefix": prefix, "cloning_status": "completed"})
    def upload_reference(self, path): return "https://f/ref.wav"


@pytest.fixture
def server(config_home):
    return build_server(load_options())


@pytest.mark.anyio
async def test_all_four_tools_are_exposed(server):
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        tools = await session.list_tools()
    assert {t.name for t in tools.tools} == EXPECTED


@pytest.mark.anyio
async def test_synthesize_requires_text_argument(server):
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert "text" in tools["synthesize_speech"].inputSchema["required"]
    assert set(tools["clone_voice"].inputSchema["required"]) == {"audio_path", "voice_name"}


@pytest.mark.anyio
async def test_calling_list_voice_models_returns_models(server):
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        res = await session.call_tool("list_voice_models", {})
    assert res.isError is False
    assert "models" in res.content[0].text
```

Add a `tests/conftest.py` fixture so the async tests run without extra plugins:

```python
@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_services.py tests/test_mcp_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.core.services'`

- [ ] **Step 3: Implement `core/services.py`**

Constructor builds `ModelRegistry()`, `SecretManager()` (if `secret_manager` is None, then `load_into_env()`), `QwenClient(resolved_key, registry, timeout=...)`, `BalanceChecker(...)` — each parameter injectable so tests never hit the network.

`ensure_balance(estimated_cost, strict)` → `self._checker.gate(...)`. `synthesize_speech` order, which is the spec's hard constraint:
1. `text.strip()` empty → `INVALID_INPUT` (**before** any balance work or billing)
2. resolve model (`model or default`) — unknown → `MODEL_NOT_SUPPORTED`
3. `estimated = registry.estimated_cost(model, len(text))`
4. `blocked = ensure_balance(estimated, options.strict_balance)` → return it if not `None`
5. `path = audio_utils.new_output_path(options.output_dir, output_format)`
6. `result = client.synthesize(...)`; attach `warnings` from `warn_if_tight`; add `estimated_cost` to `data` on success

`clone_voice` order: `materialise_audio` → `validate_reference` → `ensure_balance(registry.clone_cost(), ...)` → `upload_reference` → `client.clone_voice(...)`. Wrap the whole body in `try/except Exception` returning `ToolResult(Status.INTERNAL_ERROR, "内部错误，请查看服务端日志")` — the exception detail goes to `logging` (stderr), never into `message` (last test in `test_services.py`).

- [ ] **Step 4: Implement the tools and server**

Each tool module exposes one function taking `services` plus the tool arguments, returning `ToolResult.to_dict()`. `tools/__init__.py::register_all` decorates four closures with `@mcp.tool(name=..., description=...)`, using the doc's Chinese trigger descriptions. `server.build_server` constructs `FastMCP(name="qwen-voice-mcp", instructions=..., host=..., port=...)` (**no** `version=`), calls `register_all`, and returns it. Root `server.py` is a thin `argparse` main delegating to `qwen_voice_mcp.cli.main`, so `--transport`/`--port`/`--selftest` all work from the documented command line.

- [ ] **Step 5: Run tests to verify they pass**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_services.py tests/test_mcp_tools.py -q`
Expected: PASS (14 passed)

- [ ] **Step 6: Verify documented command line works**

Run: `D:\Conda_envs\mcp-dev\python.exe server.py --selftest`
Expected: exit code 0 with `SELFTEST OK` and the four tool names printed.

---

### Task 9: CLI, Tkinter config center, docs, and end-to-end verification

**Files:**
- Create: `qwen_voice_mcp/cli.py`
- Create: `qwen_voice_mcp/__main__.py`
- Create: `qwen_voice_mcp/gui/__init__.py`
- Create: `qwen_voice_mcp/gui/config_window.py`
- Create: `README.md`
- Create: `.env.example`
- Create: `docs/integration-guide.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: `qwen_voice_mcp.cli.main(argv=None) -> int`; console entry point `qwen-voice-mcp`; `gui.config_window.launch() -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from qwen_voice_mcp.cli import main


def test_selftest_exits_zero(config_home, capsys):
    assert main(["--selftest"]) == 0
    out = capsys.readouterr().out
    for name in ("list_voice_models", "synthesize_speech", "clone_voice", "check_balance"):
        assert name in out


def test_config_show_prints_effective_options(config_home, capsys):
    assert main(["config", "show"]) == 0
    assert "127.0.0.1" in capsys.readouterr().out


def test_config_set_then_show_roundtrip(config_home, capsys):
    assert main(["config", "set", "default_voice", "longxiaochun"]) == 0
    main(["config", "show"])
    assert "longxiaochun" in capsys.readouterr().out


def test_balance_without_key_reports_unavailable(config_home, capsys):
    assert main(["balance"]) == 0
    assert "余额" in capsys.readouterr().out


def test_verify_models_offline_reports_unverified(config_home, capsys):
    assert main(["verify-models"]) == 0
    assert "verified" in capsys.readouterr().out


def test_gui_import_does_not_require_a_display():
    from qwen_voice_mcp.gui import config_window
    assert callable(config_window.launch)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest tests/test_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qwen_voice_mcp.cli'`

- [ ] **Step 3: Implement the CLI**

`main` builds a `typer` app but is also callable with a raw `argv` list (tests call `main([...])` directly, so `main` must accept a list and route to `typer.main.get_command(app)(args, standalone_mode=False)`, returning `0`). Commands: `run`, `gui`, `selftest`, `verify-models`, `balance`, and a `config` group (`show`/`set`/`set-key`/`clear-key`). `--selftest` prints `SELFTEST OK` plus the four tool names and **must not** touch the network: it builds the server with a fake checker/client, lists tools via the in-memory session, and prints the names. `verify-models` with no key marks every model `verified=False` and prints a table (`rich`), never claiming verification it did not do.

- [ ] **Step 4: Implement the Tkinter GUI**

`config_window.launch()` builds the layout from spec §13. `import tkinter` stays **inside** `launch()` (test 6 imports only the module). API-key entry uses `show="•"` with a toggle; buttons wire to `SecretManager.store` / `BalanceChecker.query` / `ModelRegistry` refresh. Long operations run in a `threading.Thread` and post results through a `queue.Queue` polled by `root.after(100, ...)`. When `tkinter.TclError`/`ImportError` occurs (headless), print a CLI alternative and return non-zero.

- [ ] **Step 5: Write README and integration guide**

`README.md`: what it is, the four tools table, install, `python server.py --transport stdio`, DSH `cordis.patch.yml` examples for stdio and streamable-http, GUI usage, security notes, and a **「与说明书的偏差」** table reproducing spec §3 (D1–D8) verbatim in substance. `docs/integration-guide.md`: step-by-step DSH 设置 → MCP → 添加服务器, tool-mapping table from the doc §6.2, troubleshooting (auth_error / insufficient_balance / model_not_supported), and the `--verify-models` workflow. `.env.example` lists `DASHSCOPE_API_KEY=`, `QWEN_VOICE_HOME=`, `QWEN_VOICE_DEFAULT_VOICE=longanyang`, `QWEN_VOICE_BALANCE_THRESHOLD=1.00`, `QWEN_VOICE_STRICT_BALANCE=false`, `QWEN_VOICE_HTTP_AUTH_TOKEN=`.

- [ ] **Step 6: Run the full suite**

Run: `D:\Conda_envs\mcp-dev\python.exe -m pytest -q`
Expected: PASS, 0 failures, all tasks' tests green together.

- [ ] **Step 7: End-to-end verification (the completion gate)**

Run each and record real output before claiming completion:

```powershell
D:\Conda_envs\mcp-dev\python.exe -m pytest -q
D:\Conda_envs\mcp-dev\python.exe server.py --selftest
D:\Conda_envs\mcp-dev\python.exe -m qwen_voice_mcp --selftest
D:\Conda_envs\mcp-dev\python.exe -c "import os,asyncio;from qwen_voice_mcp.server import build_server;from mcp.shared.memory import create_connected_server_and_client_session as c;print('tool discovery OK')"
```

Expected: pytest reports all passed; both selftests print `SELFTEST OK` and the four tool names; exit codes all 0. Do **not** claim completion if any of these fail.

---

## Notes for the executor

- **VERIFIED (2026-10-14):** `mcp.shared.memory.create_connected_server_and_client_session` accepts a `FastMCP` instance directly — pass `server`, not `server._mcp_server`. Both work in this `mcp` 1.30 build; prefer the documented `server` form.
- **VERIFIED:** `FastMCP.tool` signature is `(name=None, title=None, description=None, annotations=None, icons=None, meta=None, structured_output=None)`.
- **VERIFIED:** `anyio`'s pytest plugin is registered (`pytest11` entry point), so the `anyio_backend` fixture and `@pytest.mark.anyio` work without extra installs.
- **ENVIRONMENT FIX ALREADY LANDED (2026-10-14):** every test using `tmp_path` used to fail in fixture setup with `PermissionError: [WinError 5]`. Root cause: pytest creates temp dirs via `os.mkdir(path, 0o700)` (`_pytest.tmpdir.TempPathFactory.getbasetemp` and `mktemp` → `make_numbered_dir(mode=0o700)`), and on Windows passing `mode` makes CPython set an **explicit protected DACL** instead of inheriting — so the sandbox token is not in the DACL of the directory it just created, and `listdir`/`write`/`rmtree` all fail. `tests/conftest.py` now patches `os.mkdir` on Windows to drop the `mode` argument, and points `TEMP`/`TMP`/`TMPDIR` at a workspace-local `.pytest_tmp`. Do **not** revert this, and do not add per-test temp workarounds. If you see a `PermissionError` on a *stale* `.pytest_tmp` subtree, delete `.pytest_tmp` and re-run; leftover directories created by the old broken behaviour carry the protected DACL.
- Run tests with the plain command from the plan; no `PYTEST_PLUGINS` shim or extra env vars are needed.
- Async tests use `pytest-anyio`'s `anyio_backend` fixture; `anyio` is already installed. Do not add `pytest-asyncio`.
- No task may call the real DashScope API. If a test would need a real key, the test is wrong.
