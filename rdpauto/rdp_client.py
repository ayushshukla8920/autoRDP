"""RDP connection management.

``RdpClient`` owns one RDP connection that this process establishes itself. It
is a client in its own right -- a peer of ``mstsc.exe``, not a driver of it --
so the session it opens is separate from whatever the local user is doing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from aardwolf.commons.factory import RDPConnectionFactory
from aardwolf.commons.iosettings import RDPIOSettings
from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

from .config import Settings
from .input_events import EmergencyStop, InputSender, SessionNotAcceptingInput

logger = logging.getLogger("rdpauto.client")

# aardwolf URL scheme per auth mode. CredSSP/NLA is used for ntlm and kerberos.
_AUTH_SCHEMES = {
    "ntlm": "rdp+ntlm-password",
    "kerberos": "rdp+kerberos-password",
    "plain": "rdp+plain",
}


class ConnectionFailed(Exception):
    """Raised when the connection could not be established."""


class RdpClient:
    """An RDP session plus the input channel into it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop = EmergencyStop(settings.stop_file)
        self.connection = None
        self.input: InputSender | None = None
        self._drain_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ set-up

    def _build_url(self) -> str:
        """Compose the aardwolf connection URL, percent-encoding credentials.

        Passwords routinely contain ``@``, ``:``, ``/`` and ``#``, all of which
        would otherwise be parsed as URL syntax.
        """
        s = self.settings
        scheme = _AUTH_SCHEMES[s.auth]
        userinfo = quote(s.username, safe="")
        if s.domain:
            userinfo = f"{quote(s.domain, safe='')}\\{userinfo}"
        if s.auth == "plain":
            # The plain scheme carries no username in the URL userinfo.
            userinfo = ""
        secret = quote(s.password, safe="")
        creds = f"{userinfo}:{secret}@" if userinfo else f"{secret}@"
        return (f"{scheme}://{creds}{s.host}:{s.port}/"
                f"?timeout={int(max(1, s.connect_timeout))}")

    def _io_settings(self) -> RDPIOSettings:
        io = RDPIOSettings()
        io.video_width = self.settings.width
        io.video_height = self.settings.height
        io.video_out_format = VIDEO_FORMAT.PIL
        io.keyboard_layout = 1033

        # Join no virtual channels. Input travels on the MCS channel, which the
        # library sets up separately, so nothing here needs them.
        #
        # This is not just tidiness. aardwolf's clipboard channel raises
        # AttributeError when the server sends a FORMAT_DATA_REQUEST and no
        # clipboard data has been set, and an exception in the channel reader
        # runs terminate() in its finally block -- so a server politely asking
        # for the clipboard would kill the whole session mid-automation.
        io.channels = []
        io.vchannels = {}

        # Belt and braces: never let the remote clipboard overwrite the local
        # one. This tool is supposed to leave the local desktop alone.
        io.clipboard_use_pyperclip = False
        return io

    def describe_url(self) -> str:
        """The connection URL with the password masked, for logs."""
        return self._build_url().replace(quote(self.settings.password, safe=""), "***")

    # --------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Connect, retrying up to ``connect_retries`` times."""
        s = self.settings
        last_error: Exception | None = None

        for attempt in range(1, s.connect_retries + 1):
            self.stop.check()
            logger.info("Connecting to %s (attempt %d/%d)", s.target, attempt, s.connect_retries)
            try:
                await self._connect_once()
                logger.info("RDP session established (%dx%d)", s.width, s.height)
                return
            except asyncio.TimeoutError:
                last_error = ConnectionFailed(
                    f"Timed out after {s.connect_timeout:.0f}s connecting to {s.host}:{s.port}")
                logger.warning("%s", last_error)
            except Exception as exc:  # noqa: BLE001 - surface any library error
                last_error = exc
                logger.warning("Connection attempt %d failed: %s", attempt, exc)

            await self._teardown()
            if attempt < s.connect_retries:
                logger.info("Retrying in %.1fs", s.retry_delay)
                await asyncio.sleep(s.retry_delay)

        raise ConnectionFailed(_explain(last_error, s)) from last_error

    async def _connect_once(self) -> None:
        io = self._io_settings()
        factory = RDPConnectionFactory.from_url(self._build_url(), io)
        connection = factory.get_connection(io)

        ok, err = await asyncio.wait_for(connection.connect(),
                                         timeout=self.settings.connect_timeout)
        if err is not None:
            raise err
        if not ok:
            raise ConnectionFailed("The server refused the connection without an error")

        self.connection = connection
        self.input = InputSender(connection, self.settings, self.stop)
        self._drain_task = asyncio.create_task(self._drain_output(), name="rdp-drain")

    async def _drain_output(self) -> None:
        """Consume the server->client queue.

        aardwolf pushes every screen update onto ``ext_out_queue``. Nothing here
        needs the individual rectangles -- the library also paints them into the
        desktop buffer that ``screenshot`` reads -- but an unread queue would
        grow without bound, so it is drained and discarded. A ``None`` is the
        library's end-of-stream sentinel.
        """
        try:
            while True:
                item = await self.connection.ext_out_queue.get()
                if item is None:
                    logger.debug("Server closed the output stream")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("Output drain stopped: %s", exc)

    @property
    def is_alive(self) -> bool:
        return self.connection is not None and not self.connection.disconnected_evt.is_set()

    def require_alive(self) -> None:
        if not self.is_alive:
            raise SessionNotAcceptingInput(
                "The RDP session is not connected, so it cannot accept input.")

    async def _teardown(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
            self._drain_task = None
        self.connection = None
        self.input = None

    async def disconnect(self) -> None:
        """Send a disconnect request and tear everything down."""
        if self.connection is None:
            return
        logger.info("Disconnecting from %s", self.settings.host)
        try:
            await asyncio.wait_for(self.connection.terminate(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Server did not acknowledge the disconnect in time")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error during disconnect: %s", exc)
        finally:
            await self._teardown()
            logger.info("Disconnected")

    async def __aenter__(self) -> "RdpClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.disconnect()

    # -------------------------------------------------------------- screenshot

    async def wait_for_desktop(self, timeout: float = 45.0, settle: float = 1.0,
                               stable_samples: int = 2) -> bool:
        """Wait until the remote desktop has painted and stopped changing.

        A freshly created RDP session is not ready for input straight away:
        Explorer still has to start and draw the shell, and until it does,
        ``Win+R`` does nothing at all because the shell is what handles it.
        Sending keystrokes before that point types them into whatever happens
        to grab focus first.

        There is no readiness signal in the protocol, so this infers one from
        the screen: wait for the first frame, then keep sampling a downscaled
        copy until it stops changing. Returns False on timeout, in which case
        the caller can carry on and rely on its own delays.
        """
        self.require_alive()
        deadline = asyncio.get_running_loop().time() + timeout

        while not self.connection.desktop_buffer_has_data:
            if asyncio.get_running_loop().time() > deadline:
                logger.warning("No frame received within %.0fs", timeout)
                return False
            await asyncio.sleep(0.25)

        previous: bytes | None = None
        stable = 0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(settle)
            thumb = self._desktop_fingerprint()
            if thumb is None:
                continue
            if previous is not None and _close_enough(previous, thumb):
                stable += 1
                if stable >= stable_samples:
                    logger.info("Remote desktop has settled")
                    return True
            else:
                stable = 0
            previous = thumb

        logger.warning("Remote desktop still changing after %.0fs, continuing anyway", timeout)
        return False

    def current_frame(self):
        """The latest decoded desktop image, or None if there is nothing yet.

        Safe to call from any thread: it only copies the image the library
        paints into, and never touches the event loop. That is what makes the
        read-only live view possible.
        """
        if self.connection is None or not self.connection.desktop_buffer_has_data:
            return None
        image = self.connection.get_desktop_buffer(VIDEO_FORMAT.PIL)
        if isinstance(image, tuple) or image is None:
            return None
        return image

    def _desktop_fingerprint(self) -> bytes | None:
        """A small greyscale thumbnail of the desktop, for change detection."""
        image = self.current_frame()
        if image is None:
            return None
        return image.convert("L").resize((64, 40)).tobytes()

    async def screenshot(self, path: str | Path | None = None) -> Path:
        """Save the current desktop buffer as a PNG and return its path."""
        self.require_alive()
        if not self.connection.desktop_buffer_has_data:
            # Give the server a moment to send the first frame.
            for _ in range(20):
                await asyncio.sleep(0.25)
                if self.connection.desktop_buffer_has_data:
                    break
            else:
                raise RuntimeError(
                    "No screen data received yet. The server has not sent a frame; "
                    "try again after some remote activity.")

        image = self.connection.get_desktop_buffer(VIDEO_FORMAT.PIL)
        if isinstance(image, tuple):  # aardwolf returns (None, exc) on failure
            raise RuntimeError(f"Could not read the desktop buffer: {image[1]}")

        if path is None:
            self.settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.settings.screenshot_dir / f"screen-{stamp}.png"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path, format="PNG")
        logger.info("Screenshot saved to %s", path)
        return path


def _close_enough(a: bytes, b: bytes, tolerance: int = 6, max_changed: int = 24) -> bool:
    """True if two thumbnails differ only trivially.

    The taskbar clock, a blinking caret and mouse-over highlights all keep
    changing forever, so exact equality would never be reached. This allows a
    handful of slightly different pixels.
    """
    if len(a) != len(b):
        return False
    changed = 0
    for x, y in zip(a, b):
        if abs(x - y) > tolerance:
            changed += 1
            if changed > max_changed:
                return False
    return True


def _explain(error: Exception | None, settings: Settings) -> str:
    """Turn a library-level failure into something actionable."""
    text = str(error or "unknown error")
    lowered = text.lower()
    hint = ""

    if "timed out" in lowered or isinstance(error, asyncio.TimeoutError):
        hint = (f"Check that {settings.host}:{settings.port} is reachable and that the "
                "firewall allows inbound RDP.")
    elif "refused" in lowered or "unreachable" in lowered or "10061" in lowered:
        hint = ("Nothing is listening on that port. Enable Remote Desktop on the server "
                "and confirm the port.")
    elif any(k in lowered for k in ("logon", "credential", "authenticate", "sec_e", "0xc000006d")):
        hint = ("Authentication was rejected. Verify the username, password and domain, "
                "and that the account may sign in over Remote Desktop.")
    elif "hybrid" in lowered or "credssp" in lowered or "protocol" in lowered:
        hint = (f"Security negotiation failed. Try RDP_AUTH=plain (or ntlm) -- currently "
                f"{settings.auth!r}.")

    return f"Could not connect to {settings.target}: {text}" + (f" {hint}" if hint else "")
