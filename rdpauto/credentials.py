"""Remembers the last used connection details.

Stored at ``%LOCALAPPDATA%\\rdp-background-automation\\profile.json`` -- outside
the project, so it can never be committed by accident.

The non-secret fields are plain JSON. The password is only saved when you ask
for it, and then it is encrypted with **DPAPI** (``CryptProtectData``), which
keys the ciphertext to your Windows user account: another account on the same
machine, or the same file copied elsewhere, cannot decrypt it. That is a real
improvement over plaintext, but it is not a secret manager -- anything running
*as you* can still decrypt it. Leave "remember password" off if that matters.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

try:  # ctypes.wintypes does not import at all off Windows
    from ctypes import wintypes
    _DWORD = wintypes.DWORD
except (ImportError, ValueError):  # pragma: no cover - only hit off Windows
    _DWORD = ctypes.c_uint32

logger = logging.getLogger("rdpauto.credentials")

PROFILE_FIELDS = ("host", "port", "username", "domain", "width", "height")


def profile_path() -> Path:
    """Where the profile lives. ``RDP_PROFILE`` overrides it."""
    override = (os.environ.get("RDP_PROFILE") or "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "rdp-background-automation" / "profile.json"


# --------------------------------------------------------------------- DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", _DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


DPAPI_AVAILABLE = sys.platform == "win32" and hasattr(ctypes, "WinDLL")


def _crypt32():
    if not DPAPI_AVAILABLE:
        raise OSError("DPAPI is only available on Windows")
    return ctypes.WinDLL("crypt32", use_last_error=True)


def _to_blob(data: bytes) -> tuple[_Blob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer  # the buffer must outlive the blob


def _from_blob(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _Blob) -> None:
    ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(blob.pbData)


def protect(secret: str) -> str:
    """Encrypt a string for the current Windows user; returns base64.

    Raises OSError off Windows, where there is no DPAPI. The caller treats that
    as "cannot remember the password" rather than a fatal error -- the rest of
    the profile is still saved and the password is simply asked for each time.
    """
    plain, keepalive = _to_blob(secret.encode("utf-8"))
    out = _Blob()
    ok = _crypt32().CryptProtectData(
        ctypes.byref(plain), "rdp-background-automation", None, None, None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out))
    del keepalive
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return base64.b64encode(_from_blob(out)).decode("ascii")
    finally:
        _free(out)


def unprotect(token: str) -> str | None:
    """Decrypt what :func:`protect` produced. None if it cannot be decrypted."""
    if not DPAPI_AVAILABLE:
        logger.debug("No DPAPI on this platform; ignoring the saved password")
        return None
    try:
        raw = base64.b64decode(token.encode("ascii"), validate=True)
    except Exception:  # noqa: BLE001 - a corrupt profile should not be fatal
        logger.debug("Saved password is not valid base64")
        return None
    cipher, keepalive = _to_blob(raw)
    out = _Blob()
    ok = _crypt32().CryptUnprotectData(
        ctypes.byref(cipher), None, None, None, None, 0x01, ctypes.byref(out))
    del keepalive
    if not ok:
        # Normal when the profile was written by another user or machine.
        logger.debug("CryptUnprotectData failed (error %d)", ctypes.get_last_error())
        return None
    try:
        return _from_blob(out).decode("utf-8")
    finally:
        _free(out)


# ------------------------------------------------------------------- profile

def load() -> dict[str, str]:
    """Read the saved profile. Returns {} when there is nothing usable."""
    path = profile_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable profile %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}

    values = {f: str(data.get(f, "")) for f in PROFILE_FIELDS}
    token = data.get("password")
    if token:
        secret = unprotect(str(token))
        if secret is None:
            logger.warning(
                "Saved password could not be decrypted (different Windows user or "
                "machine?). You will be asked for it.")
        else:
            values["password"] = secret
    return {k: v for k, v in values.items() if v}


def save(values: dict[str, str], remember_password: bool = False) -> Path | None:
    """Write the profile. Returns the path, or None if it could not be saved."""
    path = profile_path()
    record: dict[str, Any] = {f: str(values.get(f, "") or "") for f in PROFILE_FIELDS}

    if remember_password and values.get("password"):
        if not DPAPI_AVAILABLE:
            logger.warning(
                "Passwords are only remembered on Windows, where DPAPI can "
                "encrypt them. Set RDP_PASSWORD instead.")
        else:
            try:
                record["password"] = protect(values["password"])
            except OSError as exc:
                logger.warning("Could not encrypt the password, not saving it: %s", exc)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save the profile to %s: %s", path, exc)
        return None

    # Best effort: make the file readable only by this user.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.debug("Profile saved to %s", path)
    return path


def has_saved_password() -> bool:
    path = profile_path()
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("password"))
    except (OSError, ValueError):
        return False


def forget() -> bool:
    """Delete the saved profile. True if a file was removed."""
    path = profile_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
        return False
