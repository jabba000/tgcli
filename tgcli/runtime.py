"""Runs an asyncio loop in a background thread; the UI thread talks to it via submit()/call()
and receives results as events in a thread-safe queue (so all UI state is mutated on the UI thread)."""
import asyncio
import logging
import queue
import threading

log = logging.getLogger("tgcli")


class AsyncRunner:
    def __init__(self):
        self.events = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="async-backend", daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout=None):
        """Blocking call (used for login, before curses starts)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit(self, coro, on_done=None, on_error=None):
        """Non-blocking. on_done(result) / on_error(exc) run later on the UI thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def finished(f):
            try:
                res = f.result()
            except Exception as e:  # noqa
                log.exception("backend call failed")
                if on_error:
                    self.events.put(("call", on_error, e))
                else:
                    self.events.put(("error", e))
                return
            if on_done:
                self.events.put(("call", on_done, res))

        fut.add_done_callback(finished)
        return fut

    def progress(self, label, cur, total):
        self.events.put(("progress", label, cur, total))

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
