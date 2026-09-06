"""Remote keyboard and mouse input, expressed as RDP input PDUs.

Every function here ends up building a ``TS_INPUT_PDU_DATA`` that aardwolf
writes to the RDP connection's MCS channel. Nothing in this module touches the
local desktop: there is no ``SendInput``, no window handle, no local cursor.

Two ways to press a key exist in RDP, and both are used here:

* **Unicode events** (``TS_UNICODE_KEYBOARD_EVENT``) carry a UTF-16 code unit and
  let the server decide which keystroke produces it. This is layout independent,
  so it is the default for typing text.
* **Scancode events** (``TS_KEYBOARD_EVENT``) carry a physical key position and
  are the only way to express named keys and modifier chords (Enter, Ctrl+S...).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Iterable, Sequence

from aardwolf.commons.queuedata.constants import MOUSEBUTTON
from aardwolf.keyboard import VK_MODIFIERS
from aardwolf.keyboard.layoutmanager import KeyboardLayoutManager

logger = logging.getLogger("rdpauto.input")


class InputError(Exception):
    """Base class for input problems."""


class SessionNotAcceptingInput(InputError):
    """The RDP session is gone, so input cannot be delivered."""


class SessionStalled(SessionNotAcceptingInput):
    """The connection is alive but the remote stopped responding (frozen
    desktop). A subclass so the reconnect handler treats it like a lost
    session -- reconnecting usually revives a wedged remote."""


class EmergencyStopped(InputError):
    """The local emergency stop was tripped."""


# Friendly key names -> the virtual-key names in aardwolf's layout tables.
KEY_ALIASES: dict[str, str] = {
    "ENTER": "VK_RETURN",
    "RETURN": "VK_RETURN",
    "TAB": "VK_TAB",
    "ESC": "VK_ESCAPE",
    "ESCAPE": "VK_ESCAPE",
    "SPACE": "VK_SPACE",
    "BACKSPACE": "VK_BACK",
    "BKSP": "VK_BACK",
    "DELETE": "VK_DELETE",
    "DEL": "VK_DELETE",
    "INSERT": "VK_INSERT",
    "INS": "VK_INSERT",
    "HOME": "VK_HOME",
    "END": "VK_END",
    "PAGEUP": "VK_PRIOR",
    "PGUP": "VK_PRIOR",
    "PAGEDOWN": "VK_NEXT",
    "PGDN": "VK_NEXT",
    "UP": "VK_UP",
    "DOWN": "VK_DOWN",
    "LEFT": "VK_LEFT",
    "RIGHT": "VK_RIGHT",
    "CTRL": "VK_LCONTROL",
    "CONTROL": "VK_LCONTROL",
    "LCTRL": "VK_LCONTROL",
    "RCTRL": "VK_RCONTROL",
    "ALT": "VK_LMENU",
    "LALT": "VK_LMENU",
    "RALT": "VK_RMENU",
    "SHIFT": "VK_LSHIFT",
    "LSHIFT": "VK_LSHIFT",
    "RSHIFT": "VK_RSHIFT",
    "WIN": "VK_LWIN",
    "LWIN": "VK_LWIN",
    "RWIN": "VK_RWIN",
    "MENU": "VK_APPS",
    "APPS": "VK_APPS",
    "CAPSLOCK": "VK_CAPITAL",
    "NUMLOCK": "VK_NUMLOCK",
    "SCROLLLOCK": "VK_SCROLL",
    "PRINTSCREEN": "VK_SNAPSHOT",
    "PAUSE": "VK_PAUSE",
    "BREAK": "VK_PAUSE",
}
KEY_ALIASES.update({f"F{n}": f"VK_F{n}" for n in range(1, 25)})

MODIFIER_NAMES = {"CTRL", "CONTROL", "LCTRL", "RCTRL", "ALT", "LALT", "RALT",
                  "SHIFT", "LSHIFT", "RSHIFT", "WIN", "LWIN", "RWIN"}

# Keys that a real keyboard sends with an E0 prefix, and which RDP therefore
# needs the KBDFLAGS_EXTENDED bit for.
#
# This is not cosmetic. The navigation cluster shares its scancodes with the
# numeric keypad -- Home and Numpad-7 are both scancode 71, Delete and
# Numpad-decimal are both 83 -- and the extended bit is the only thing that
# distinguishes them. Without it, `Home` types "7" and `Delete` types "." on
# any machine with NumLock on.
EXTENDED_VKS = {
    "VK_INSERT", "VK_DELETE", "VK_HOME", "VK_END", "VK_PRIOR", "VK_NEXT",
    "VK_UP", "VK_DOWN", "VK_LEFT", "VK_RIGHT",
    "VK_RCONTROL", "VK_RMENU", "VK_DIVIDE", "VK_SNAPSHOT",
    "VK_LWIN", "VK_RWIN", "VK_APPS",
}

BUTTONS = {
    "left": MOUSEBUTTON.MOUSEBUTTON_LEFT,
    "right": MOUSEBUTTON.MOUSEBUTTON_RIGHT,
    "middle": MOUSEBUTTON.MOUSEBUTTON_MIDDLE,
}


class EmergencyStop:
    """Two-level stop switch for outgoing input.

    * **Pause** (STOP button, or the sentinel file) suspends typing while
      keeping the session connected. Typing parks at the next character and
      continues from there when resumed -- nothing is lost, nothing disconnects.
    * **Abort** (window close, Ctrl+C) unwinds the run for good and lets the
      caller disconnect.

    The sentinel file is stat'd at most every ``poll_interval`` seconds so
    per-character typing stays cheap.
    """

    def __init__(self, stop_file: Path, poll_interval: float = 0.2) -> None:
        self.stop_file = Path(stop_file)
        self.poll_interval = poll_interval
        self._aborted = False
        self._paused = False
        self._reason = ""
        self._last_poll = 0.0
        self._file_present = False

    # ---- pause (resumable) ----
    def pause(self, reason: str = "paused") -> None:
        if not self._paused:
            self._paused = True
            self._reason = reason
            logger.info("Paused: %s", reason)

    def resume(self) -> None:
        """Lift a pause and clear the sentinel file, so typing continues."""
        self._paused = False
        self._last_poll = 0.0
        self._file_present = False
        try:
            self.stop_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove stop file %s: %s", self.stop_file, exc)
        logger.info("Resumed")

    # ---- abort (final) ----
    def abort(self, reason: str = "requested locally") -> None:
        if not self._aborted:
            self._aborted = True
            self._reason = reason
            logger.warning("EMERGENCY STOP: %s", reason)

    # Alias kept for the abort callers (Ctrl+C, window close).
    def trip(self, reason: str = "requested locally") -> None:
        self.abort(reason)

    def reset(self) -> None:
        """Clear everything (pause and abort) and remove the sentinel file."""
        self._aborted = False
        self._paused = False
        self._reason = ""
        self._last_poll = 0.0
        self._file_present = False
        try:
            self.stop_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove stop file %s: %s", self.stop_file, exc)

    def _sentinel_present(self) -> bool:
        now = time.monotonic()
        if now - self._last_poll >= self.poll_interval:
            self._last_poll = now
            self._file_present = self.stop_file.exists()
        return self._file_present

    @property
    def paused(self) -> bool:
        return self._paused or self._sentinel_present()

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def tripped(self) -> bool:
        """Backwards-compatible 'stopped in any way' (paused or aborted)."""
        return self._aborted or self.paused

    def check(self) -> None:
        """Raise on abort only. Pausing does not raise -- typing waits instead
        (see :meth:`gate`), so a pause never tears the run down."""
        if self._aborted:
            raise EmergencyStopped(f"Aborted ({self._reason}).")

    async def gate(self) -> None:
        """Await here between keystrokes: raise on abort, block while paused."""
        if self._aborted:
            raise EmergencyStopped(f"Aborted ({self._reason}).")
        while self.paused:
            await asyncio.sleep(self.poll_interval)
            if self._aborted:
                raise EmergencyStopped(f"Aborted ({self._reason}).")


class InputSender:
    """Builds and sends RDP input events over an established connection."""

    def __init__(self, connection, settings, stop: EmergencyStop) -> None:
        self._conn = connection
        self._settings = settings
        self._stop = stop
        layout = KeyboardLayoutManager().get_layout_by_shortname(settings.keyboard_layout)
        if layout is None:
            raise InputError(
                f"Unknown keyboard layout {settings.keyboard_layout!r}. "
                "Use an aardwolf layout short name such as 'enus'."
            )
        self._layout = layout

    # ---------------------------------------------------------------- plumbing

    def _guard(self) -> None:
        """Refuse to send if the session is down or the stop switch is set."""
        self._stop.check()
        if self._conn.disconnected_evt.is_set():
            raise SessionNotAcceptingInput(
                "The RDP session has disconnected, so it cannot accept input. "
                "Reconnect before sending more events."
            )

    @staticmethod
    def _check_result(result, what: str) -> None:
        """aardwolf returns ``(None, exc)`` on failure and ``None``/``(True, None)`` on success."""
        if isinstance(result, tuple) and len(result) == 2 and result[1] is not None:
            raise SessionNotAcceptingInput(f"Failed to send {what}: {result[1]}") from result[1]

    def _scancode_for_key(self, name: str) -> tuple[int, bool]:
        """Resolve a key name to ``(scancode, is_extended)``.

        Accepts a named key (``ENTER``, ``F5``) or a single character, so that
        chords like ``ctrl+s`` and ``win+r`` name their final key naturally.
        """
        key = name.strip()
        vk = KEY_ALIASES.get(key.upper())
        if vk is not None:
            try:
                return self._layout.vk_to_scancode(vk), vk in EXTENDED_VKS
            except KeyError:
                raise InputError(
                    f"Key {name!r} ({vk}) has no scancode in layout "
                    f"{self._settings.keyboard_layout!r}") from None

        if len(key) == 1:
            # Shortcuts are written unshifted (ctrl+s, not ctrl+S): take the
            # base key position and let any modifiers come from the chord.
            try:
                return self._layout.char_to_scancode(key.lower())[0], False
            except KeyError:
                pass

        raise InputError(
            f"Unknown key {name!r}. Use a single character or one of: "
            f"{', '.join(sorted(KEY_ALIASES))}"
        )

    # ---------------------------------------------------------------- keyboard

    async def key_down(self, name: str) -> None:
        await self._send_scancode(*self._scancode_for_key(name), pressed=True)

    async def key_up(self, name: str) -> None:
        await self._send_scancode(*self._scancode_for_key(name), pressed=False)

    async def _send_scancode(self, scancode: int, extended: bool = False, *,
                             pressed: bool,
                             modifiers: VK_MODIFIERS = VK_MODIFIERS(0)) -> None:
        self._guard()
        result = await self._conn.send_key_scancode(scancode, pressed, extended, modifiers)
        self._check_result(result, f"scancode {scancode}")

    async def _send_char(self, char: str, pressed: bool) -> None:
        self._guard()
        result = await self._conn.send_key_char(char, pressed)
        self._check_result(result, f"character {char!r}")

    async def key(self, name: str) -> None:
        """Press and release a single named key, e.g. ``ENTER`` or ``F5``."""
        scancode, extended = self._scancode_for_key(name)
        await self._send_scancode(scancode, extended, pressed=True)
        await asyncio.sleep(self._settings.key_delay)
        await self._send_scancode(scancode, extended, pressed=False)
        await asyncio.sleep(self._settings.action_delay)

    async def chord(self, combo: str) -> None:
        """Press a modifier combination such as ``ctrl+s`` or ``ctrl+shift+p``.

        Modifiers are held down, the final key is tapped, then modifiers are
        released in reverse order -- the same ordering a real keyboard produces.
        """
        parts = [p for p in combo.replace(" ", "").split("+") if p]
        if not parts:
            raise InputError("Empty key combination")
        if len(parts) == 1:
            await self.key(parts[0])
            return

        *mods, final = parts
        unknown = [m for m in mods if m.upper() not in MODIFIER_NAMES]
        if unknown:
            raise InputError(
                f"{', '.join(unknown)} is not a modifier. "
                "Modifiers are ctrl, alt, shift and win; put the real key last."
            )

        mod_codes = [self._scancode_for_key(m) for m in mods]
        final_code, final_ext = self._scancode_for_key(final)
        try:
            for code, ext in mod_codes:
                await self._send_scancode(code, ext, pressed=True)
                await asyncio.sleep(self._settings.key_delay)
            await self._send_scancode(final_code, final_ext, pressed=True)
            await asyncio.sleep(self._settings.key_delay)
            await self._send_scancode(final_code, final_ext, pressed=False)
        finally:
            # Never leave a modifier stuck down in the remote session.
            for code, ext in reversed(mod_codes):
                try:
                    await self._conn.send_key_scancode(code, False, ext, VK_MODIFIERS(0))
                except Exception:  # noqa: BLE001 - best effort cleanup
                    logger.debug("Could not release modifier scancode %s", code)
        await asyncio.sleep(self._settings.action_delay)

    async def type_text(self, text: str) -> None:
        """Type a literal string into the focused remote window.

        Newlines and tabs become real Enter/Tab keystrokes; everything else is
        sent as a keystroke for that character.
        """
        for char in text:
            await self._stop.gate()
            if char == "\r":
                continue  # CRLF: the \n does the work
            if char == "\n":
                await self.key("ENTER")
                continue
            if char == "\t":
                await self.key("TAB")
                continue
            await self._type_char(char)
            await asyncio.sleep(self._settings.char_delay)

    async def _type_char(self, char: str) -> None:
        if self._settings.type_mode == "scancode":
            await self._type_char_scancode(char)
            return
        if len(char.encode("utf-16-le")) != 2:
            # A surrogate pair will not fit the 2-byte unicodeCode field.
            raise InputError(
                f"{char!r} is outside the Basic Multilingual Plane and cannot be "
                "sent as a single RDP unicode event."
            )
        await self._send_char(char, True)
        await asyncio.sleep(self._settings.key_delay)
        await self._send_char(char, False)

    async def _type_char_scancode(self, char: str) -> None:
        """Layout-dependent fallback: map the character to a key position."""
        try:
            scancode, modifiers = self._layout.char_to_scancode(char)
        except KeyError:
            raise InputError(
                f"{char!r} has no key position in layout "
                f"{self._settings.keyboard_layout!r}. Use RDP_TYPE_MODE=unicode."
            ) from None

        mod_codes = []
        for flag in VK_MODIFIERS:
            if flag not in modifiers:
                continue
            # Caps-lock in the layout tables just means "shifted".
            name = "VK_LSHIFT" if flag == VK_MODIFIERS.VK_CAPITAL else flag.name
            if name in ("VK_SHIFT", "VK_CONTROL", "VK_MENU"):
                name = name[:3] + "L" + name[3:]
            try:
                mod_codes.append((self._layout.vk_to_scancode(name),
                                  name in EXTENDED_VKS))
            except KeyError:
                logger.debug("No scancode for modifier %s, ignoring", name)

        try:
            for code, ext in mod_codes:
                await self._send_scancode(code, ext, pressed=True)
            # A printable character always sits on the main block, never on the
            # keypad, so it is never an extended key.
            await self._send_scancode(scancode, False, pressed=True, modifiers=modifiers)
            await asyncio.sleep(self._settings.key_delay)
            await self._send_scancode(scancode, False, pressed=False, modifiers=modifiers)
        finally:
            for code, ext in reversed(mod_codes):
                try:
                    await self._conn.send_key_scancode(code, False, ext, VK_MODIFIERS(0))
                except Exception:  # noqa: BLE001 - best effort cleanup
                    logger.debug("Could not release modifier scancode %s", code)

    async def type_lines(self, text: str, editor_safe: bool = True) -> None:
        """Type multi-line text into a code editor.

        Editors re-indent as you type, so typing our own leading whitespace after
        an auto-indent would double it. For each line we therefore select the
        whitespace the editor just inserted (``Home`` then ``Shift+End``) and let
        the line's own text replace the selection. ``Escape`` before each
        ``Enter`` dismisses any completion popup that would otherwise be accepted
        by the newline.
        """
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for index, line in enumerate(lines):
            await self._stop.gate()
            if editor_safe:
                await self.key("HOME")
                await self.chord("shift+END")
                if not line:
                    # Selection (if any) must still be cleared on a blank line.
                    await self.key("DELETE")
            for char in line:
                await self._stop.gate()
                if char == "\t":
                    await self.key("TAB")
                    continue
                await self._type_char(char)
                await asyncio.sleep(self._settings.char_delay)
            if index < len(lines) - 1:
                if editor_safe:
                    await self.key("ESC")
                await self.key("ENTER")

    # ------------------------------------------------------------------- mouse

    async def move(self, x: int, y: int) -> None:
        """Move the remote pointer without pressing anything."""
        self._guard()
        result = await self._conn.send_mouse(MOUSEBUTTON.MOUSEBUTTON_HOVER, int(x), int(y), False)
        self._check_result(result, f"pointer move to ({x}, {y})")

    async def click(self, x: int, y: int, button: str = "left") -> None:
        """Move to ``(x, y)`` and click, in remote screen coordinates."""
        key = button.lower()
        if key not in BUTTONS:
            raise InputError(f"Unknown mouse button {button!r}; use left, right or middle")
        btn = BUTTONS[key]
        await self.move(x, y)
        await asyncio.sleep(self._settings.key_delay)
        self._guard()
        self._check_result(
            await self._conn.send_mouse(btn, int(x), int(y), True), "mouse press")
        await asyncio.sleep(self._settings.key_delay)
        self._check_result(
            await self._conn.send_mouse(btn, int(x), int(y), False), "mouse release")
        await asyncio.sleep(self._settings.action_delay)

    async def double_click(self, x: int, y: int, button: str = "left") -> None:
        await self.click(x, y, button)
        await asyncio.sleep(0.05)
        await self.click(x, y, button)

    async def scroll(self, x: int, y: int, steps: int = 3, up: bool = True) -> None:
        """Turn the wheel at ``(x, y)``; ``steps`` is the number of notches."""
        btn = (MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP if up
               else MOUSEBUTTON.MOUSEBUTTON_WHEEL_DOWN)
        await self.move(x, y)
        for _ in range(max(1, steps)):
            self._guard()
            self._check_result(
                await self._conn.send_mouse(btn, int(x), int(y), True, 120), "mouse wheel")
            await asyncio.sleep(self._settings.key_delay)
        await asyncio.sleep(self._settings.action_delay)
