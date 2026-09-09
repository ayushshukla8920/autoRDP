"""Remembers the last used connection details.

Stored at ``%LOCALAPPDATA%\\autordp\\profile.json`` -- outside
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

logger = logging.getLogger("autordp.credentials")

PROFILE_FIELDS = ("host", "port", "username", "domain", "width", "height")


def profile_path() -> Path:
    """Where the profile lives. ``RDP_PROFILE`` overrides it."""
    override = (os.environ.get("RDP_PROFILE") or "").strip()
    if override:
        return Path(override)
    return _base() / "autordp" / "profile.json"


def _base() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))


def legacy_profile_path() -> Path:
    """Where the profile lived before the tool was renamed to autordp.

    Read from, never written to. Without this, renaming the directory would
    silently lose a saved connection and the next run would ask for everything
    again with no explanation -- and the saved password, which is the part
    people actually mind re-entering, would look like it had been thrown away.
    The DPAPI blob itself is unaffected: the description string passed to
    CryptProtectData is metadata, not part of the key, so a password encrypted
    under the old name still decrypts.
    """
    return _base() / "rdp-background-automation" / "profile.json"


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
        ctypes.byref(plain), "autordp", None, None, None,
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
# On disk, version 2:
#
#     {"version": 2, "connections": [{"name": ..., "host": ..., ...}, ...]}
#
# Version 1 was a single flat object, one connection and no envelope. It is
# still read and folded into a one-entry list, so an existing profile keeps
# working and is rewritten in the new shape by the next `config set`.
#
# Connections are addressed by a **1-based** index, because the index is a
# thing people type (`--conn 2`) rather than an offset the code walks.

SCHEMA_VERSION = 2


class Connection(dict):
    """One saved connection. A dict, so it drops straight into the resolver."""

    @property
    def label(self) -> str:
        """`name`, or `user@host` when it was never given one."""
        if self.get("name"):
            return str(self["name"])
        who = self.get("username") or "?"
        return f"{who}@{self.get('host') or '?'}"


def _read_raw() -> dict:
    """The parsed profile file, or {} when there is nothing usable."""
    path = profile_path()
    if not path.exists():
        # Fall back to the pre-rename location, once. The next successful
        # connection rewrites it at the new path, so this fades out by itself.
        legacy = legacy_profile_path()
        if not (os.environ.get("RDP_PROFILE") or "").strip() and legacy.exists():
            logger.info("Reading the profile from its old location %s; the "
                        "next save will write it to %s", legacy, path)
            path = legacy
        else:
            return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable profile %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _stored_records() -> list[dict[str, Any]]:
    """The raw records as they sit on disk, passwords still encrypted."""
    data = _read_raw()
    records = data.get("connections")
    if not isinstance(records, list):
        records = [data] if data else []
    return [r for r in records if isinstance(r, dict)]


def _decode(record: dict) -> Connection:
    """One stored record, with its password decrypted if it can be."""
    values = Connection({f: str(record.get(f, "") or "") for f in PROFILE_FIELDS})
    values["name"] = str(record.get("name", "") or "")
    token = record.get("password")
    if token:
        secret = unprotect(str(token))
        if secret is None:
            logger.warning(
                "A saved password could not be decrypted (different Windows "
                "user or machine?). You will be asked for it.")
        else:
            values["password"] = secret
    return Connection({k: v for k, v in values.items() if v})


def load_all() -> list[Connection]:
    """Every saved connection, in the order they will be numbered."""
    # _stored_records folds version 1 -- where the whole file was a single
    # connection with no envelope -- into a one-entry list.
    return [_decode(r) for r in _stored_records()]


def load(index: int | None = None) -> Connection:
    """One saved connection. ``index`` is 1-based; None means the first.

    An out-of-range index returns empty rather than raising: the caller turns
    "nothing saved" and "no such connection" into the same actionable message,
    and neither is exceptional enough to unwind a run over.
    """
    saved = load_all()
    if not saved:
        return Connection()
    if index is None:
        return saved[0]
    if 1 <= index <= len(saved):
        return saved[index - 1]
    return Connection()


def count() -> int:
    return len(load_all())


def _encode(values: dict[str, str], remember_password: bool) -> dict[str, Any]:
    record: dict[str, Any] = {f: str(values.get(f, "") or "") for f in PROFILE_FIELDS}
    if values.get("name"):
        record["name"] = str(values["name"])

    if remember_password and values.get("password"):
        if not DPAPI_AVAILABLE:
            # Deliberately not stored in plain text as a consolation prize. Off
            # Windows there is no OS-backed secret store here, and a base64
            # blob in a JSON file would look like protection while being none.
            logger.warning(
                "Passwords are only remembered on Windows, where DPAPI can "
                "encrypt them against your account. Elsewhere, pass the "
                "password in RDP_PASSWORD or on stdin.")
        else:
            try:
                record["password"] = protect(values["password"])
            except OSError as exc:
                logger.warning("Could not encrypt the password, not saving it: %s",
                               exc)
    return record


def _write(records: list[dict[str, Any]]) -> Path | None:
    path = profile_path()
    payload = {"version": SCHEMA_VERSION, "connections": records}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save the profile to %s: %s", path, exc)
        return None

    # Best effort: make the file readable only by this user. It matters more
    # now that it can hold several encrypted passwords.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.debug("Profile saved to %s", path)
    return path


def save(values: dict[str, str], remember_password: bool = False,
         index: int | None = None) -> Path | None:
    """Add or update one connection. Returns the file path, or None on failure.

    With no ``index``, an existing entry for the same host and username is
    updated in place and anything else is appended. Without that, running
    ``config set`` twice against the same machine -- which is exactly what
    happens after a password change -- would quietly accumulate duplicates.
    """
    # The stored records are carried through untouched rather than decoded and
    # re-encoded. Only the one being written is rebuilt, so every other saved
    # password keeps the exact ciphertext it already had -- a round trip
    # through _decode/_encode would silently drop any it could not decrypt.
    records = _stored_records()
    fresh = _encode(values, remember_password)

    if index is not None and 1 <= index <= len(records):
        records[index - 1] = fresh
    else:
        for position, existing in enumerate(records):
            if (existing.get("host", "").lower() == fresh.get("host", "").lower()
                    and existing.get("username", "").lower()
                    == fresh.get("username", "").lower()):
                records[position] = fresh
                break
        else:
            records.append(fresh)
    return _write(records)


def has_saved_password(index: int | None = None) -> bool:
    """Whether the selected connection has a password stored on disk."""
    records = _stored_records()
    position = 0 if index is None else index - 1
    if not 0 <= position < len(records):
        return False
    return bool(records[position].get("password"))


def forget(index: int | None = None) -> bool:
    """Remove one connection, or the whole file when ``index`` is None."""
    if index is None:
        path = profile_path()
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            return False

    records = _stored_records()
    if not 1 <= index <= len(records):
        return False
    del records[index - 1]
    if not records:
        return forget(None)
    return _write(records) is not None
