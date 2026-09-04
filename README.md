# rdp-background-automation

A Windows-only Python client that opens **its own RDP connection** to a Windows
server and drives the remote desktop by sending RDP keyboard and mouse input
events. Your local mouse and keyboard are never touched, so you keep using your
PC while the automation works in the remote session.

---

## 1. Which RDP library, and why

### The requirement

The client had to inject input into an RDP session **it establishes itself**, at
the protocol level. Anything that drives `mstsc.exe` by moving the local cursor,
or that calls `SendInput`/`pyautogui`/`pynput` on the local desktop, was out of
scope by design.

In RDP terms this means the client must be able to emit **client-to-server input
PDUs** — `TS_INPUT_PDU_DATA` carrying `TS_KEYBOARD_EVENT`,
`TS_UNICODE_KEYBOARD_EVENT` and `TS_POINTER_EVENT` (MS-RDPBCGR §2.2.8.1). Many
"RDP libraries" only implement the *server* side, or only screenshot the client
side, so this was the first thing to check.

### Options considered

| Library | Status | Input injection | Verdict |
|---|---|---|---|
| **[aardwolf](https://github.com/skelsec/aardwolf)** | Maintained; 0.2.14 released 2026-06-20 | Yes — full client-side input | **Chosen** |
| [rdpy](https://github.com/citronneur/rdpy) | Unmaintained since ~2016, Python 2 | Partial | Rejected: will not run on modern Python |
| [rdpy3](https://github.com/Nacho-Neko/rdpy3) | Unofficial Python 3 fork, low activity | Partial | Rejected: no CredSSP/NLA, little maintenance |
| [PyRDP](https://github.com/GoSecure/pyrdp) | Actively maintained | Man-in-the-middle oriented | Rejected: proxies/records sessions rather than driving one |
| FreeRDP / `wfreerdp` | The reference native client, very actively maintained | Yes, but no scripting API | Fallback (see below) |
| Apache Guacamole (`guacd`) | Actively maintained | Yes, over the Guacamole protocol | Rejected: needs a Linux/Docker daemon alongside |

### Verification, before any implementation was written

`aardwolf` was installed and its source read to confirm the input path is real
and not a wrapper around local automation. In `aardwolf/connection.py`:

* `send_key_scancode()` builds a `TS_KEYBOARD_EVENT`, wraps it in
  `TS_INPUT_EVENT` / `TS_INPUT_PDU_DATA`, and writes it to the MCS channel.
* `send_key_char()` does the same with a `TS_UNICODE_KEYBOARD_EVENT`.
* `send_mouse()` does the same with a `TS_POINTER_EVENT`, including wheel flags.

These are ordinary protocol messages on the connection this process opened.
There is no window handle, no `SendInput`, and no local cursor anywhere in the
path. The connection also does CredSSP/NLA via NTLM or Kerberos, which modern
Windows requires by default.

**Conclusion: server-side input injection into an independently established RDP
session is supported, so no fallback architecture is needed.**

If you later outgrow this — you need RemoteFX/H.264 decoding, RDP file system or
audio redirection, or multi-monitor — the right move is FreeRDP: build
`wfreerdp` and drive it through a small helper process, or bind
`freerdp_input_send_keyboard_event()` / `freerdp_input_send_mouse_event()` via
`ctypes`. That is strictly more work, and unnecessary for GUI automation.

### One honest caveat

`aardwolf` is a small project maintained by one author, and it decodes only a
subset of RDP's bitmap codecs. That affects **screenshots** (see
[Limitations](#11-limitations-of-background-gui-input)), not input. Input is the
part this project depends on, and it is complete.

---

## 2. Platform support

|  | Local PC (runs this) | Remote target |
|---|---|---|
| Windows 10/11, Server | fully supported | **required** |
| Linux / macOS | core works, see below | not applicable |

The **target must be Windows** — that is what speaks RDP and runs the editor.

The **client** is mostly portable: `aardwolf` has Linux wheels, and the input,
screenshot and codebase code is plain Python. Two things are Windows-only:

* **tkinter needs a display.** On a headless VPS there is no X server, so
  `gui.py` cannot open — use **`cli.py`** instead, which offers the same five
  actions from a terminal and never imports tkinter.
* **Remembering the password uses DPAPI**, which is a Windows API. Off Windows
  it degrades cleanly: the rest of the profile is still saved and the password
  is simply asked for each time, or taken from `RDP_PASSWORD`.

So a headless Linux VPS runs it fine through `cli.py`; `gui.py` needs a
desktop or X forwarding.

## 3. Windows prerequisites

**On the local PC (where this runs)**

* Windows 10/11 or Windows Server. No RDP client needed — this *is* an RDP client.
* Network access to the server on TCP 3389 (or your chosen port).

**On the remote Windows server**

* Remote Desktop enabled (see [section 6](#7-enabling-rdp-on-the-windows-server)).
* An account with the *Allow log on through Remote Desktop Services* right.
* For the codebase actions: **git** on the local PC's `PATH`.
* For the Phase 2 demo: nothing extra — it drives **Notepad** by default.
  Only if you pass `--editor code` do you need VS Code installed with `code` on
  the `PATH`, and Python on the server if you want `--run` to work.

## 4. Python version

**Python 3.13 on 64-bit Windows.** Also fine on 3.9–3.12.

Do **not** use Python 3.14 yet. `aardwolf` depends on `arc4`, which has no
cp314 wheel, so pip falls back to compiling it and fails unless you have MSVC
build tools. Check what you have:

```powershell
py -0p
```

## 5. Dependencies

```
aardwolf==0.2.13     # RDP protocol client -- the only real dependency
```

`aardwolf` pulls in `Pillow` (used here to write PNG screenshots), `cryptography`,
`asyauth`, `asysocks` and `minikerberos`.

Nothing else is needed. `tkinter` (for the input form) ships with the standard
Windows Python installer, and the remembered-credentials store uses Windows
DPAPI through `ctypes`.

The pin matters: **0.2.13 is the newest release with prebuilt Windows wheels**
for CPython 3.9–3.13. 0.2.14 ships as a source archive only and needs a Rust
toolchain to build its RLE decoder extension.

## 6. Installing

```powershell
cd rdp-background-automation

# A 3.13 virtual environment (adjust the path to your 3.13 install)
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

There is no config file to set up. The first run asks for the connection details
and remembers them (see [section 12](#13-configuration)).

Verify the install without touching the network:

```powershell
python -c "import gui, cli; print('ok')"
```

## 7. Enabling RDP on the Windows server

Run in an elevated PowerShell **on the server**:

```powershell
# Allow Remote Desktop connections
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' `
  -Name 'fDenyTSConnections' -Value 0

# Require Network Level Authentication (recommended; matches RDP_AUTH=ntlm)
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' `
  -Name 'UserAuthentication' -Value 1

# Open the firewall
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

# Confirm it is listening
Get-NetTCPConnection -LocalPort 3389 -State Listen
```

Grant the automation account access, using a **dedicated account** rather than
one you also sign in with interactively (see
[section 11](#12-when-the-session-is-minimised-or-disconnected) for why):

```powershell
Add-LocalGroupMember -Group 'Remote Desktop Users' -Member 'automation'
```

Test from the local PC before using this tool:

```powershell
Test-NetConnection -ComputerName SERVER -Port 3389
```

## 8. Running it

There are **two entry points**, one per environment. Every other file is a
module and refuses to run on its own:

```powershell
python gui.py     # desktop: a window
python cli.py     # server:  a terminal, no tkinter
```

Both offer the same five actions, share the same remembered profile, and run
the same code underneath — `run_session` for the demos, `run_codebase_session`
for repositories — so behaviour never drifts between them.

### `cli.py`, for a VPS

```bash
python cli.py                                    # menu, prompts for what is missing
python cli.py demo --editor code
python cli.py repo https://github.com/pallets/click --minutes 30
python cli.py repo https://github.com/me/proj --minutes 0    # no limit
python cli.py test                               # interactive rdp> prompt
python cli.py --forget
```

With no arguments it prompts, offering everything it already knows as a default
you accept with Enter, then shows a numbered menu:

```
What would you like to run?
  1) Connection test        - an interactive rdp> prompt
  2) Editor demo, Notepad   - type a generated Python file
  3) Editor demo, VS Code   - the same, in VS Code
  4) Type a codebase        - clone a git repo, into Notepad
  5) Type a codebase        - clone a git repo, into VS Code
  q) Quit
```

Give it an action on the command line and it stops prompting, so it works from
cron or systemd — set `RDP_PASSWORD` in the environment, or save it beforehand.
Missing values produce a clear error and exit 2 rather than blocking on a
prompt that nobody is there to answer.

Exit codes: `0` success, `1` connection failed, `2` configuration, `3`
emergency stop, `4` session stopped accepting input, `5` input error, `6`
repository problem, `130` interrupted.

There is no live view in the CLI — it is a picture, and a terminal is not. The
screenshots still land in `screenshots/`.

### `gui.py`, for a desktop

A connection form appears, prefilled with whatever was remembered last time.
Pick one of five actions and press **Connect**:

| Action | What it does |
|---|---|
| Connection test | An interactive prompt, in the window |
| Editor demo, Notepad | Types a generated Python file into Notepad |
| Editor demo, VS Code | The same, into VS Code |
| Type a codebase, Notepad | Clones a git repo and types its files |
| Type a codebase, VS Code | The same, into VS Code |

Everything then runs **in this process** and streams into a log window — no
second console is spawned. The window has a log pane, a `rdp>` command box for
the interactive action, a **STOP** button, a **Live view** button, and a
**Screenshots** button that opens the capture folder.

### Live view

**Live view** opens a read-only mirror of the remote desktop, refreshing at
2/5/10 fps. It reads the same decoded frame buffer the screenshots come from
and **sends nothing back** — clicking or typing in that window does nothing to
the remote session; use the command box for that.

It is a picture, not a video codec. The buffer only holds regions the server has
actually sent, and aardwolf decodes a subset of RDP's bitmap encodings, so
anything it cannot decode simply stays as it was. Treat it as a progress
monitor, not a faithful remote display — for that, open `mstsc` alongside.

10 fps costs noticeably more CPU than 2 fps, because each frame is copied,
rescaled and converted for Tk.

Three threads keep that responsive: the Tk thread only touches widgets and polls
a queue; a worker thread drives the session so a long `type` never freezes the
UI; and the asyncio loop the RDP client lives on runs on a third. That is what
lets the STOP button stay clickable while thousands of keystrokes are going out.

`python gui.py --stop` opens just the floating STOP button, useful beside your
own work during a long run.

### The interactive action

On success the log shows:

```
Connected successfully
```

Then type commands into the box at the bottom (Up/Down recalls history):

```
rdp> type Hello from the automation client
rdp> key ENTER
rdp> wait 2
rdp> screenshot
rdp> quit
```

### Commands

| Command | Description |
|---|---|
| `type <text>` | Type text into the focused remote window, verbatim including spaces |
| `key <key>` | Press a key or chord: `ENTER`, `F5`, `ctrl+s`, `ctrl+shift+p`, `alt+F4`, `win+r` |
| `click <x> <y> [button]` | Click at remote coordinates (`left`/`right`/`middle`) |
| `dclick <x> <y>` | Double-click |
| `move <x> <y>` | Move the pointer only |
| `scroll <x> <y> [up\|down] [notches]` | Turn the wheel |
| `wait <seconds>` | Pause locally |
| `screenshot [path]` | Save the remote screen as PNG |
| `status` | Connection and emergency-stop state |
| `keys` | List every key name `key` understands |
| `stop` / `resume` | Trip / clear the emergency stop |
| `help`, `quit` | |

Coordinates are in the remote screen space you configured (`RDP_WIDTH` ×
`RDP_HEIGHT`, default 1280×800), with `0,0` at the top left. Out-of-range
coordinates are rejected rather than silently clamped.

### Special characters and keys

Text is typed as **RDP unicode keyboard events** by default, so the *server*
decides which keystroke produces each character. That makes punctuation like
`@ # $ ^ & * ( ) { } [ ] | \ ~ " '` layout-independent and correct without any
per-character mapping on this side. `\n` becomes a real Enter, `\t` a real Tab,
and `\r` is dropped.

Named keys and modifier chords necessarily use **scancodes**, because a modifier
has no unicode representation. Those carry the **extended-key bit** where a real
keyboard would: the navigation cluster shares scancodes with the numeric keypad
— `Home` and Numpad-7 are both scancode 71, `Delete` and Numpad-decimal are both
83 — and that bit is the only thing telling them apart. Without it, `Home` types
`7` and `Delete` types `.` on any machine with NumLock on. Set `RDP_TYPE_MODE=scancode` to send text that
way too — only useful if a remote application ignores unicode input, and it then
depends on `RDP_KEYBOARD_LAYOUT` matching the remote layout.

Characters outside the Basic Multilingual Plane (emoji, for instance) are
rejected with a clear error: they do not fit RDP's 2-byte unicode field.

## 9. The editor demo (Phase 2)

Pick **Editor demo, Notepad** or **Editor demo, VS Code** in the form.

**Notepad is the default target, and it is the more reliable one.** It has no
auto-indent, no auto-closing brackets, no completion popups and no extensions,
so there is nothing to fight: the demo skips the per-line guard entirely, which
also removes four keystrokes per line and makes typing noticeably faster.
### Keeping the launch step simple

Everything typed into the Run dialog is fire-and-forget: there is no way to read
an error back from it. An earlier version of this demo pre-created the file:

```
cmd /c type nul > "{path}" && notepad "{path}"
```

That is a trap. If the redirection fails — no write permission on the folder,
say — the `&&` short-circuits, the editor never launches, and the `cmd` window
flashes and closes leaving nothing on screen to explain why.

So the Notepad path is now just `notepad`, with no arguments at all: it opens an
Untitled buffer and the full path is typed into the Save As dialog afterwards.
Passing a not-yet-existing path to Notepad would instead raise a "cannot find
the file, create it?" prompt whose wording and buttons vary between Windows
builds. VS Code is happy to open a path that does not exist and create it on
save, so it keeps a direct `Ctrl+S`.

If a launch ever does fail, `screenshots\demo-1-editor.png` is taken straight
after it and will show you what is actually on the remote screen.

### Waiting for the session to be usable

A newly created RDP session is **not ready for input straight away**. Explorer
still has to start and paint the shell, and until it does, `Win+R` is silently
dropped — the shell is what implements it. Keystrokes sent in that window land
in whatever grabs focus first, which on a fresh login is often a startup app.

There is no readiness signal in the RDP protocol, so the demo infers one from
the screen: it waits for the first frame, then samples a downscaled copy of the
desktop until it stops changing (`--startup-timeout`, default 45s, then
`--startup-wait` extra seconds). A small pixel tolerance keeps the taskbar clock
and a blinking caret from counting as "still changing". On timeout it carries on
and falls back to the fixed delays rather than giving up.

This is also why `demo-0-before.png` is captured *after* that wait — taken
before it, it is just a black rectangle.

Running the file is **off by default**; pass `--run` to execute it.

What it does:

1. Connects.
2. **Waits for the remote desktop to finish starting up**, then screenshots it.
3. Generates a small harmless Python module — a few functions, a loop, a list
   comprehension, some random numbers.
4. Opens the Run dialog with `Win+R` and launches the editor.
5. **Types the code in.**
6. Saves it as
   `C:\Users\Public\Documents\automation_demo_<timestamp>.py` — for Notepad by
   typing that path into the Save As dialog, for VS Code with a plain `Ctrl+S`.
   The timestamp means a run never has to answer an overwrite prompt.
7. With `--run`, executes it (`cmd /k` for Notepad, the integrated terminal for
   VS Code) so the output stays on screen for the final screenshot.
8. Screenshots each stage into `screenshots\` and disconnects cleanly.

With `--editor code`, VS Code is launched with
`--disable-extensions --disable-workspace-trust`, because IntelliSense and
auto-formatting rewrite text as it arrives and the trust dialog would swallow
the first keystrokes. **Those flags only apply when VS Code starts a new
instance** — if VS Code is already running in the remote session your `code`
command is forwarded to that instance and the flags are ignored. Close it there
first, or use Notepad.

### How typed code survives the editor

Editors fight you when you type code. Two behaviours matter:

* **Auto-indent.** After Enter, the editor inserts its own indentation. Typing
  your own leading spaces on top of that doubles them, and by line 20 the file
  is unparseable.
* **Auto-closing brackets and quotes.** Typing `(` inserts `()`.

`InputSender.type_lines(editor_safe=True)` handles both, and the demo enables it
only for VS Code. Before each line it presses `Home` then
`Shift+End`, which selects exactly the whitespace the editor just inserted; the
line's own text then replaces that selection. `Escape` before each `Enter`
dismisses any completion popup that would otherwise be accepted by the newline.
Auto-closed brackets take care of themselves, because typing the matching closer
types *over* the inserted one.

The generated code is also written to cooperate: spaces not tabs, no
backslashes, brackets and quotes balanced within each line, and comments instead
of `"""` docstrings — a third consecutive quote is exactly where auto-closing
stops being predictable.

This was verified offline against a simulated editor that applies auto-indent
and auto-closing: 25 different generated modules round-trip **byte for byte**,
and the same input with the strategy disabled visibly corrupts indentation.

## 10. Typing a codebase

Pick **Type a codebase** in the form, give it a git URL and a minute budget.
It shallow-clones the repository locally, selects the files worth typing, and
drives the editor through them one at a time — creating folders, opening each
file, typing it, saving it.

### Read this before you start one

**Typing is slow, and that is not a bug — it is the medium.** Every character
is an RDP input event and the remote UI has to keep up, so throughput is about
20–25 characters a second, roughly **1 KB a minute**:

| Repository | Realistic time |
|---|---|
| 30 KB | ~30 minutes |
| 450 KB (e.g. `pallets/click`) | **~7 hours** |
| 1 MB+ | overnight |

So this is not a way to get code onto the server. If you want the repo there,
run `git clone` on the server — it will take seconds. This exists to drive a
real editor through a real codebase, which is the interesting automation
problem.

### The time budget

Because of that, the work is bounded by the **Minutes (0 = no limit)** field.
Set it to `0` to type the entire repository however long that takes; set any
other number and it estimates each file from your actual
`char_delay`/`key_delay`/`action_delay` settings, takes files in order until the
budget runs out, and reports what it left behind:

```
[2] Selecting files
    124 candidate file(s), 455,066 characters
    18 skipped, first few:
      - package-lock.json: lock file
      - docs/_static/click.png: not UTF-8 text
[3] Typing runs at roughly 20-25 characters a second, so the whole
    repository would take 7.0 hours.
    Budget is 10 min: typing 12 file(s), about 10 min.
    112 file(s) left out; raise the budget to include more.
```

### What gets skipped

Binaries, lock files, anything over 20 KB, non-UTF-8 files, and anything
containing characters outside the Basic Multilingual Plane (emoji) — RDP's
unicode keyboard event carries a single UTF-16 code unit, so those cannot be
typed at all. Build and dependency directories (`.git`, `node_modules`,
`__pycache__`, `dist`, `venv` …) are ignored outright.

Files are ordered so that **real source comes before dot-directory
housekeeping** — otherwise a short budget would be spent entirely on
`.github/ISSUE_TEMPLATE` and never reach the code.

### One editor window, not a hundred

Opening each file with `code -n` would be a memory bug, not a style choice:
`-n` means *new window*, every VS Code window is an Electron renderer at
roughly 150 MB, and a hundred-file repository would exhaust the server.

Alt+F4 after each file would fix the memory but cost a **12-second cold start
per file** — on a 100-file run that is 20 minutes of nothing but VS Code
launching.

So the codebase path uses a different launch command from the one-file demo:

| | Launch | Cleanup |
|---|---|---|
| Editor demo (one file) | `code -n -g <path>` — a clean window | none |
| Codebase (many files) | `code -r -g <path>` — **reuse the open window** | `Ctrl+W` closes the tab |
| Codebase, Notepad | `notepad` | `Alt+F4` — Notepad is cheap to restart |

`-r` (`--reuse-window`) hands the file to the instance that is already running,
so only the first file pays the cold start and the rest wait `relaunch_wait`
(3s) instead of 12s. One window, one tab at a time, flat memory.

The catch worth knowing: `-r` reuses **whatever VS Code window is already open**
in that session, including one you were working in. `Ctrl+W` only closes the
tab it just opened, but if you would rather it not touch your window at all,
close VS Code in the remote session before starting a codebase run.

### Where it lands

`C:\Users\Public\Documents\<repo-name>\` in the remote session, preserving the
directory structure. Notepad cannot create folders from its Save As dialog, so
each new directory is made first with a `cmd /c mkdir`; VS Code creates them
itself on save.

## 11. Limitations of background GUI input

**What this genuinely gives you.** The remote session is a real interactive
Windows session, separate from your local desktop. Input arrives through the
server's RDP stack exactly as `mstsc.exe`'s would, so remote applications cannot
tell the difference. Nothing is queued against a hidden window; there is no
local window at all. You can use your PC normally throughout.

**One interactive session per user, per machine.** This is the big one.

* On **Windows Server**, multiple concurrent RDP sessions need the Remote
  Desktop Session Host role and RDS licensing. Without it you get the built-in
  two-administrator limit.
* On **Windows 10/11 Pro**, there is only *one* interactive session. Connecting
  with this tool **locks or disconnects whoever is at the physical console.**
  If your "server" is a Pro machine, expect that.
* If the account you connect as is *already* signed in (at the console or over
  RDP), Windows reconnects you to that existing session instead of creating a
  new one — and your automation shares a desktop with that user. Use a dedicated
  automation account to get a session of your own.

**Other constraints.**

* **No text or element introspection.** RDP delivers bitmaps. There is no UI
  automation tree, so you cannot query a control's state — you send input at
  coordinates and verify with screenshots. Coordinates are brittle; prefer
  keyboard navigation, which this project does throughout.
* **Screenshots may be incomplete.** `aardwolf` decodes a subset of RDP's bitmap
  codecs, and the desktop buffer only contains regions the server has actually
  sent. Blank or partial screenshots are a decoding/update-coverage gap, not an
  input failure. Screenshots are for eyeballing progress, not for pixel
  assertions.
* **The secure desktop is not yours to drive.** `Ctrl+Alt+Del` cannot be sent as
  ordinary scancodes. UAC elevation prompts switch the session to a secure
  desktop; interaction over RDP can work but timing and screen updates become
  unreliable. Design flows that never need elevation.
* **Timing is unavoidable.** There is no "wait until the window is ready"
  signal, only sleeps. Everything is configurable for this reason; slow servers
  need bigger values.
* **No clipboard channel at all.** Pasting would be far faster than typing, but
  the clipboard channel is not joined — and that is now a correctness
  requirement, not a preference. `aardwolf`'s RDPECLIP handler raises
  `AttributeError: 'NoneType' object has no attribute 'datatype'` when the
  server sends a `FORMAT_DATA_REQUEST` and no clipboard data has been set, and
  an exception in its channel reader runs `terminate()` in a `finally` block.
  A server politely asking for the clipboard would therefore kill the session
  mid-automation. With no virtual channels joined the bug cannot fire, and your
  local clipboard is never touched either.

## 12. When the session is minimised or disconnected

**Minimised: not applicable, and that is the point.** The classic failure —
"minimise the RDP window and GUI automation stops, because Windows stops
rendering the desktop and the client stops processing input" — comes from
automating `mstsc.exe`'s *window*. This client has no window. Nothing can be
minimised, and there is no local UI in the input path.

**Disconnected: input stops, and you are told.** If the TCP connection drops, or
the server ends the session, or an administrator uses *Log off* / *Disconnect*:

* `aardwolf` sets `disconnected_evt`, and every subsequent input call raises
  `SessionNotAcceptingInput` with a plain-language message. Nothing is sent into
  the void.
* the interactive prompt notices and exits; a demo or codebase run aborts,
  reports which step failed, and still disconnects cleanly.
* Retries apply to *establishing* the connection (`RDP_CONNECT_RETRIES`), not to
  reconnecting mid-run. Reconnecting mid-sequence would resume against unknown
  remote UI state, so the tool stops instead of guessing.

What happens to the session itself is Windows' decision. A disconnected session
keeps running by default: VS Code stays open and your file stays where it was.
It becomes non-interactive, though — the desktop is no longer composited, so
some applications stop repainting and screen-scraping would be meaningless until
something reconnects. Reconnect with this tool or `mstsc` and you land back in
the same session. If the server has *End a disconnected session* configured,
the session is destroyed instead and your remote work is lost.

**Emergency stop.** Two independent local mechanisms, both of which cut input
without waiting for the network:

* **Create the stop file.** From any shell, on your local PC:
  ```powershell
  New-Item STOP
  ```
  Every input call checks for it (at most every 200 ms) and refuses to send.
  Delete it, or run `resume`, to continue.
* **Press Ctrl+C**, or use the runner window's STOP button. At an interactive
  prompt this aborts the running command and keeps the session; press it again
  to quit. During a demo or codebase run it aborts the sequence and disconnects
  cleanly. Modifier keys held down mid-chord are always released
  on the way out, so the remote session is never left with a stuck `Ctrl`.

Both are local and synchronous: they stop this process from *sending*, so they
work even if the server has stopped responding.

## 13. Configuration

There is no config file in the project. Values resolve in this order, first
match wins:

1. **environment variables** (`RDP_HOST` and friends) — handy for scripting
2. **the remembered profile** — written after a successful connection
3. **a prompt** — the console, or the tkinter form with `--gui`

### Remembered credentials

After a connection succeeds, the host, port, username, domain and screen size
are saved to:

```
%LOCALAPPDATA%\rdp-background-automation\profile.json
```

That is deliberately **outside the project**, so it cannot be committed by
accident. Next run, those values are prefilled — press Enter at each console
prompt to accept them, or edit them in the form.

The **password is not saved unless you ask for it**:

Tick **Remember password** in the form to save it, and **Forget saved** to
clear everything remembered.

When saved, the password is encrypted with **Windows DPAPI**
(`CryptProtectData`), which ties the ciphertext to your Windows user account:
another account on the machine, or the same file copied elsewhere, cannot
decrypt it. If decryption fails you simply get asked for the password again.

Be clear-eyed about what that buys you: it stops the password sitting in
plaintext, but anything running *as you* can still decrypt it. Leave the option
off if that matters, and use `RDP_PASSWORD` or the prompt instead.

Override the profile location with `RDP_PROFILE=<path>`.

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `RDP_HOST`, `RDP_PORT` | — / `3389` | Prompted if unset |
| `RDP_USERNAME`, `RDP_DOMAIN` | — / empty | Domain optional |
| `RDP_AUTH` | `ntlm` | `ntlm` (CredSSP/NLA), `kerberos`, or `plain` |
| `RDP_WIDTH`, `RDP_HEIGHT` | `1280`, `800` | Also the `click` coordinate space |
| `RDP_CONNECT_TIMEOUT` | `20` | Seconds per attempt |
| `RDP_CONNECT_RETRIES` | `3` | Attempts before giving up |
| `RDP_RETRY_DELAY` | `3` | Seconds between attempts |
| `RDP_CHAR_DELAY` | `0.012` | Between characters |
| `RDP_KEY_DELAY` | `0.03` | Between key down and up |
| `RDP_ACTION_DELAY` | `0.25` | After a click, chord, or Enter |
| `RDP_TYPE_MODE` | `unicode` | Or `scancode` |
| `RDP_KEYBOARD_LAYOUT` | `enus` | Only used by `scancode` mode |
| `RDP_SCREENSHOT_DIR` | `screenshots/` | |
| `RDP_STOP_FILE` | `STOP` | Emergency stop sentinel |
| `RDP_PROFILE` | `%LOCALAPPDATA%\...` | Where remembered details are stored |
| `RDP_LOG_LEVEL`, `RDP_LOG_FILE` | `INFO` / none | |

Passwords are percent-encoded into the connection URL, so `@ : / # ? &` in a
password are safe. `repr(Settings)` omits the password and logged URLs mask it.

## 14. The tkinter front end

```powershell
python gui.py            # connection form + pick what to run
python gui.py --stop     # just the floating STOP button
```

The form collects host, port, username, domain, password and screen size,
prefilled from the remembered profile, plus the repo URL and minute budget for
the codebase actions, a "Remember password" tick box, and the choice of what to
run. **Forget saved** clears the stored profile.

**Connect** then opens the runner window, which executes everything in this
process. An earlier version spawned a second console instead; running in-process
is better because the log is one scrollable pane you can search, and the STOP
button sits right next to it.

Output reaches the log pane by pointing `sys.stdout`/`sys.stderr` at a queue
while a run is active, so the ordinary `print` calls throughout the code need no
changes. That redirect is thread-aware: writes from the Tk thread pass through
to the real console, and only the session's threads are captured — `sys.stdout`
is process-global, and capturing it unconditionally swallowed output that had
nothing to do with the run.

`python gui.py --stop` opens a small always-on-top window with one big button
that creates and removes the emergency stop file. It talks to nothing — it only
touches that file — so it works even if the automation process is wedged, and
you can leave it up beside your work while a long run is going.

## 15. Troubleshooting

**`error: Microsoft Visual C++ 14.0 or greater is required` or `can't find Rust compiler` during `pip install`**
You are on Python 3.14, or pip is trying to build `aardwolf` from source. Use
Python 3.13 and the pinned `aardwolf==0.2.13`, which has a Windows wheel.

**`The remote computer refused the network connection` (WinError 1225)**
Nothing is listening. Check `fDenyTSConnections`, the firewall rule, and the
port: `Test-NetConnection -ComputerName SERVER -Port 3389`.

**Connection times out**
The host is unreachable or filtered. Verify the address, and check for a
network-level firewall between you and the server.

**Authentication fails / `STATUS_LOGON_FAILURE` / `0xc000006d`**
Check username, password and domain. Use the domain field rather than
`DOMAIN\user` in the username. Confirm the account is in *Remote Desktop Users*
and has the *Allow log on through Remote Desktop Services* right.

**CredSSP or security-negotiation errors**
Try `RDP_AUTH=plain` to isolate it — if plain works, the problem is NLA. Mismatched
CredSSP patch levels between client and server ("encryption oracle
remediation") show up here; patch both sides rather than weakening the policy.
Use `RDP_AUTH=kerberos` for a domain account when NTLM is disabled.

**Connected, but keystrokes go nowhere**
Something else has focus in the remote session. Screenshot first, then click to
focus. Remember the session starts with no window focused if nothing auto-ran.

**Characters are wrong, doubled, or dropped**
Raise `RDP_CHAR_DELAY` and `RDP_KEY_DELAY` — a loaded server drops input that
arrives faster than it drains its queue. If specific punctuation is wrong you
are probably in `scancode` mode with a mismatched layout; use
`RDP_TYPE_MODE=unicode`.

**Stray digits appear before every line (`7`, `7.`, `71`)**
The extended-key bit is missing from navigation keys, so `Home`/`Delete` are
arriving as Numpad-7 and Numpad-decimal. This is fixed — `EXTENDED_VKS` in
`input_events.py` lists the keys that need it. If you add a key to
`KEY_ALIASES` from the navigation cluster or the keypad, add it there too.

**Typed code comes out badly indented**
Something is re-indenting as you type. Confirm VS Code launched with
`--disable-extensions`, and raise `--action-delay` so the `Home`/`Shift+End`
sequence is not racing the editor.

**The editor never opens and the keystrokes went somewhere else**
The session was still starting up. This is what `--startup-timeout` /
`--startup-wait` exist for; raise `--startup-wait` if the server is slow or has
startup apps that steal focus. `demo-0-before.png` being an almost-empty file
is the tell-tale sign that the desktop had not painted yet.

**VS Code does not open in the demo**
`code` is not on the server's `PATH`. Test in the remote session with
`code --version`, reinstall with the "Add to PATH" option, or pass a full path.
Also raise `--editor-wait`; first launch is slow.

**Screenshots are blank or partial**
An `aardwolf` bitmap-decoding gap, or the server has not sent those regions
yet. Cause some remote activity and retry. Input is unaffected.

**`No screen data received yet`**
The server has sent no frame at all. Wait, or trigger remote activity, then
retry.

**Your local session gets disconnected when the tool connects**
The target is Windows 10/11 Pro, which allows one interactive session, or you
connected as the account already signed in at the console. Use a dedicated
automation account, or a Windows Server SKU.

**Input stops mid-run with "session has disconnected"**
The connection dropped or the session was ended server-side. Check network
stability and the *End a disconnected session* / idle-timeout policies.

**`AttributeError: 'NoneType' object has no attribute 'datatype'` from `RDPECLIP/channel.py`**
An `aardwolf` clipboard bug that also tears down the session. It cannot happen
with the shipped configuration, which joins no virtual channels. If you see it,
something has re-enabled `iosettings.channels` — put it back to `[]` in
`rdp_client._io_settings()`.

**It remembered the wrong host or username**
Press **Forget saved** in the connection form, or just correct the fields.

**"Saved password could not be decrypted"**
The profile was written by a different Windows user or on a different machine;
DPAPI will not decrypt it there. You are prompted for the password instead —
reconnect with `--remember` to re-save it.

**`git is not on PATH`**
Install Git for Windows. Only the codebase actions need it.

**A codebase run says it would take hours**
It would — see [section 9](#10-typing-a-codebase). Lower the minute budget, or
accept that only part of the repository gets typed. If you actually want the
files on the server, run `git clone` there instead.

**A repository types nothing**
Every file was filtered out: binaries, files over 20 KB, non-UTF-8, or emoji
(which RDP's unicode key event cannot carry). The log lists the reason per file.

**Everything hangs**
`python gui.py --stop`, `New-Item STOP` in the project directory, or close the
runner window — closing it trips the stop and disconnects cleanly.

Run with `RDP_LOG_LEVEL=DEBUG` for protocol-level detail, and
`RDP_LOG_FILE=run.log` to keep it.

## 16. Building autoRDP.exe

```powershell
.\build.ps1                  # single autoRDP.exe in dist\
.\build.ps1 -OneDir          # a folder that starts far faster
.\build.ps1 -Clean           # after changing the spec or a dependency
```

The script creates the virtual environment if missing, installs dependencies,
regenerates the icon when `assets/icon-source.png` has changed, builds, and then
**launches the exe to confirm it opens a window**.

`autoRDP.exe` is the **GUI only**. The spec's entry point is `gui.py`, so
`cli.py` is unreachable and PyInstaller does not bundle it — the packaged
first-party modules are `gui` plus the `rdpauto` package, nothing else. On a
server you run `cli.py` from a checkout rather than from the exe, which is the
right shape anyway: a headless box has no display for the window the exe opens.

That last step earns its keep. A PyInstaller build can succeed and still produce
an exe that dies on startup, because this dependency tree imports things
dynamically and static analysis cannot see them. Two such bugs were found this
way and are now handled in `autoRDP.spec`:

* `aardwolf` loads keyboard layouts with
  `importlib.import_module('aardwolf.keyboard.layouts.layout_KBDUSX')`. Without
  `collect_submodules`, the app starts, connects, and then fails on the first
  keystroke with *Unknown keyboard layout 'enus'*.
* `unicrypto` picks its crypto backend at import time, reaching
  `Cryptodome.Util.Counter`. Without `Cryptodome` collected, the exe dies before
  the window appears.

A third trap is in the app rather than the spec: `PROJECT_ROOT` normally comes
from `__file__`, which in a one-file bundle points inside a temporary directory
that is deleted on exit. `config._project_root()` detects `sys.frozen` and
anchors to the executable instead, so the STOP file and `screenshots\` stay
next to the exe — a stop file written into a vanishing temp directory would
never be seen.

| | One file | One folder |
|---|---|---|
| Ship | a single 32 MB `autoRDP.exe` | the whole `dist\autoRDP\` folder (66 MB) |
| Time to window | **~12 s** | **~1 s** |
| Why | unpacks the whole archive to a temp directory on every launch | nothing to unpack |

Those figures are measured, not estimated: three launches each, timing from
`Start-Process` until the window has a title. One-file averaged 12.0 s
(13.9 / 11.0 / 11.2) and one-folder 1.1 s (1.2 / 0.9 / 1.1). The build script's
own smoke test tends to report a friendlier number for one-file, because it
runs immediately after the build while the archive is still hot in the disk
cache — treat 12 s as the number a user will actually see.

### Reading the build output

The script keeps PyInstaller quiet. Its output goes to
`build" + BS + "pyinstaller-err.log` and only a count reaches the screen:

```
[4] Building
    6 benign warning(s) from dependencies, logged to build\pyinstaller-err.log
```

Those six are the same every time, and none of them is actionable:

* `unicrypto.backends.pycryptodomex` cannot be introspected because of a
  circular import inside it (twice). The modules are still collected via
  `Cryptodome`, which the self-check confirmed at runtime.
* `arc4` is a single C extension module rather than a package, so there is no
  package data or DLL directory to walk (twice).
* `pycparser.lextab` / `yacctab` do not exist on disk -- cffi generates them at
  runtime.

`PYTHONWARNINGS=ignore` is set for the build, which also removes the dozens of
`UserWarning` lines that `Cryptodome`'s self-test suite emits merely from being
imported during analysis. If PyInstaller ever does fail, the script prints the
last 25 log lines and the log path instead of a bare error.

**Both builds take about the same time to produce** (~40 s), and `-Clean` costs
nothing measurable here (40 s either way). PyInstaller re-runs its analysis on
every build regardless, and for this project that analysis plus writing the
archive dominates, so the cache it keeps in `build/` saves very little. Use
`-Clean` freely.

`ONEFILE` at the top of `autoRDP.spec` is the switch; `-OneDir` flips it without
touching the tracked file. UPX compression is deliberately off — it roughly
doubles the rate at which antivirus flags a PyInstaller exe.

Put the exe somewhere writable. It keeps `STOP` and `screenshots\` beside
itself, which will not work under `C:\Program Files`.

## 17. Project layout

```
rdp-background-automation/
├── gui.py               ENTRY POINT - desktop: form, runner window, live view
├── cli.py               ENTRY POINT - server: menu and flags, never loads tkinter
├── build.ps1            ENTRY POINT - one-click release build
│
├── rdpauto/             the application; nothing here runs on its own
│   ├── config.py        settings resolution, secure password prompt, logging
│   ├── credentials.py   remembers the connection, DPAPI-encrypts the password
│   ├── input_events.py  keyboard/mouse RDP input events, emergency stop
│   ├── rdp_client.py    connect, retry, screenshot, live frames, disconnect
│   ├── console.py       command dispatch and the asyncio loop thread
│   ├── session.py       editor profiles and the flows both front ends run
│   ├── codebase.py      git clone, file selection, time budgeting
│   └── code_generator.py   the harmless Python module the demo types
│
├── assets/
│   ├── icon-source.png  the original artwork
│   ├── autoRDP.ico      generated by make_icon.py, embedded in the exe
│   └── autoRDP-256.png  generated too, handy for docs and shortcuts
│
├── autoRDP.spec         PyInstaller recipe (ONEFILE switch at the top)
├── make_icon.py         keys the white background out of the artwork
├── requirements.txt
├── .gitignore
└── README.md
```

Three entry points, one package. Everything under `rdpauto/` refuses to run
directly and says which entry point to use instead. The two front ends are
thin — they collect settings and call the same shared coroutines
(`session.run_session`, `session.run_codebase_session`, `console.dispatch`) — so
there is no second implementation to keep in step.

Two modules were renamed when the package was introduced, because their names
had stopped being true: `main.py` was never the entry point (it holds the
command dispatch) and `demo.py` had grown past the demo into the shared session
flows. They are `console.py` and `session.py` now.


## 18. Scope

This drives machines you administer, from an account you own, for GUI
automation and testing. It needs valid credentials and a server configured to
accept Remote Desktop connections — it is a Remote Desktop client, with the same
access as any other. Don't point it at systems you are not authorised to use.
