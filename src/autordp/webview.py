"""A read-only live view of the remote screen, served over HTTP.

The tkinter front end used to show the desktop in a panel. A browser is a
better place for it: it needs no display on the machine running the tool, so it
works from a headless VPS over an SSH tunnel, and more than one person can
watch the same run.

    autordp connect --view
    autordp repo <url> --view 8900

The stream is **MJPEG** -- ``multipart/x-mixed-replace``, one JPEG after
another on a single response. That is an old format, and it is the right one
here: every browser renders it in a plain ``<img>`` tag with no JavaScript, no
WebSocket, no MediaSource, and no negotiation. A desktop that changes in bursts
and then sits still also suits it, because a frame is only pushed when the
screen actually changes.

**This view is read-only and unauthenticated.** Nothing it serves can send input
to the session, but anything it serves is a picture of a live desktop. That is
why it binds to 127.0.0.1 unless told otherwise, and why binding anywhere else
prints a warning: on a VPS, 0.0.0.0 means the whole internet. Use an SSH tunnel
instead:

    ssh -L 8900:127.0.0.1:8900 you@vps
"""

from __future__ import annotations

import http.server
import io
import json
import logging
import socket
import socketserver
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger("autordp.webview")

# Long enough that an idle desktop is not re-encoded pointlessly, short enough
# that the browser connection does not look dead to an impatient proxy.
IDLE_KEEPALIVE = 5.0
DEFAULT_PORT = 8900

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>autordp live view</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #0d0f12; color: #c9d1d9;
         font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; gap: 1rem; align-items: center;
           padding: .5rem .9rem; background: #161b22;
           border-bottom: 1px solid #30363d; flex: none; }
  header b { color: #58a6ff; font-weight: 600; }
  #state { margin-left: auto; }
  .dot { display: inline-block; width: .55rem; height: .55rem;
         border-radius: 50%; background: #3fb950; margin-right: .4rem; }
  .dot.off { background: #f85149; }
  main { flex: 1; display: grid; place-items: center; overflow: auto;
         padding: .9rem; }
  img { max-width: 100%; max-height: 100%; object-fit: contain;
        border: 1px solid #30363d; background: #000; }
  footer { padding: .4rem .9rem; color: #6e7681; border-top: 1px solid #30363d;
           flex: none; }
</style>
<header>
  <b>autordp</b>
  <span id="target"></span>
  <span id="state"><span class="dot" id="dot"></span><span id="status">connecting</span></span>
</header>
<main><img id="screen" src="/stream" alt="remote desktop"></main>
<footer>Read-only view &mdash; this page cannot send input to the session.</footer>
<script>
  const dot = document.getElementById("dot");
  const status = document.getElementById("status");
  const target = document.getElementById("target");
  const screen = document.getElementById("screen");

  async function poll() {
    try {
      const response = await fetch("/status.json", { cache: "no-store" });
      const info = await response.json();
      target.textContent = info.target + "  " + info.width + "x" + info.height;
      dot.classList.toggle("off", !info.connected);
      status.textContent = info.connected
        ? (info.stopped ? "stopped" : "live") + " \\u00b7 " + info.frames + " frames"
        : "disconnected";
    } catch {
      dot.classList.add("off");
      status.textContent = "view offline";
    }
  }
  poll();
  setInterval(poll, 2000);

  // An MJPEG response that ends -- the session dropped, or the tool exited --
  // leaves the last frame frozen on screen with no indication why. Retrying
  // makes the view recover on its own if the run reconnects.
  screen.addEventListener("error", () => {
    setTimeout(() => { screen.src = "/stream?" + Date.now(); }, 2000);
  });
</script>
"""


class LiveView:
    """Serves the client's current frame to a browser. Read-only."""

    def __init__(self, client, port: int = DEFAULT_PORT,
                 host: str = "127.0.0.1", quality: int = 70,
                 max_fps: float = 8.0) -> None:
        self.client = client
        self.host = host
        self.port = port
        self.quality = quality
        self.min_interval = 1.0 / max_fps if max_fps > 0 else 0.0
        self.frames = 0
        self._server: socketserver.BaseServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Start serving and return the URL. Raises OSError if the port is taken."""
        view = self

        class Handler(_Handler):
            live = view

        # ThreadingHTTPServer, not HTTPServer: an MJPEG response never ends, so
        # a single-threaded server would be occupied by the first browser
        # forever and /status.json would hang.
        self._server = http.server.ThreadingHTTPServer((self.host, self.port),
                                                       Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="rdp-webview", daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self.host in ("", "0.0.0.0") else self.host
        return f"http://{shown}:{self.port}/"

    @property
    def exposed(self) -> bool:
        """Whether this is reachable from outside the machine."""
        return self.host not in ("127.0.0.1", "localhost", "::1")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "LiveView":
        self.start()
        return self

    def __exit__(self, *_exc_info) -> bool:
        self.stop()
        return False

    # -- frames ------------------------------------------------------------

    def jpeg(self) -> bytes | None:
        """The current frame as JPEG bytes, or None if there is nothing yet."""
        image = self.client.current_frame()
        if image is None:
            return None
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.quality, optimize=False)
        self.frames += 1
        return buffer.getvalue()

    def status(self) -> dict:
        settings = self.client.settings
        return {
            "target": settings.target,
            "connected": self.client.is_alive,
            "stopped": self.client.stop.tripped,
            "width": settings.width,
            "height": settings.height,
            "frames": self.frames,
        }


def free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """``preferred`` if it is free, otherwise one the OS picks.

    Falling back beats failing: the view is a convenience, and refusing to
    start a forty-minute typing run because something else already has port
    8900 would be the wrong trade.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Four routes and nothing else. ``live`` is set by :meth:`LiveView.start`."""

    live: LiveView
    protocol_version = "HTTP/1.1"
    server_version = "autordp"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        route = urlparse(self.path).path
        if route == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/status.json":
            self._send(json.dumps(self.live.status()).encode("utf-8"),
                       "application/json")
        elif route == "/frame.jpg":
            self._single_frame()
        elif route == "/stream":
            self._stream()
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A live desktop must never be cached, by the browser or by anything
        # between it and here.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _single_frame(self) -> None:
        frame = self.live.jpeg()
        if frame is None:
            self.send_error(503, "No frame yet")
            return
        self._send(frame, "image/jpeg")

    def _stream(self) -> None:
        boundary = "autordpframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # No Content-Length is possible and the response never ends, so the
        # connection cannot be reused afterwards.
        self.send_header("Connection", "close")
        self.end_headers()

        previous = None
        last_sent = 0.0
        try:
            while True:
                if not self.live.client.is_alive:
                    break
                frame = self.live.jpeg()
                now = time.monotonic()
                unchanged = frame is not None and frame == previous

                if frame is None or (unchanged and now - last_sent < IDLE_KEEPALIVE):
                    # Nothing worth sending. Sleeping here rather than pushing a
                    # duplicate is what keeps an idle session close to zero
                    # bandwidth, which matters when the tool is typing for an
                    # hour and the screen changes one line at a time.
                    time.sleep(max(self.live.min_interval, 0.1))
                    continue

                self.wfile.write(f"--{boundary}\r\n".encode("ascii"))
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n"
                                 .encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                previous, last_sent = frame, now
                time.sleep(self.live.min_interval)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser navigated away or was closed. Entirely normal.
            logger.debug("live view client disconnected")
        except Exception as exc:  # noqa: BLE001 - a view must never kill a run
            logger.debug("live view stream ended: %s", exc)

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler writes to stderr by default, which would print
        # a request line over the progress bar on every frame.
        logger.debug("webview %s", fmt % args)
