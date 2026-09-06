"""An asyncio event loop living on its own thread.

Every front end drives async RDP work from synchronous code (a REPL, a tkinter
callback), so the loop runs on a worker thread and the caller blocks on a
future. Polling the future in short slices is what lets a Ctrl+C reach the main
thread mid-command instead of being swallowed by a blocking ``await``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading


class LoopThread:
    """An asyncio event loop living on its own thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="rdp-loop", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, on_interrupt=None):
        """Run a coroutine to completion, staying responsive to Ctrl+C.

        Polling the future in short slices is what lets a Ctrl+C be delivered to
        this thread; ``on_interrupt`` then asks the coroutine to wind itself down
        rather than being abandoned mid-keystroke.
        """
        future = self.submit(coro)
        interrupted = False
        while True:
            try:
                return future.result(timeout=0.2)
            except concurrent.futures.TimeoutError:
                continue
            except KeyboardInterrupt:
                if interrupted:
                    raise
                interrupted = True
                print("\n^C - stopping the current command...")
                if on_interrupt is not None:
                    on_interrupt()
                try:
                    future.result(timeout=5)
                except Exception:  # noqa: BLE001 - already unwinding
                    pass
                raise

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()
