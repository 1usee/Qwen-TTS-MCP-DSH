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


# ---------------------------------------------------------------------------
# KEK fallback chain
#
# keyring only exists on desktops: Linux/WSL/Docker/CI fall back to keyring's
# fail backend, where ``set_password`` raises. Before the key-file fallback, that
# made ``qwen-voice-mcp setup`` impossible off Windows/macOS — the one thing a
# first-time user must do.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_keyring(monkeypatch):
    """Make every keyring operation fail, as an unavailable backend does."""
    import keyring

    def boom(*args, **kwargs):
        raise RuntimeError("no usable keyring backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "set_password", boom)
    return boom


@pytest.fixture
def memory_keyring(monkeypatch):
    """A working credential store backed by a dict."""
    import keyring

    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(
        keyring, "set_password", lambda s, u, v: store.__setitem__((s, u), v)
    )
    return store


def test_store_succeeds_without_a_keyring_backend(tmp_path, no_keyring):
    """A headless machine must still be able to save the API key."""
    sm = SecretManager(path=tmp_path / "vault.enc")
    sm.store("DASHSCOPE_API_KEY", KEY)

    assert sm.retrieve("DASHSCOPE_API_KEY") == KEY
    blob = json.loads(sm.path.read_text(encoding="utf-8"))
    assert blob["mode"] == "keyfile"
    assert (tmp_path / "vault.enc.key").exists()


def test_keyfile_mode_never_puts_the_secret_in_either_file(tmp_path, no_keyring):
    SecretManager(path=tmp_path / "vault.enc").store("k", KEY)
    assert KEY.encode() not in (tmp_path / "vault.enc").read_bytes()
    assert KEY.encode() not in (tmp_path / "vault.enc.key").read_bytes()


def test_keyfile_vault_reopens_from_a_fresh_instance(tmp_path, no_keyring):
    SecretManager(path=tmp_path / "vault.enc").store("k", KEY)
    assert SecretManager(path=tmp_path / "vault.enc").retrieve("k") == KEY


def test_keyring_mode_is_preferred_when_a_backend_works(tmp_path, memory_keyring):
    sm = SecretManager(path=tmp_path / "vault.enc")
    sm.store("k", KEY)

    assert sm.retrieve("k") == KEY
    blob = json.loads(sm.path.read_text(encoding="utf-8"))
    assert blob["mode"] == "keyring"
    assert not (tmp_path / "vault.enc.key").exists()


def test_explicit_passphrase_beats_the_keyfile_fallback(tmp_path, no_keyring):
    """A passphrase the user actually typed must decide the mode, not the fallback."""
    sm = SecretManager(path=tmp_path / "vault.enc", passphrase="pw")
    sm.store("k", KEY)

    blob = json.loads(sm.path.read_text(encoding="utf-8"))
    assert blob["mode"] == "passphrase"


def test_default_vault_path_defers_to_config_home(monkeypatch, tmp_path):
    """config.json and vault.enc must not drift into different directories.

    The Windows branch happens to agree today; the non-Windows branch did not
    (``~/.config`` here vs ``XDG_CONFIG_HOME`` there). Pinning the delegation
    keeps a single source of truth for the config home.
    """
    from qwen_voice_mcp.core import secret_manager as sm_mod

    shared = tmp_path / "shared-home"
    monkeypatch.delenv("QWEN_VOICE_VAULT", raising=False)
    monkeypatch.setattr(sm_mod, "config_home", lambda: shared, raising=False)

    assert sm_mod.default_vault_path() == shared / "vault.enc"


def test_vault_follows_qwen_voice_home(monkeypatch, tmp_path):
    from qwen_voice_mcp.core.secret_manager import default_vault_path

    home = tmp_path / "h"
    monkeypatch.setenv("QWEN_VOICE_HOME", str(home))
    monkeypatch.delenv("QWEN_VOICE_VAULT", raising=False)
    assert default_vault_path() == home / "vault.enc"


def test_default_vault_path_honours_an_explicit_override(monkeypatch, tmp_path):
    from qwen_voice_mcp.core.secret_manager import default_vault_path

    target = tmp_path / "custom" / "s.enc"
    monkeypatch.setenv("QWEN_VOICE_VAULT", str(target))
    assert default_vault_path() == target
