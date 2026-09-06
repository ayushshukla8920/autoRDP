"""tkinter front end for autoRDP.

    python -m rdpauto.gui           # connection form, then a log window
    python -m rdpauto.gui --stop    # a floating STOP button, nothing else

(or the ``autordp-gui`` console script after ``pip install -e .``).

One of two entry points -- ``rdpauto.cli`` is the other, for machines with no
display. Everything under ``rdpauto`` is a module of the application.
"""

from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk
from pathlib import Path

from typing import TYPE_CHECKING

from ..config import PROJECT_ROOT, Settings
from ..loop import LoopThread
from .form import ConnectionForm
from .stop import show_stop_button

# The RDP stack (aardwolf + crypto + Pillow decode paths) is ~40 MB and only
# needed once you actually connect, so it is imported lazily -- the connection
# form and the --stop window stay light until then.
if TYPE_CHECKING:
    from ..rdp_client import RdpClient

# Form field -> environment variable, the one place this mapping lives.
_ENV = {
    "host": "RDP_HOST", "port": "RDP_PORT", "username": "RDP_USERNAME",
    "domain": "RDP_DOMAIN", "password": "RDP_PASSWORD",
    "width": "RDP_WIDTH", "height": "RDP_HEIGHT",
    "telegram_token": "RDP_TELEGRAM_TOKEN",
    "telegram_chat_id": "RDP_TELEGRAM_CHAT_ID",
}


def _apply_to_env(values: dict[str, str]) -> None:
    # Only set keys the form carries, so a blank field never wipes an env/.env value.
    for name, env in _ENV.items():
        if name in values:
            os.environ[env] = values.get(name, "")


def _settings_from(values: dict[str, str]) -> Settings:
    """Build Settings from form values, then scrub the plaintext password from
    the environment so no child process (e.g. the git clone) inherits it."""
    _apply_to_env(values)
    settings = Settings.load(interactive=False)
    os.environ.pop("RDP_PASSWORD", None)
    return settings


def collect_connection_details(show_action: bool = False) -> dict[str, str] | None:
    """Show the connection form and return the entered values, or None."""
    root = tk.Tk()
    form = ConnectionForm(root, show_action=show_action)
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return form.result


def settings_from_gui() -> Settings | None:
    """Build Settings from the form; everything else keeps its default.

    Used by the CLI/session ``--gui`` path.
    """
    values = collect_connection_details(show_action=False)
    if values is None:
        return None
    return _settings_from(values)


class AppSession:
    """Holds the asyncio loop and one RDP client so a single connection is
    reused across actions. The connection is only rebuilt when the target host
    changes or it has dropped, and closed only when the whole app exits."""

    def __init__(self) -> None:
        self.loop: LoopThread | None = None
        self.client: RdpClient | None = None

    # Fields that define the live connection: a change to any of them means the
    # existing session can't be reused and must be rebuilt.
    _CONN_KEYS = ("host", "port", "username", "domain", "auth", "width", "height")

    def prepare(self, settings: Settings) -> "RdpClient":
        from ..rdp_client import RdpClient   # heavy: loaded only on first connect
        if self.loop is None:
            self.loop = LoopThread()
            self.loop.start()
        if self.client is not None and (
                not self.client.is_alive
                or not self._same_connection(self.client.settings, settings)):
            self._disconnect()
        if self.client is None:
            self.client = RdpClient(settings)
        else:
            # Same live connection: adopt the new settings so form edits to
            # cosmetic fields (Telegram, screenshots, stall timeout) take effect.
            self.client.settings = settings
        self.client.stop.reset()          # clear any prior pause/abort
        return self.client

    @classmethod
    def _same_connection(cls, a: Settings, b: Settings) -> bool:
        return all(getattr(a, k) == getattr(b, k) for k in cls._CONN_KEYS)

    def _disconnect(self) -> None:
        if self.client is not None and self.loop is not None:
            try:
                self.loop.run(self.client.disconnect())
            except Exception:  # noqa: BLE001
                pass
        self.client = None

    def close(self) -> None:
        self._disconnect()
        if self.loop is not None:
            self.loop.close()
            self.loop = None


def run_in_window(values: dict[str, str], app: AppSession) -> bool:
    """Run one action in a log window, reusing ``app``'s connection. Returns
    True if the user asked to go back to the menu, False if they quit."""
    from .runner import RunnerWindow      # pulls the RDP stack; only on connect
    settings = _settings_from(values)
    app.prepare(settings)
    root = tk.Tk()
    window = RunnerWindow(root, app, values.get("action", "test"), options=values)
    root.mainloop()
    return window.restart


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tkinter front end for autoRDP")
    parser.add_argument("--stop", action="store_true",
                        help="show only the floating emergency STOP button")
    args = parser.parse_args(argv)

    if args.stop:
        stop_file = Path((os.environ.get("RDP_STOP_FILE") or "").strip()
                         or PROJECT_ROOT / "STOP")
        show_stop_button(stop_file)
        return 0

    # Loop: form -> run -> (Menu button) back to the form, reusing one
    # connection, until the user quits.
    app = AppSession()
    first = True
    try:
        while True:
            values = collect_connection_details(show_action=True)
            if values is None:
                return 130 if first else 0
            first = False
            if not run_in_window(values, app):
                return 0
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main())
