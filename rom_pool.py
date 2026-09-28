"""Small pool of booted ROM workers with their in-memory ready checkpoints."""

import atexit
import contextlib
import threading
import time


class EnginePool:
    def __init__(self, factory, size=3):
        self.factory = factory
        self.size = size
        self._ready = []
        self._created = 0
        self._condition = threading.Condition()
        self._closed = False
        atexit.register(self.close)

    @contextlib.contextmanager
    def session(self, deadline):
        with self._condition:
            while not self._ready and self._created >= self.size and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timeout: no ROM worker became available")
                self._condition.wait(remaining)
            if self._closed:
                raise RuntimeError("ROM worker pool is closed")
            if self._ready:
                worker = self._ready.pop()
            else:
                self._created += 1
                worker = None
        if worker is None:
            try:
                worker = self.factory(deadline)
            except BaseException:
                with self._condition:
                    self._created -= 1
                    self._condition.notify()
                raise
        healthy = True
        try:
            yield worker
        except BaseException:
            healthy = False
            raise
        finally:
            discard = False
            with self._condition:
                if healthy and not self._closed and worker[0].is_running:
                    self._ready.append(worker)
                else:
                    self._created -= 1
                    discard = True
                self._condition.notify()
            if discard:
                worker[0].close()

    def prewarm(self):
        """Start idle workers in the background; a failed start remains retryable."""
        def start():
            for _ in range(self.size):
                with self._condition:
                    if self._closed or self._created >= self.size:
                        return
                    self._created += 1
                try:
                    worker = self.factory(time.monotonic() + 30)
                except Exception:
                    with self._condition:
                        self._created -= 1
                        self._condition.notify()
                    return
                with self._condition:
                    if self._closed:
                        self._created -= 1
                        discard = True
                    else:
                        self._ready.append(worker)
                        discard = False
                    self._condition.notify()
                if discard:
                    worker[0].close()
        threading.Thread(target=start, name="rom-prewarm", daemon=True).start()

    def close(self):
        with self._condition:
            self._closed = True
            idle, self._ready = self._ready, []
            self._created -= len(idle)
            self._condition.notify_all()
        for client, _mailbox in idle:
            client.close()
