"""Configuration and credential loading.

Values are resolved in this order, first match wins:

1. environment variables (``RDP_HOST`` and friends) -- useful for scripting
2. the remembered profile (see :mod:`credentials`)
3. an interactive prompt, or the tkinter form when ``--gui`` is used

Nothing is read from a file inside the project. The password is either taken
from ``RDP_PASSWORD``, recovered from the DPAPI-encrypted profile, or typed at a
non-echoing prompt -- never stored in source.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import credentials

def _project_root() -> Path:
    """Where writable state (screenshots, the STOP file) belongs.

    In a PyInstaller one-file build ``__file__`` points inside a temporary
    extraction directory that is deleted when the process exits, so anchoring
    to it would put the STOP file and the screenshots somewhere that vanishes
    -- and the emergency stop would silently never be seen. Next to the
    executable is both persistent and where a user would look.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # This module lives in src/rdpauto/, so the repo root is two levels up.
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _project_root()

logger = logging.getLogger("rdpauto.config")


def load_dotenv(path: Path | None = None) -> None:
    """Load ``KEY=value`` lines from a ``.env`` file into ``os.environ``.

    Real environment variables always win, so a value already set is left
    alone -- the file only fills in what is missing. This is where secrets like
    ``RDP_TELEGRAM_TOKEN`` live, kept out of the saved profile and out of git.
    """
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", env_path, exc)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


