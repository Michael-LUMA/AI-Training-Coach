"""Credential encryption with Windows DPAPI or an explicit master key.

中文摘要：无后端拒绝存储；不接受旧明文。masterkey 是 stdlib 受限构造，
非审计级加密；可引入依赖后应迁移到 Fernet/libsodium。
Masterkey uses HKDF-SHA256, an HMAC-counter keystream and encrypt-then-MAC.
This constrained stdlib construction is NOT audited cryptography. Migrate to
Fernet/libsodium when dependencies become available. Supply a random, secret
master key of at least 32 UTF-8 bytes; keep it outside the database and backups.
Existing plaintext databases are rejected: reconnect into a fresh database.
Deleting/replacing a row does not guarantee erasure of old SQLite backups.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import secrets
from ctypes import wintypes
from action_cards import ActionError

PREFIX = "luma-key-v1:"
CRYPTPROTECT_UI_FORBIDDEN = 1


def _error():
    return ActionError("key_storage_unavailable", "Encrypted credential storage unavailable; check backend and reconnect if necessary", 503)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


class MasterKeyBackend:
    """HKDF extract/expand + HMAC-CTR + HMAC-SHA256; NOT audited crypto.

    No password stretching: the caller must provision high-entropy key material.
    Salt and nonce are fresh 32-byte random values on every encryption.
    """
    name = "masterkey"

    def __init__(self, master_key: str | None = None):
        value = os.environ.get("LUMA_COACH_MASTER_KEY") if master_key is None else master_key
        if not isinstance(value, str):
            raise _error()
        try:
            self._key = value.encode("utf-8")
        except UnicodeError:
            raise _error() from None
        if len(self._key) < 32 or not value.strip():
            raise _error()

    def _keys(self, salt: bytes) -> tuple[bytes, bytes]:
        # RFC 5869 Extract followed by two Expand blocks, domain-separated info.
        prk = _hmac(salt, self._key)
        info = b"luma-coach/masterkey/v1"
        first = _hmac(prk, info + b"\x01")
        second = _hmac(prk, first + info + b"\x02")
        return first, second

    @staticmethod
    def _crypt(key: bytes, nonce: bytes, value: bytes) -> bytes:
        result = bytearray()
        for offset in range(0, len(value), 32):
            block = _hmac(key, b"luma-coach/ctr/v1" + nonce + (offset // 32).to_bytes(8, "big"))
            result.extend(a ^ b for a, b in zip(value[offset:offset+32], block))
        return bytes(result)

    def encrypt(self, plaintext: bytes) -> bytes:
        salt, nonce = secrets.token_bytes(32), secrets.token_bytes(32)
        enc, mac = self._keys(salt)
        payload = salt + nonce + self._crypt(enc, nonce, plaintext)
        return payload + _hmac(mac, b"luma-coach/masterkey/v1" + payload)

    def decrypt(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) < 96:
            raise _error()
        payload, tag = ciphertext[:-32], ciphertext[-32:]
        enc, mac = self._keys(payload[:32])
        if not hmac.compare_digest(tag, _hmac(mac, b"luma-coach/masterkey/v1" + payload)):
            raise _error()
        return self._crypt(enc, payload[32:64], payload[64:])


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class DPAPIBackend:
    """Windows current-user DPAPI, no UI, no machine-wide protection flag."""
    name = "dpapi"

    def __init__(self):
        if os.name != "nt":
            raise _error()
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            blob = ctypes.POINTER(_Blob)
            protect = self._crypt32.CryptProtectData
            protect.argtypes = [blob, wintypes.LPCWSTR, blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob]
            protect.restype = wintypes.BOOL
            unprotect = self._crypt32.CryptUnprotectData
            unprotect.argtypes = [blob, ctypes.POINTER(wintypes.LPWSTR), blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob]
            unprotect.restype = wintypes.BOOL
            self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            self._kernel32.LocalFree.restype = ctypes.c_void_p
            # A DLL can load even when this account/profile cannot use DPAPI.
            probe = b"luma-coach-backend-probe"
            if self.decrypt(self.encrypt(probe)) != probe:
                raise _error()
        except (AttributeError, OSError):
            raise _error() from None

    def _call(self, value: bytes, decrypt: bool) -> bytes:
        buffer = ctypes.create_string_buffer(value)
        source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = _Blob()
        function = self._crypt32.CryptUnprotectData if decrypt else self._crypt32.CryptProtectData
        try:
            if not function(ctypes.byref(source), None, None, None, None,
                            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
                raise _error()
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                ctypes.memset(output.pbData, 0, output.cbData)
                self._kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._call(plaintext, False)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._call(ciphertext, True)


class KeyStore:
    """Versioned ciphertext envelope; injected backends enable offline tests.

    Windows always selects DPAPI. Initialization/operation failures never fall
    back to plaintext or a different backend. Backend mismatch fails closed.
    """
    def __init__(self, backend=None):
        self.backend = backend
        if backend is None:
            try:
                self.backend = DPAPIBackend() if os.name == "nt" else MasterKeyBackend()
            except ActionError:
                self.backend = None

    @property
    def status(self) -> dict:
        return {"encrypted_at_rest": self.backend is not None,
                "key_storage_backend": self.backend.name if self.backend else None}

    def encrypt(self, plaintext: str) -> str:
        if self.backend is None:
            raise _error()
        try:
            raw = self.backend.encrypt(plaintext.encode("utf-8"))
            return PREFIX + self.backend.name + ":" + base64.b64encode(raw).decode("ascii")
        except (ValueError, UnicodeError, OSError):
            raise _error() from None

    def decrypt(self, envelope: str) -> str:
        if self.backend is None or not isinstance(envelope, str):
            raise _error()
        prefix = PREFIX + self.backend.name + ":"
        if not envelope.startswith(prefix) or len(envelope) > 65536:
            raise _error()
        try:
            raw = base64.b64decode(envelope[len(prefix):], validate=True)
            return self.backend.decrypt(raw).decode("utf-8")
        except (ValueError, UnicodeError, OSError):
            raise _error() from None
