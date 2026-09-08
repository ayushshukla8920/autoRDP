# autordp

Drive a Windows machine over RDP — type into its editors, click its windows,
watch its desktop in your browser — from a terminal on any machine. No Python
required.

```sh
npx @ayushshukla8920/autordp doctor
npm install -g @ayushshukla8920/autordp
```

The important part: **it opens its own RDP session.** This is not a robot moving
your mouse. It is an RDP client in its own right, a peer of `mstsc.exe`, so the
desktop it drives is a session it established and your own is never touched. You
can keep working while it runs.

## Two things it does

```sh
autordp connect          # test the connection, then drive it from an rdp> prompt
autordp repo <git-url>   # clone a repository and type it into a remote editor
```

Run `autordp` with no arguments for a menu. Everything else supports those two:

```sh
autordp doctor           # is this machine ready? runtime, credentials, the port
autordp config set       # verify a connection, then remember it
autordp keys ctrl        # which key names `key` understands
```

Every command takes `--help`.

### Watch it in a browser

```sh
autordp connect --view          # http://127.0.0.1:8900
autordp repo <git-url> --view 9000
```

A read-only MJPEG stream of the remote screen, in any browser, with no
JavaScript framework and no plugin. It binds to `127.0.0.1` by default — the
page is unauthenticated, so from a VPS reach it through a tunnel rather than
`--view-host 0.0.0.0`:

```sh
ssh -L 8900:127.0.0.1:8900 you@vps
```

### Drive it without a prompt

```sh
autordp connect -c "key win+r" -c "type notepad" -c "key ENTER"
autordp connect --script session.txt        # one command per line, # comments
echo "$RDP_SECRET" | autordp --password-stdin --no-input connect -c "screenshot health.png"
```

### Before a long run

Typing is slow — every character is an RDP input event, so throughput is about
20–25 characters a second. `--dry-run` clones and plans without connecting or
needing any credentials, so you can see what a time budget actually buys:

```sh
autordp repo https://github.com/pallets/click --minutes 30 --dry-run
```

While it runs you get a progress bar counting characters, not files, with a live
rate and ETA — file sizes vary too much for a per-file bar to mean anything.

## Configuration

Connection details resolve in this order, first match wins:

1. flags — `--host`, `--user`, `--domain`, `--port`
2. environment — `RDP_HOST`, `RDP_USERNAME`, `RDP_PASSWORD`, `RDP_DOMAIN`, `RDP_PORT`
3. the saved profile — written by `autordp config set`
4. a prompt, but only when stdin is a terminal

There is deliberately no `--password` flag: an argument is visible in `ps` and
in shell history. Use `RDP_PASSWORD`, `--password-stdin`, or save one with
`autordp config set --remember-password`, which encrypts it with DPAPI so only
your Windows account can read it back.

Under cron or CI, pass `--no-input` so a missing value fails immediately with a
message naming the flag, rather than blocking on a prompt nobody will answer.
`autordp config env` lists every variable that is read.

## Stopping it

Ctrl+C, or create the file the run names on startup. Both are checked between
keystrokes, so input stops mid-file rather than at the end of it.

## Requirements

The Windows machine needs Remote Desktop enabled and TCP 3389 reachable, and the
account has to be allowed to sign in remotely. `autordp doctor` checks the parts
it can see from here. `autordp repo` also needs `git` on your PATH.

## Exit codes

| Code | Meaning              | Code | Meaning                  |
| ---- | -------------------- | ---- | ------------------------ |
| 0    | success              | 4    | input refused by session |
| 1    | connection failed    | 5    | input error              |
| 2    | configuration        | 6    | repository problem       |
| 3    | emergency stop       | 130  | interrupted              |

## Platforms

Prebuilt binaries: **Windows x64**, **Linux x64**, **Linux arm64**, **macOS
Apple silicon**. Windows on ARM uses the x64 build under emulation.

**macOS Intel** has no prebuilt binary. Install from source instead:

```sh
pip install autordp
```

Installing this package pulls exactly one binary: each platform build declares
its `os` and `cpu`, and npm skips the ones that do not match.

## Licence

MIT. Full documentation and source:
<https://github.com/ayushshukla8920/autoRDP>
