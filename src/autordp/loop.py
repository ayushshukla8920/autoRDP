"""An asyncio event loop living on its own thread.

The RDP library is asynchronous; the command prompt is not. Running the loop on
a worker thread and keeping the prompt on the main one is what lets Ctrl+C work
during a long operation.

That split is not stylistic. On Windows, ``KeyboardInterrupt`` is delivered to
the main thread, and only to the main thread. With the event loop there, a
Ctrl+C in the middle of typing a forty-line file would tear down the RDP
session; with the loop on a worker, the main thread takes the interrupt, asks
the coroutine to wind itself down between keystrokes, and the session survives.

Ctrl+C is also the emergency stop, so this has to be reliable.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading


class LoopThread:
    """Owns one event loop and runs coroutines on it from the main thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="rdp-loop",
                                        daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, on_interrupt=None):
        """Run a coroutine to completion, staying responsive to Ctrl+C.

        Polling the future in short slices is what lets a Ctrl+C be delivered
        to this thread at all -- a blocking ``future.result()`` would swallow
        it. ``on_interrupt`` then asks the coroutine to stop itself rather than
        being abandoned mid-keystroke, and a second Ctrl+C gives up waiting.
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

    def __enter__(self) -> "LoopThread":
        self.start()
        return self

    def __exit__(self, *_exc_info) -> bool:
        self.close()
        return False
