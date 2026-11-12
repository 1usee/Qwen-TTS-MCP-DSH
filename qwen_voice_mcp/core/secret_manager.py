"""Encrypted secret vault (spec §8, task 3).

Design
------
Two layers:

1. **KEK (key-encryption key)** — resolved in this order, first success wins:
   * preferred: 32 random bytes get-or-created in the OS credential store via
     ``keyring`` (``keyring.set_password(config.KEYRING_SERVICE,
     config.KEYRING_USERNAME, <b64>)``);
   * an explicit passphrase, stretched with
     ``PBKDF2HMAC(SHA256, length=32, salt=salt, iterations=600_000)`` — only when
     the caller actually supplied one, so a typed passphrase always decides the
     mode rather than silently losing to a fallback;
   * last resort: a ``vault.enc.key`` file next to the vault, ACL-restricted to
     the current user. keyring needs a desktop session (Secret Service / DPAPI),
     so on WSL, in Docker, and in CI every keyring call fails; without this tier
     ``setup`` could not store a key at all on those hosts. **Documented
     trade-off:** the key sits on the same disk as the ciphertext, so this tier
     protects against accidental sharing and casual inspection, not against an
     attacker who can already read the user's profile. ``doctor`` reports which
     tier is active.
2. **Vault file** — ``{"v":1,"mode":...,"salt":b64,"nonce":b64,"ct":b64}`` where
   ``ct`` is AES-256-GCM over the **whole** ``name -> value`` mapping serialised
   as one UTF-8 JSON document. Encrypting the mapping as a single blob means key
   *names* are not readable on disk either — only ``mode``/``v`` are cleartext.

Invariants enforced here (spec §8.4, §17):

* AES-256-GCM only, with a **fresh random 12-byte nonce on every write**.
* Every read *and* write happens under an exclusive
  ``filelock.FileLock(str(path) + ".lock")``, so concurrent writers cannot lose
  a key (plan "Review Focus" item 5).
* Writes are atomic: a temp file in the same directory, then ``os.replace``.
* Decryption failure (``cryptography.exceptions.InvalidTag``) raises
  :class:`VaultError` with a Chinese message that never reveals key material.
* Plaintext never reaches disk, logs, or error messages; :meth:`SecretManager.redact`
  is the masking helper the rest of the project uses for log/error output.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from filelock import FileLock

import config

from qwen_voice_mcp.core.config_store import config_home

__all__ = ["SecretManager", "VaultError", "VAULT_FORMAT_VERSION"]

#: On-disk format version written into (and expected back from) the vault file.
VAULT_FORMAT_VERSION = 1
#: KEK length in bytes (AES-256).
KEK_BYTES = 32
#: AES-GCM nonce length in bytes — regenerated for every single write.
NONCE_BYTES = 12
#: PBKDF2 salt length in bytes.
SALT_BYTES = 16
#: PBKDF2 iteration count for the passphrase-derived KEK (spec §8.1).
PBKDF2_ITERATIONS = 600_000

MODE_KEYRING = "keyring"
MODE_PASSPHRASE = "passphrase"
MODE_KEYFILE = "keyfile"

#: Suffix appended to the vault path for the local fallback KEK file.
KEYFILE_SUFFIX = ".key"


class VaultError(Exception):
    """Raised when the vault cannot be opened, decrypted, or written.

    The message is safe to log: it never contains the passphrase, the KEK, or
    any stored secret value.
    """


def default_vault_path() -> Path:
    """Resolve the vault location.

    Precedence: ``QWEN_VOICE_VAULT`` (explicit file) → ``config_home()/vault.enc``.

    Delegating to :func:`config_store.config_home` keeps ``config.json`` and the
    vault in one directory on every platform; the previous hand-rolled copy used
    ``~/.config`` where ``config_home()`` used ``XDG_CONFIG_HOME``, so the two
    files could land in different places on Linux.
    """
    override = os.environ.get("QWEN_VOICE_VAULT")
    if override:
        return Path(override).expanduser()
    return config_home() / "vault.enc"


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class SecretManager:
    """AES-256-GCM vault holding ``name -> value`` pairs.

    Parameters
    ----------
    path:
        Vault file location. Defaults to :func:`default_vault_path`.
    prefer_keyring:
        When true, try to get-or-create the KEK in the OS credential store
        first, falling back to passphrase mode on any keyring failure.
    passphrase:
        Passphrase used by (or as fallback to) passphrase mode.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        prefer_keyring: bool | None = None,
        passphrase: str | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_vault_path()
        self.prefer_keyring = (
            not os.environ.get(config.KEYRING_DISABLE_ENV)
            if prefer_keyring is None
            else prefer_keyring
        )
        self.passphrase = passphrase
        self.lock_path = str(self.path) + ".lock"
        self._lock = FileLock(self.lock_path)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def store(self, name: str, value: str) -> None:
        """Insert/overwrite one secret and rewrite the vault atomically."""
        with self._lock:
            entries, salt = self._load_locked()
            entries[name] = value
            self._save_locked(entries, salt)

    def retrieve(self, name: str) -> str | None:
        """Return the stored value, or ``None`` when the name is absent."""
        with self._lock:
            entries, _ = self._load_locked()
        return entries.get(name)

    def delete(self, name: str) -> bool:
        """Delete ``name``; return ``True`` only when something was removed."""
        with self._lock:
            entries, salt = self._load_locked()
            if name not in entries:
                return False
            del entries[name]
            if entries:
                self._save_locked(entries, salt)
            else:
                # Nothing left to hide: drop the ciphertext entirely.
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            return True

    def list_names(self) -> list[str]:
        """Sorted key names — values stay encrypted and are never returned."""
        with self._lock:
            entries, _ = self._load_locked()
        return sorted(entries)

    def load_into_env(self, name: str = config.API_KEY_ENV) -> bool:
        """Inject the vault value into ``os.environ`` unless already set.

        Environment variables win over the vault (spec §8.3), so an existing
        value is left untouched and ``False`` is returned. ``True`` means the
        vault value was injected.
        """
        if os.environ.get(name):
            return False
        value = self.retrieve(name)
        if not value:
            return False
        os.environ[name] = value
        return True

    @staticmethod
    def redact(value: str | None) -> str:
        """Mask a secret for logs/errors, keeping a short recognition hint."""
        if value is None:
            return "<unset>"
        if value == "":
            return "<empty>"
        if len(value) < 8:
            return "***"
        return f"{value[:6]}…{value[-4:]}"

    # ------------------------------------------------------------------
    # KEK resolution
    # ------------------------------------------------------------------
    def _keyring_kek(self) -> bytes | None:
        """Get-or-create the KEK in the OS credential store.

        Returns ``None`` (never raises) on *any* keyring problem, so the caller
        can fall back to passphrase mode.
        """
        try:
            import keyring  # imported lazily: the backend is probed on first use

            stored = keyring.get_password(config.KEYRING_SERVICE, config.KEYRING_USERNAME)
            if stored:
                raw = _b64d(stored)
                if len(raw) == KEK_BYTES:
                    return raw
            fresh = secrets.token_bytes(KEK_BYTES)
            keyring.set_password(config.KEYRING_SERVICE, config.KEYRING_USERNAME, _b64e(fresh))
            return fresh
        except Exception:
            return None

    def _keyfile_path(self) -> Path:
        """Location of the local fallback KEK (``vault.enc`` -> ``vault.enc.key``)."""
        return Path(str(self.path) + KEYFILE_SUFFIX)

    def _keyfile_kek(self, *, create: bool) -> bytes | None:
        """Read (or first-time create) the ACL-restricted local KEK file.

        Returns ``None`` when the file is missing and ``create`` is false, when it
        is unusable, or when the filesystem refuses the write — the caller then
        falls through to passphrase mode and its clearer error message.
        """
        path = self._keyfile_path()
        try:
            if path.exists():
                raw = _b64d(path.read_text(encoding="utf-8").strip())
                return raw if len(raw) == KEK_BYTES else None
            if not create:
                return None
            fresh = secrets.token_bytes(KEK_BYTES)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_b64e(fresh), encoding="utf-8")
            self._harden_permissions(path)
            return fresh
        except (OSError, ValueError):
            # ValueError covers base64 decoding of a corrupted key file.
            return None

    def _derive_passphrase_kek(self, salt: bytes, *, required: bool) -> bytes | None:
        if self.passphrase is None:
            if required:
                raise VaultError("vault 解密失败：该密钥库使用口令模式，但未提供口令")
            return None
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=KEK_BYTES,
            salt=salt,
            iterations=PBKDF2_ITERATIONS,
        )
        return kdf.derive(self.passphrase.encode("utf-8"))

    def _kek_for_write(
        self, salt: bytes, *, fresh_salt: bool
    ) -> tuple[bytes, str, bytes]:
        """Return ``(kek, mode, salt)`` for a brand-new or replaced vault document.

        ``salt`` is reused verbatim when the document already carries one, so a
        passphrase-mode vault keeps opening with the passphrase given for the
        original write. A brand-new document (or a passphrase-mode document
        that has no meaningful salt yet) gets a fresh random salt — and the
        returned salt is always the exact one the KEK was derived from.
        """
        if self.prefer_keyring:
            kek = self._keyring_kek()
            if kek is not None:
                return kek, MODE_KEYRING, salt
        if self.passphrase is not None:
            # An explicit passphrase is authoritative: it must not be shadowed by
            # the key-file fallback, or the vault would silently switch modes and
            # stop opening with the passphrase the user typed.
            if fresh_salt or len(salt) != SALT_BYTES:
                salt = secrets.token_bytes(SALT_BYTES)
            return self._derive_passphrase_kek(salt, required=True), MODE_PASSPHRASE, salt
        # Local key-file fallback. Not gated on ``prefer_keyring``: that flag only
        # decides whether the OS credential store is *attempted*, while the
        # key file is the tier that keeps headless hosts (and QWEN_VOICE_NO_KEYRING)
        # usable at all. An explicit passphrase above still takes precedence.
        kek = self._keyfile_kek(create=True)
        if kek is not None:
            return kek, MODE_KEYFILE, salt
        if fresh_salt or len(salt) != SALT_BYTES:
            salt = secrets.token_bytes(SALT_BYTES)
        kek = self._derive_passphrase_kek(salt, required=True)
        assert kek is not None  # required=True guarantees a key or a raise
        return kek, MODE_PASSPHRASE, salt

    def _kek_for_read_with(self, mode: str, salt: bytes) -> bytes:
        """Return the KEK matching the mode recorded inside the vault file."""
        if mode == MODE_KEYRING:
            kek = self._keyring_kek()
            if kek is not None:
                return kek
            # Keyring unavailable/changed since the write — a passphrase may
            # still be able to open a vault re-written into passphrase mode.
            kek = self._derive_passphrase_kek(salt, required=False)
            if kek is not None:
                return kek
            raise VaultError("vault 解密失败：无法访问系统凭据管理器，且未提供口令")
        if mode == MODE_KEYFILE:
            kek = self._keyfile_kek(create=False)
            if kek is not None:
                return kek
            raise VaultError("vault 解密失败：本机密钥文件缺失或损坏（vault.enc.key）")
        kek = self._derive_passphrase_kek(salt, required=self.passphrase is not None)
        if kek is None:
            raise VaultError("vault 解密失败：该密钥库使用口令模式，但未提供口令")
        return kek

    # ------------------------------------------------------------------
    # blob read/write
    # ------------------------------------------------------------------
    def _empty_vault(self) -> dict[str, Any]:
        """Placeholder state for a vault file that does not exist yet."""
        return {"salt": secrets.token_bytes(SALT_BYTES), "mode": None, "nonce": None}

    def _read_blob_locked(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty_vault()
        except OSError as exc:
            raise VaultError("vault 读取失败：无法读取密钥库文件") from exc
        try:
            blob = json.loads(raw)
        except ValueError as exc:
            raise VaultError("vault 读取失败：密钥库文件已损坏，无法解析") from exc
        if not isinstance(blob, dict):
            raise VaultError("vault 读取失败：密钥库文件格式不正确")
        if blob.get("v") != VAULT_FORMAT_VERSION:
            raise VaultError("vault 读取失败：密钥库版本不受支持")
        for field in ("salt", "nonce", "ct"):
            if not isinstance(blob.get(field), str):
                raise VaultError("vault 读取失败：密钥库文件缺少必要字段")
        return blob

    def _load_locked(self) -> tuple[dict[str, str], bytes]:
        """Decrypt the whole mapping. Caller must already hold the lock."""
        blob = self._read_blob_locked()
        if blob.get("nonce") is None:  # no file yet
            return {}, blob["salt"]

        salt = _b64d(blob["salt"])
        mode = blob.get("mode")
        if mode not in (MODE_KEYRING, MODE_PASSPHRASE, MODE_KEYFILE):
            raise VaultError("vault 读取失败：密钥库模式无效")
        kek = self._kek_for_read_with(mode, salt)
        try:
            plaintext = AESGCM(kek).decrypt(_b64d(blob["nonce"]), _b64d(blob["ct"]), None)
        except InvalidTag as exc:
            raise VaultError(
                "vault 解密失败：主密钥或口令不匹配，或文件已被篡改"
            ) from exc
        except (ValueError, TypeError) as exc:
            raise VaultError("vault 解密失败：密钥库文件已损坏") from exc
        try:
            entries = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise VaultError("vault 解密失败：密钥库内容已损坏") from exc
        if not isinstance(entries, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in entries.items()
        ):
            raise VaultError("vault 解密失败：密钥库内容格式不正确")
        return dict(entries), salt

    def _save_locked(self, entries: dict[str, str], salt: bytes) -> None:
        """Encrypt with a fresh nonce and atomically replace the vault file."""
        kek, mode, salt = self._kek_for_write(salt, fresh_salt=not self.path.exists())
        nonce = secrets.token_bytes(NONCE_BYTES)
        payload = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ct = AESGCM(kek).encrypt(nonce, payload, None)
        blob = {
            "v": VAULT_FORMAT_VERSION,
            "mode": mode,
            "salt": _b64e(salt),
            "nonce": _b64e(nonce),
            "ct": _b64e(ct),
        }
        self._atomic_write_locked(json.dumps(blob, ensure_ascii=False, indent=2))

    def _atomic_write_locked(self, text: str) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{self.path.name}.{os.getpid()}.tmp"
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise VaultError("vault 写入失败：无法原子替换密钥库文件") from exc
        self._harden_permissions()
        keyfile = self._keyfile_path()
        if keyfile.exists():
            self._harden_permissions(keyfile)

    def _harden_permissions(self, path: Path | None = None) -> None:
        """Best-effort ACL tightening on Windows (spec §8.2) — never fatal."""
        target = self.path if path is None else path
        if os.name != "nt":  # pragma: no cover - POSIX-only path
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return
        try:  # pragma: no cover - exercised only on Windows
            import subprocess

            subprocess.run(
                [
                    "icacls",
                    str(target),
                    "/inheritance:r",
                    "/grant:r",
                    f"{os.environ.get('USERNAME', '')}:F",
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
