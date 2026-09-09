# autordp

A client that opens **its own RDP connection** to a Windows machine and drives
the remote desktop by sending RDP keyboard and mouse input events. Your local
mouse and keyboard are never touched, so you keep using your PC while the
automation works in the remote session.

```sh
npm install -g @ayushshukla8920/autordp     # a prebuilt binary; no Python needed
autordp doctor
```

It does two things:

| | |
|---|---|
| **`autordp connect`** | Test the connection, then drive the session from an `rdp>` prompt — type, key, click, screenshot. |
| **`autordp repo <url>`** | Clone a git repository and type its files into a remote editor. |

Add `--view` to either and watch the remote screen in your browser.

The *target* must be Windows — that is what speaks RDP and runs the editors.
The client runs on Windows, Linux and macOS; see
[section 2](#2-platform-support).

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


The **target must be Windows** — that is what speaks RDP and runs the editor.

The **client is portable**. `aardwolf` has wheels for Windows, Linux and macOS,
and the input, screenshot, live-view and codebase code is plain Python and
standard library. The npm packages ship a prebuilt binary for all three. There
is no display requirement anywhere: the live view is served over HTTP to a
browser that can be somewhere else entirely, which is what makes a headless VPS
a perfectly ordinary place to run this from.

One thing is Windows-only, and it degrades cleanly:

* **Remembering the password uses DPAPI**, a Windows API that encrypts the
  secret against your Windows account. Off Windows the rest of the profile is
  still saved and the password is asked for each time, or taken from
  `RDP_PASSWORD`, or piped in with `--password-stdin`.


## 3. Windows prerequisites


**On the local PC (where this runs)**

* Windows 10/11 or Windows Server. No RDP client needed — this *is* an RDP client.
* Network access to the server on TCP 3389 (or your chosen port).

**On the remote Windows server**

* Remote Desktop enabled (see [section 6](#7-enabling-rdp-on-the-windows-server)).
* An account with the *Allow log on through Remote Desktop Services* right.
* For the codebase actions: **git** on the local PC's `PATH`.
* For `autordp repo`: nothing extra — it drives **Notepad** by default.
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


One dependency, and it brings one of its own that matters:

```
aardwolf==0.2.13      the RDP client
  └── Pillow          arrives with it; turns the desktop buffer into PNG and JPEG
```

Everything else is the standard library. The terminal rendering — colour,
spinners, progress bars, tables — is a few hundred lines of ANSI escapes rather
than `rich`, and the live view is `http.server` rather than a web framework.
Both are deliberate: every dependency added here is also a dependency
PyInstaller has to bundle into the binary that npm ships on five platforms, and
neither would have earned its megabytes. The remembered-credentials store
reaches DPAPI through `ctypes`.

The pin matters: **0.2.13 is the newest release with prebuilt wheels** for
CPython 3.9–3.13. 0.2.14 ships as a source archive only and needs a Rust
toolchain to build its RLE decoder extension — which, across a five-platform CI
matrix, is exactly where things go wrong.


## 6. Installing


Three ways in, in order of how little they ask of you.

### npm — no Python at all

```sh
npm install -g @ayushshukla8920/autordp
autordp doctor
```

This is the `opencode` model. `@ayushshukla8920/autordp` is a tiny launcher
whose `optionalDependencies` list one package per platform, each holding a
PyInstaller build of this repository and declaring its own `os` and `cpu`. npm
skips every package that does not match the machine, so exactly one ~29 MB
binary is downloaded and the launcher hands over to it. Nothing is compiled and
no interpreter is needed.

```sh
npx @ayushshukla8920/autordp doctor    # without installing anything at all
```

Prebuilt for **Windows x64**, **Linux x64**, **Linux arm64** and **macOS Apple
silicon**. Windows on ARM uses the x64 build under emulation.

**macOS Intel is not built.** The RDP library ships no macOS x86_64 wheel, and
GitHub retired the Intel macOS runners, so there is nowhere to compile one --
see [section 18](#18-publishing). `pip install autordp` works there.

### pip

```sh
pip install autordp
autordp doctor
```

### From a checkout

```powershell
git clone https://github.com/ayushshukla8920/autoRDP
cd autordp

# A 3.13 virtual environment (adjust the path to your 3.13 install)
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -e .
```

Then `autordp`, or `python -m autordp` without installing.

There is no config file to set up. `autordp config set` asks for the connection
details, checks them by connecting, and only then remembers them (see
[section 14](#14-configuration)).

### Checking the install

```sh
autordp doctor
```

`doctor` is the thing to run when something is wrong. It walks the same path a
real run takes — runtime, the bundled RDP stack, the keyboard layout tables,
git, the saved profile, the writable directories, then whether the server's RDP
port actually answers — and exits non-zero if any of it would stop a run:

```
Environment check
────────────────────────────────────────
  ✔ runtime          autordp 0.1.0, bundled binary
  ✔ platform         linux (posix)
  ✔ rdp stack        aardwolf 0.2.13
  ✔ keyboard layout  enus loaded, tables populated
  ✔ git              /usr/bin/git
  ✔ profile          Administrator@10.0.0.4
  ✘ rdp port         10.0.0.4:3389 -- timed out
      check the server is up, RDP is enabled, and the firewall allows TCP 3389
```

Two of those lines are about the *build* rather than this machine, and they earn
their place in a packaged one. aardwolf reaches its 400-odd keyboard layout
modules through `importlib`, which PyInstaller's static analysis cannot see, so
a bad build starts fine, connects fine, and then fails on the first keystroke —
possibly twenty minutes into a run. `doctor` loads a layout and resolves a
scancode through it, and CI asserts on the result before publishing anything.

The port probe is a plain TCP connect. It is there because a refused connection
and a rejected password produce similar-looking errors from the RDP library, and
this tells them apart in a second.

`--json` gives the same report as data, on stdout, with the narration moved to
stderr:

```sh
autordp doctor --json | jq '.checks[] | select(.status != "ok")'
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


```sh
autordp                          # interactive menu
autordp connect                  # test the connection, then an rdp> prompt
autordp repo <git-url> -m 30     # clone a repository and type it

autordp doctor                   # check everything a run depends on
autordp config set               # verify a connection, then remember it
autordp keys ctrl                # which key names `key` understands
```

Every command takes `--help`. Connection flags work before or after the
subcommand, so `autordp --host box repo <url>` and
`autordp repo <url> --host box` are the same thing.

### Why not the old grammar

This replaces `cli.py [test|demo|repo] [url] --editor X`, where the action and
the editor multiplied into five hidden internal names (`demo-notepad`,
`repo-code`, …) and the second positional only meant anything for one of the
three actions. `--help` could not say that `--minutes` applies to `repo` alone,
because argparse had no way to know. It does now, because there is one
subparser per action and `--minutes` is attached to exactly one of them.

### Running unattended

Give it a subcommand and it never prompts. Add `--no-input` to be certain:
anything missing then fails immediately with a message naming the flag to pass,
rather than blocking on a prompt nobody will answer.

```sh
autordp connect -c "key win+r" -c "type notepad" -c "key ENTER"
autordp connect --script session.txt      # one command per line, # comments
echo "$RDP_SECRET" | autordp --password-stdin --no-input \
    connect -c "screenshot health.png"
```

`-c` commands stop at the first failure. That is the right default for a
script: the commands are almost always a sequence where each assumes the last
worked, so carrying on after a failed click would type into whatever happened to
have focus.

There is deliberately **no `--password` flag**: an argument is visible in `ps`
and in shell history. Use `RDP_PASSWORD`, `--password-stdin`, or save one with
`autordp config set --remember-password`.

Exit codes: `0` success, `1` connection failed, `2` configuration, `3`
emergency stop, `4` session stopped accepting input, `5` input error, `6`
repository problem, `130` interrupted.

### Output

Colour, a spinner while connecting, and a progress bar with a live rate and ETA
while typing — which matters when the honest answer to "how long is this going
to take" is measured in hours:

```
[6/6] Typing into C:\Users\Public\Documents\click
  typing ▕████████░░░░░░░░░░░░░░░░░░░░░░▏  27%  26.1k/96.9k  22/s  ETA 53m18s  7/40  src/click/core.py
```

The bar counts **characters, not files**. File sizes in a repository vary by two
orders of magnitude, so a per-file bar would sit at 3/40 for twenty minutes and
then jump; characters are what actually cost time, so the bar moves smoothly and
its ETA is worth reading.

All of it degrades on its own: no colour when the output is not a terminal or
`NO_COLOR` is set, ASCII glyphs when the console's code page cannot encode box
drawing, and a plain line every 10% instead of a redrawing bar when the output
is a file. `--color`, `--ascii` and `--quiet` override the detection, and
`--json` puts machine-readable output on stdout while moving narration to
stderr.

### The interactive menu

Running `autordp` with no arguments shows two choices, plus the two utilities
worth reaching for by hand:

```
autordp  v0.1.0
────────────────────────────────────────
    drives a Windows machine over RDP, in a session it opens itself

  1  Test connection     connect, then drive the session from an rdp> prompt
  2  Type a codebase     clone a git repository and type it into a remote editor
  d  Check this machine  runtime, credentials, whether the port answers
  s  Save connection     verify details, then remember them
  q  Quit
```

The editor and the repository URL are asked for afterwards rather than
multiplied into the list. Whatever you pick, it prints the equivalent command
line before running it —

```
    same as: autordp repo https://github.com/pallets/click --minutes 30 --editor code
```

— and then runs it *through the real parser*, so that printed line is
guaranteed to be a command you could have typed yourself.

A numbered list rather than an arrow-key selector, deliberately. A full-screen
TUI needs raw terminal mode, and the two places this is most often started from
— a Windows console inside an existing RDP session, and a plain VPS shell — are
where raw mode is least reliable. A numbered list works in both, works in a
pipe, and can be read out of a screenshot when something has gone wrong.


## 9. The live view


The remote screen, in a browser, while the run happens:

```sh
autordp connect --view              # http://127.0.0.1:8900
autordp repo <git-url> --view 9000  # or pick a port
```

```
  ✔ live view at http://127.0.0.1:8900/
```

The stream is **MJPEG** — `multipart/x-mixed-replace`, one JPEG after another on
a single response. That is an old format, and it is the right one here: every
browser renders it in a plain `<img>` tag with no JavaScript, no WebSocket, no
MediaSource and no negotiation. A desktop that changes in bursts and then sits
still also suits it, because a frame is only pushed when the screen actually
changes — an idle session costs close to nothing, which matters when the tool is
typing for an hour and the screen changes one line at a time.

Four routes, and nothing else:

| Route | What it is |
|---|---|
| `/` | the page: the stream, the target, a connected/stopped indicator |
| `/stream` | the MJPEG stream itself |
| `/frame.jpg` | a single current frame, for scripting |
| `/status.json` | connected, stopped, screen size, frames served |

### It is read-only, and unauthenticated

Nothing the page serves can send input to the session — there is no route that
accepts one. But everything it serves is a picture of a live desktop, and there
is no password on it.

So it binds to `127.0.0.1` by default, and warns if you bind it anywhere else.
On a VPS, `--view-host 0.0.0.0` means the whole internet. Use a tunnel:

```sh
ssh -L 8900:127.0.0.1:8900 you@vps
# then open http://127.0.0.1:8900 on your own machine
```

If the port is busy the view moves to one the OS picks and says so, rather than
refusing to start — losing the view is not worth losing a forty-minute run over.
The same reasoning applies to any error it hits: the view is wrapped so that it
can fail without taking the session with it.

### Why not tkinter

There used to be a desktop front end with the live view in a panel. A browser is
strictly better here: it needs no display on the machine running the tool, so it
works from a headless VPS through the tunnel above; more than one person can
watch the same run; and it removes tkinter, Pillow's Tk bridge and several
megabytes from every platform package npm ships.


## 10. The rdp> prompt


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


## 11. Typing a codebase


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

So the codebase path is careful about which launch command it uses:

| | Launch | Cleanup |
|---|---|---|
| The obvious approach | `code -n -g <path>` — a new window per file | none |
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

### Waiting for the session to be usable

A newly created RDP session is **not ready for input straight away**. Explorer
still has to start and paint the shell, and until it does, `Win+R` is silently
dropped — the shell is what implements it. Keystrokes sent in that window land
in whatever grabs focus first, which on a fresh login is often a startup app.

There is no readiness signal in the RDP protocol, so a run infers one from
the screen: it waits for the first frame, then samples a downscaled copy of the
desktop until it stops changing (`--startup-timeout`, default 45s, then
`--startup-wait` extra seconds). A small pixel tolerance keeps the taskbar clock
and a blinking caret from counting as "still changing". On timeout it carries on
and falls back to the fixed delays rather than giving up.

This is also why the first screenshot is captured *after* that wait — taken
before it, it is just a black rectangle.

With `--editor code`, VS Code is launched with
`--disable-extensions --disable-workspace-trust`, because IntelliSense and
auto-formatting rewrite text as it arrives and the trust dialog would swallow
the first keystrokes. **Those flags only apply when VS Code starts a new
instance** — if VS Code is already running in the remote session your `code`
command is forwarded to that instance and the flags are ignored. Close it
there first, or use Notepad.

### How typed code survives the editor

Editors fight you when you type code. Two behaviours matter:

* **Auto-indent.** After Enter, the editor inserts its own indentation. Typing
  your own leading spaces on top of that doubles them, and by line 20 the file
  is unparseable.
* **Auto-closing brackets and quotes.** Typing `(` inserts `()`.

`InputSender.type_lines(editor_safe=True)` handles both, and it is enabled
only for VS Code. Before each line it presses `Home` then
`Shift+End`, which selects exactly the whitespace the editor just inserted; the
line's own text then replaces that selection. `Escape` before each `Enter`
dismisses any completion popup that would otherwise be accepted by the newline.
Auto-closed brackets take care of themselves, because typing the matching closer
types *over* the inserted one.

This was verified offline against a simulated editor that applies auto-indent
and auto-closing: 25 different source files round-trip **byte for byte**, and
the same input with the strategy disabled visibly corrupts indentation.


## 12. Limitations of background GUI input


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


## 13. When the session is minimised or disconnected


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
* the interactive prompt notices and exits; a codebase run aborts,
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

  It lives in **the directory you ran the command from**, not next to the
  binary — which matters once npm has put that binary inside `node_modules`.
  The run prints the exact path on startup, and `RDP_STOP_FILE` overrides it.
  `screenshots/` follows the same rule, via `RDP_SCREENSHOT_DIR`.
* **Press Ctrl+C**. At an interactive
  prompt this aborts the running command and keeps the session; press it again
  to quit. During a codebase run it aborts the sequence and disconnects
  cleanly. Modifier keys held down mid-chord are always released
  on the way out, so the remote session is never left with a stuck `Ctrl`.

Both are local and synchronous: they stop this process from *sending*, so they
work even if the server has stopped responding.


## 14. Configuration


There is no config file in the project. Values resolve in this order, first
match wins:

1. **command line flags** — `--host`, `--user`, `--domain`, `--port`
2. **environment variables** (`RDP_HOST` and friends) — handy for scripting
3. **the remembered profile** — written after a successful connection
4. **a prompt** — but only when stdin is a terminal

That last condition matters more than it looks. The old CLI decided whether to
prompt from whether a subcommand was given, which meant the same missing
password produced a clean error under one invocation and an indefinite block
under another. The test is now whether stdin is actually a terminal, so a pipe,
a cron job and a CI runner all fail fast with a message naming the flag to
pass. `--no-input` forces that behaviour everywhere.

`autordp config show` prints what is currently remembered, `autordp config
env` lists every variable that is read, and both take `--json`.

### Remembered credentials

After a connection succeeds, the host, port, username, domain and screen size
are saved to:

```
%LOCALAPPDATA%\autordp\profile.json
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
startup apps that steal focus. The first screenshot being an almost-empty file
is the tell-tale sign that the desktop had not painted yet.

**VS Code does not open in a run**
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
Install Git for Windows, or `git` from your package manager on Linux and
macOS. Only the codebase actions need it.

**`git clone failed: fatal: remote helper 'https' aborted session`**
`git-remote-https` died before it spoke any protocol, and the usual reason is
a library mismatch: something has pointed the dynamic linker somewhere other
than the system libraries, so git loads the wrong `libcrypto`/`libssl` and
exits on a symbol lookup. The frozen binary used to cause exactly this on
Linux and macOS by handing git its own bundled libraries; that is fixed (the
subprocess environment is restored first — `autordp/environment.py`). If you
still see it, check for an `LD_LIBRARY_PATH` in your own shell or unit file,
and confirm the clone works by hand:

```bash
env -u LD_LIBRARY_PATH git clone --depth 1 https://github.com/<owner>/<repo> /tmp/t
```

The message keeps the last few lines of git's output, newest first, so the
line that names the real cause is in it rather than only the `aborted session`
summary.

**A codebase run says it would take hours**
It would — see [section 9](#10-typing-a-codebase). Lower the minute budget, or
accept that only part of the repository gets typed. If you actually want the
files on the server, run `git clone` there instead.

**A repository types nothing**
Every file was filtered out: binaries, files over 20 KB, non-UTF-8, or emoji
(which RDP's unicode key event cannot carry). The log lists the reason per file.

**Everything hangs**
Press Ctrl+C, run `stop` at the `rdp>` prompt, or create the stop file the run
named on startup (`New-Item STOP`). All three are checked between keystrokes.

Run with `RDP_LOG_LEVEL=DEBUG` for protocol-level detail, and
`RDP_LOG_FILE=run.log` to keep it.


## 16. Building the binary


CI builds a binary on five platforms for every release
([section 18](#18-publishing)). To build one locally:

```powershell
.\scripts\build.ps1              # Windows
```

```sh
python -m PyInstaller autordp.spec --noconfirm    # anywhere
```

`build.ps1` creates the virtual environment if it is missing, installs
dependencies, builds, and then **runs the result**. Pass `-Npm` to also assemble
the npm packages around it, `-Clean` to discard PyInstaller's caches first.

### Why the build is verified by running it

A PyInstaller build can succeed and still produce a binary that is missing half
of what it needs, because this dependency tree imports things dynamically and
static analysis cannot see them. Two such bugs were found the hard way and are
now handled in `autordp.spec`:

1. **aardwolf loads keyboard layouts with**
   `importlib.import_module('aardwolf.keyboard.layouts.layout_KBDUSX')`.
   A dynamic import like that is invisible to PyInstaller, so none of the
   400-odd layout modules would be bundled. The binary starts, connects, and
   then fails on the first keystroke with *Unknown keyboard layout 'enus'*.
   `collect_submodules` fixes it.

2. **unicrypto chooses a crypto backend at import time**, also with `__import__`:
   `unicrypto.backends.pycryptodomex` → `from Cryptodome.Util import Counter`.
   Without `Cryptodome` collected, the binary dies on startup with
   *cannot import name 'Counter' from 'Cryptodome.Util'*.

The first of those is the nastier one, because nothing about the build or the
first minute of a run looks wrong. So the smoke test does not check that the
binary exists — it runs `autordp doctor --json` and asserts that both the
`rdp stack` and `keyboard layout` checks report `ok`. `doctor` loads a real
layout and resolves a real scancode through it, so a hollow build fails in
seconds instead of twenty minutes in. `scripts/build.ps1` and
`.github/workflows/release.yml` run the same assertion.

### What is deliberately not excluded

`unittest` looks like obvious dead weight in a CLI, but `asn1tools` →
`pyparsing` → `pyparsing.testing` imports it at module scope, so excluding it
breaks the build at startup with `ModuleNotFoundError`.

`PIL` stays too: `client.py` asks aardwolf for `VIDEO_FORMAT.PIL` frames, so
every screenshot and every live-view frame goes through it. What *is* excluded
is `tkinter` and Pillow's Tk bridge, which is worth several megabytes on every
platform package now that there is no GUI to need them.

UPX compression is off. Packed executables are flagged by antivirus far more
often, and this one is downloaded by `npm install` onto machines whose owners
did not choose to trust it.


## 17. Project layout


```
autordp/
├── README.md
├── pyproject.toml        Python packaging; version comes from src/autordp/__init__
├── requirements.txt
├── autordp.spec          PyInstaller recipe for the binary npm ships
│
├── src/autordp/
│   ├── __init__.py       the one place the version is written down
│   ├── __main__.py       ENTRY POINT — `python -m autordp`, and PyInstaller's
│   ├── config.py         settings resolution and logging
│   ├── credentials.py    remembers the connection, DPAPI-encrypts the password
│   ├── client.py         connect, retry, screenshot, frames, disconnect
│   ├── input.py          keyboard/mouse RDP input events, emergency stop
│   ├── loop.py           the asyncio loop thread that keeps Ctrl+C working
│   ├── shell.py          the rdp> commands
│   ├── session.py        editor profiles and the codebase typing flow
│   ├── codebase.py       git clone, file selection, time budgeting
│   ├── webview.py        the read-only live view, over HTTP
│   ├── daemon.py         detaching with `-d`, the pid file, stopping
│   ├── environment.py    what a child process inherits from a frozen build
│   │
│   └── cli/
│       ├── main.py       dispatch, and the one place exceptions become exit codes
│       ├── parser.py     the subcommand grammar
│       ├── context.py    flags → environment → profile → prompt
│       ├── commands.py   what each subcommand does
│       ├── menu.py       the interactive menu
│       └── ui.py         colour, spinners, progress bars, tables
│
├── npm/autordp/          the npm launcher package (source)
│   ├── package.json      optionalDependencies are stamped in at build time
│   ├── bin/autordp.js    resolves the platform binary and hands over to it
│   └── install.js        postinstall: check it arrived, fix the +x bit
│
├── scripts/
│   ├── build.ps1         local Windows build, with the smoke test
│   ├── build-npm.mjs     assembles every npm package from CI's binaries
│   └── make_icon.py      keys the white background out of the artwork
│
├── .github/workflows/
│   ├── ci.yml            imports, the parser grammar, the live view, npm assembly
│   └── release.yml       build on 5 platforms, smoke test, publish
│
└── assets/               icon source and the generated .ico
```

Two ways to start it: `autordp` once installed, or `python -m autordp` from a
checkout. Nothing else under `src/autordp/` is an entry point.

**The layering is the point.** `cli/` knows about terminals; everything above it
does not. `session.py` narrates with `print` so its output reads correctly in a
log file or a cron mail, and `cli/commands.py` intercepts that stream and
repaints it for a terminal — one narration in the code, the right one for each
audience. The progress callbacks (`on_plan`, `on_file`, `on_chars`) are optional
parameters defaulting to `None`, so the session layer has no idea a progress bar
exists.

`shell.py` is separate from the prompt that reads its commands for the same
reason: the same `dispatch` runs a command typed interactively, one passed with
`-c`, and one read from a `--script` file, so those three can never drift apart.

### What was removed, and why

The tkinter front end and its PyInstaller spec are gone, along with the
generated-file demo action. The GUI's one genuinely useful feature — the live
view — came back as `webview.py`, which is better than what it replaced
([section 9](#9-the-live-view)). A run typed a generated file to prove input
worked; `autordp connect` proves the same thing in one command without writing
anything to the remote machine.

Some modules were renamed when they moved into `src/`, because the old names had
stopped being true: `main.py` was never the entry point, `demo.py` had grown
past a run, `rdp_client.py` and `input_events.py` were stuttering inside a
package already called `autordp`. They are `shell.py`, `session.py`, `client.py`
and `input.py` now.


## 18. Publishing

Pushing to `main` never publishes. `ci.yml` runs on every push and pull request;
`release.yml` publishes, and only fires two ways.

### On demand, from the Actions tab

The usual route. Push as often as you like, release when you mean to.

```sh
# 1. bump the one place a version is written down
$EDITOR src/autordp/__init__.py      # __version__ = "0.2.0"
git commit -am "..." && git push
```

Then **Actions -> release -> Run workflow**, untick **dry_run**, Run. It builds
every platform, publishes, and tags the commit it published from so there is
still a record of what produced that version.

Leave `dry_run` ticked to rehearse: it builds everything and runs
`npm pack --dry-run` on each package, then stops.

### By tag

```sh
node scripts/release.mjs --version 0.2.0
```

That bumps `__version__`, checks the version is free on the registry, builds and
smoke-tests locally as a gate, then commits, tags and pushes. Pushing a `v*` tag
by hand does the same thing without the local gate.

### The version has to move every time

npm never allows a version to be reused, even after `npm unpublish`. So
`verify` refuses to start a real publish if the version is already on the
registry:

```
Error: version 0.1.0 is already published for: @ayushshukla8920/autordp ...
       Bump __version__ in src/autordp/__init__.py.
```

That check earns its place. Without it a forgotten bump fails *inside* the
publish loop, after some platform packages are already live -- and since those
versions can never be reused, the only way out is a bump anyway. Better to find
out before a single binary has been built.

`scripts/build-npm.mjs` stamps every `package.json` from `__version__`, so the
Python package and the npm packages cannot disagree about what they are.

### One secret

`NPM_TOKEN` in Settings -> Secrets and variables -> Actions: an npm **granular**
token with **read and write** on packages and scopes, and **no IP allowlist** --
GitHub runners get ephemeral IPs, so any CIDR there fails the publish with a 403
that reads like an auth problem. Note npm's default expiry is 30 days.

### Details that are not obvious

- **Publishing order.** Platform packages first, launcher last. The launcher
  lists them as `optionalDependencies`, so an install landing between the two
  publishes must not find a launcher whose dependencies do not exist yet.
- **Only what was built is declared.** If a runner fails, that platform is
  skipped with a warning and left out of the launcher's dependencies. Listing a
  version that was never published would make `npm install` fail on *every*
  platform, because a missing optional dependency is still a resolution error.
  This is not theoretical: it is what kept 0.1.0 correct when `darwin-x64` was
  dropped mid-release.
- **glibc.** The Linux builds run on `ubuntu-22.04`, not `ubuntu-latest`. glibc
  is forward compatible but not backward, so a binary linked against the newest
  runner's glibc refuses to start on an older server -- exactly the kind of
  machine this tool gets run from.
- **A retired runner label does not error.** It queues forever waiting for a
  runner that will never come, and `continue-on-error` cannot help because the
  job never starts. That is why `macos-13` was removed rather than tolerated,
  and why the build job carries `timeout-minutes`.

### The npm layout

```
@ayushshukla8920/autordp                 the launcher: 4 kB, no binary
  |- @ayushshukla8920/autordp-win32-x64      optional, os=win32  cpu=x64
  |- @ayushshukla8920/autordp-linux-x64      optional, os=linux  cpu=x64
  |- @ayushshukla8920/autordp-linux-arm64    optional, os=linux  cpu=arm64
  \- @ayushshukla8920/autordp-darwin-arm64   optional, os=darwin cpu=arm64
```

Users only ever install the launcher. npm reads `os` and `cpu` *before*
unpacking and skips a package that does not match, so an install downloads
exactly one binary -- `npm install @ayushshukla8920/autordp` reports
**2 packages added**, not five. This is how esbuild, swc, rollup and opencode
all do it; the only visible cost is that npmjs.com indexes each package
separately, so a scope search lists them all.

`bin/autordp.js` finds the binary with `require.resolve`, which walks the same
lookup npm itself uses -- so hoisting, nested `node_modules`, pnpm's symlink
store and yarn workspaces all resolve correctly, none of which a hand-built
relative path would.

It is a `spawnSync` with inherited stdio rather than an exec-and-replace,
because Node has no `execve`. The cost is one extra process in the tree; the
benefit is that it works identically on Windows, where there is no exec at all.
Signals are inherited along with the terminal, so **Ctrl+C reaches the binary
directly** -- which matters here, because Ctrl+C is the emergency stop.


## 19. Scope


This drives machines you administer, from an account you own, for GUI
automation and testing. It needs valid credentials and a server configured to
accept Remote Desktop connections — it is a Remote Desktop client, with the same
access as any other. Don't point it at systems you are not authorised to use.