@dataclass
class Settings:
    """Everything the client needs to connect and pace its input."""

    # --- connection ---
    host: str
    username: str
    password: str = field(repr=False)
    domain: str = ""
    port: int = 3389
    auth: str = "ntlm"  # ntlm | kerberos | plain

    # --- remote screen ---
    width: int = 1280
    height: int = 800

    # --- reliability ---
    connect_timeout: float = 20.0
    connect_retries: int = 3
    retry_delay: float = 3.0
    reconnect_attempts: int = 3   # mid-run reconnect tries after a dropped session
    reconnect_delay: float = 5.0
    stall_timeout: float = 45.0   # frozen-screen seconds before treating typing as stalled (0 = off)

    # --- telegram alerts (env/.env only, never saved to the profile) ---
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""

    # --- input pacing ---
    char_delay: float = 0.012   # between characters of `type`
    key_delay: float = 0.03     # between key down and key up
    action_delay: float = 0.25  # after a discrete action (click, chord, Enter)
    type_mode: str = "unicode"  # unicode | scancode
    keyboard_layout: str = "enus"

    # --- local paths ---
    screenshot_dir: Path = PROJECT_ROOT / "screenshots"
    stop_file: Path = PROJECT_ROOT / "STOP"
    log_file: Path | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.auth not in ("ntlm", "kerberos", "plain"):
            raise ConfigError(f"RDP_AUTH must be ntlm, kerberos or plain (got {self.auth!r})")
        if self.type_mode not in ("unicode", "scancode"):
            raise ConfigError(f"RDP_TYPE_MODE must be unicode or scancode (got {self.type_mode!r})")
        if not 0 < self.port < 65536:
            raise ConfigError(f"RDP_PORT out of range: {self.port}")
        self.screenshot_dir = Path(self.screenshot_dir)
        self.stop_file = Path(self.stop_file)

    @property
    def target(self) -> str:
        """Human-readable target, safe to log."""
        who = f"{self.domain}\\{self.username}" if self.domain else self.username
        return f"{who}@{self.host}:{self.port}"

    def as_profile(self) -> dict[str, str]:
        """The non-secret details worth remembering. Secrets (password, Telegram
        token) are added by :meth:`remember` only when asked -- keeping them out
        here is what stops a plain ``remember()`` from wiping a saved password."""
        return {
            "host": self.host,
            "port": str(self.port),
            "username": self.username,
            "domain": self.domain,
            "width": str(self.width),
            "height": str(self.height),
            "telegram_chat_id": self.telegram_chat_id,
        }

    def remember(self, remember_password: bool = False) -> Path | None:
        """Save these details. Secrets are included only when
        ``remember_password`` is set, so an ordinary post-connect ``remember()``
        never touches (or clears) an already-saved password or token."""
        profile = self.as_profile()
        if remember_password:
            profile["password"] = self.password
            profile["telegram_token"] = self.telegram_token
        return credentials.save(profile, remember_password=remember_password)

    @classmethod
    def load(cls, interactive: bool = True) -> "Settings":
        """Resolve settings from the environment, the profile, then prompts."""
        saved = credentials.load()

        def pick(env_name: str, key: str, fallback: str = "") -> str:
            return _env(env_name) or saved.get(key, "") or fallback

        host = pick("RDP_HOST", "host")
        username = pick("RDP_USERNAME", "username")
        domain = pick("RDP_DOMAIN", "domain")
        port_raw = pick("RDP_PORT", "port")
        password = os.environ.get("RDP_PASSWORD") or saved.get("password", "")

        if interactive:
            if saved:
                where = credentials.profile_path()
                print(f"Using remembered details from {where}")
                print("  (press Enter to accept a remembered value; "
                      "`python -m rdpauto.gui` to edit them, `--forget` to clear)")
            host = host or _ask("RDP host/IP", required=True)
            username = username or _ask("Username", required=True)
            if not domain:
                domain = _ask("Domain (optional, blank for none)")
            if not port_raw:
                port_raw = _ask("Port (blank for 3389)") or "3389"
            if not password:
                password = _prompt_password()
        else:
            missing = [n for n, v in (("RDP_HOST", host), ("RDP_USERNAME", username)) if not v]
            if missing:
                raise ConfigError("Missing required config: " + ", ".join(missing))
            if not password:
                raise ConfigError(
                    "No password available. Set RDP_PASSWORD, save one with "
                    "`python -m rdpauto.gui`, or run interactively.")

        try:
            port = int(port_raw or 3389)
        except ValueError:
            raise ConfigError(f"Port must be an integer, got {port_raw!r}") from None

        def pick_int(env_name: str, key: str, default: int) -> int:
            raw = _env(env_name) or saved.get(key, "")
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ConfigError(f"{env_name} must be an integer, got {raw!r}") from None

        log_file = _env("RDP_LOG_FILE")
        return cls(
            host=host,
            username=username,
            password=password,
            domain=domain,
            port=port,
            auth=_env("RDP_AUTH", "ntlm").lower(),
            width=pick_int("RDP_WIDTH", "width", 1280),
            height=pick_int("RDP_HEIGHT", "height", 800),
            connect_timeout=_env_float("RDP_CONNECT_TIMEOUT", 20.0),
            connect_retries=_env_int("RDP_CONNECT_RETRIES", 3),
            retry_delay=_env_float("RDP_RETRY_DELAY", 3.0),
            reconnect_attempts=_env_int("RDP_RECONNECT_ATTEMPTS", 3),
            reconnect_delay=_env_float("RDP_RECONNECT_DELAY", 5.0),
            stall_timeout=_env_float("RDP_STALL_TIMEOUT", 45.0),
            telegram_token=_env("RDP_TELEGRAM_TOKEN") or saved.get("telegram_token", ""),
            telegram_chat_id=_env("RDP_TELEGRAM_CHAT_ID") or saved.get("telegram_chat_id", ""),
            char_delay=_env_float("RDP_CHAR_DELAY", 0.012),
            key_delay=_env_float("RDP_KEY_DELAY", 0.03),
            action_delay=_env_float("RDP_ACTION_DELAY", 0.25),
            type_mode=_env("RDP_TYPE_MODE", "unicode").lower(),
            keyboard_layout=_env("RDP_KEYBOARD_LAYOUT", "enus"),
            screenshot_dir=Path(_env("RDP_SCREENSHOT_DIR") or PROJECT_ROOT / "screenshots"),
            stop_file=Path(_env("RDP_STOP_FILE") or PROJECT_ROOT / "STOP"),
            log_file=Path(log_file) if log_file else None,
            log_level=_env("RDP_LOG_LEVEL", "INFO").upper(),
        )


def _ask(label: str, required: bool = False) -> str:
    while True:
        try:
            value = input(f"{label}: ").strip()
        except EOFError:
            # An optional field can simply take its default when there is no tty.
            if not required:
                print()
                return ""
            raise ConfigError(f"{label} is required but stdin is closed") from None
        if value or not required:
            return value
        print("  -> required, please enter a value")


def _prompt_password() -> str:
    """Read the password without echoing it."""
    while True:
        try:
            value = getpass.getpass("Password (not echoed): ")
        except EOFError:
            raise ConfigError("Password is required but stdin is closed") from None
        if value:
            return value
        print("  -> required, please enter a value")


def setup_logging(settings: Settings) -> None:
    """Configure root logging once, honouring the configured level and file."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # aardwolf is chatty at DEBUG; keep it a notch quieter than our own logs.
    if settings.log_level == "DEBUG":
        logging.getLogger("aardwolf").setLevel(logging.INFO)
