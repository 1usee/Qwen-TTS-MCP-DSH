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
