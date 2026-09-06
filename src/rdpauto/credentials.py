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

PROFILE_FIELDS = ("host", "port", "username", "domain", "width", "height",
                  "telegram_chat_id", "repo")


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
#
# The store keeps one entry per server, keyed by host, plus the last one used:
#   {"profiles": {"192.168.1.3": {...}, "192.168.1.43": {...}}, "last": "..."}
# An older single-server file (flat dict with "host" at the top) is migrated
# into that shape on read, so nothing saved before is lost.

def _read_store() -> dict[str, Any]:
    """The raw store as ``{"profiles": {...}, "last": host}``, migrating the
    old single-profile format on the way."""
    path = profile_path()
    if not path.exists():
        return {"profiles": {}, "last": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable profile %s: %s", path, exc)
        return {"profiles": {}, "last": ""}
    if not isinstance(data, dict):
        return {"profiles": {}, "last": ""}
    if "profiles" in data and isinstance(data["profiles"], dict):
        return data
    if data.get("host"):                       # legacy flat single profile
        return {"profiles": {str(data["host"]): data}, "last": str(data["host"])}
    return {"profiles": {}, "last": ""}


def _decrypt(entry: dict[str, Any]) -> dict[str, str]:
    """Turn one stored entry into plain form values, decrypting the secrets."""
    values = {f: str(entry.get(f, "")) for f in PROFILE_FIELDS}
    if entry.get("password"):
        secret = unprotect(str(entry["password"]))
        if secret is None:
            logger.warning("Saved password could not be decrypted (different "
                           "Windows user or machine?). You will be asked for it.")
        else:
            values["password"] = secret
    if entry.get("telegram_token"):            # encrypted like the password
        secret = unprotect(str(entry["telegram_token"]))
        if secret is not None:
            values["telegram_token"] = secret
    return {k: v for k, v in values.items() if v}


def hosts() -> list[str]:
    """Every saved server's host, newest-used first then the rest sorted."""
    store = _read_store()
    names = list(store["profiles"])
    last = store.get("last")
    names.sort()
    if last in names:
        names.remove(last)
        names.insert(0, last)
    return names


def load(host: str | None = None) -> dict[str, str]:
    """Values for ``host``, or the last-used server when host is None. {} if none."""
    store = _read_store()
    if host is None:
        host = store.get("last", "")
    entry = store["profiles"].get(host or "", {})
    return _decrypt(entry) if entry else {}


def save(values: dict[str, str], remember_password: bool = False) -> Path | None:
    """Save one server (keyed by its host) and mark it last-used. Merges into
    that host's existing entry, leaving the other servers untouched."""
    host = (values.get("host") or "").strip()
    if not host:
        logger.warning("No host in the values; nothing saved.")
        return None

    store = _read_store()
    entry = store["profiles"].get(host, {})
    for f in PROFILE_FIELDS:
        if f in values:
            entry[f] = str(values.get(f) or "")

    # Password: saved only when the box is ticked; unticking forgets it.
    if remember_password and values.get("password"):
        if not DPAPI_AVAILABLE:
            logger.warning("Passwords are only remembered on Windows (DPAPI). "
                           "Set RDP_PASSWORD instead.")
        else:
            try:
                entry["password"] = protect(values["password"])
            except OSError as exc:
                logger.warning("Could not encrypt the password, not saving it: %s", exc)
    elif "password" in values and not remember_password:
        entry.pop("password", None)

    # Telegram token: saved whenever provided, encrypted; clearing it forgets it.
    if values.get("telegram_token"):
        if not DPAPI_AVAILABLE:
            logger.warning("The Telegram token is only remembered on Windows "
                           "(DPAPI). Set RDP_TELEGRAM_TOKEN in .env instead.")
        else:
            try:
                entry["telegram_token"] = protect(values["telegram_token"])
            except OSError as exc:
                logger.warning("Could not encrypt the Telegram token, not saving it: %s", exc)
    elif "telegram_token" in values:
        entry.pop("telegram_token", None)

    store["profiles"][host] = entry
    store["last"] = host

    path = profile_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save the profile to %s: %s", path, exc)
        return None
    try:
        os.chmod(path, 0o600)               # best effort: readable only by this user
    except OSError:
        pass
    return path


def has_saved_password(host: str | None = None) -> bool:
    store = _read_store()
    if host is None:
        host = store.get("last", "")
    return bool(store["profiles"].get(host or "", {}).get("password"))


def forget(host: str | None = None) -> bool:
    """Remove one saved server, or (host=None) delete the whole store."""
    path = profile_path()
    if host is None:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            return False

    store = _read_store()
    if host not in store["profiles"]:
        return False
    del store["profiles"][host]
    if store.get("last") == host:
        store["last"] = next(iter(store["profiles"]), "")
    try:
        path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not update %s: %s", path, exc)
        return False
    return True
