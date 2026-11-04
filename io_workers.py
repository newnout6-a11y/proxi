"""Bounded daemon workers for HTTP reads that cannot be cancelled before headers."""
from concurrent.futures import Executor, Future
import queue
import threading


class DaemonIOExecutor(Executor):
    """Keep blocked urllib workers from holding up interpreter shutdown.

    Sessions still abort established responses and drain their asyncio tasks.
    A connection waiting for its first headers may outlive that bounded cleanup.
    """
    def __init__(self, max_workers, thread_name_prefix="relay-io"):
        self._max_workers = max(1, int(max_workers))
        self._prefix = thread_name_prefix
        self._jobs = queue.SimpleQueue()
        self._idle = threading.Semaphore(0)
        self._lock = threading.Lock()
        self._threads = []
        self._shutdown = False

    def submit(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot submit work after shutdown")
            future = Future()
            self._jobs.put((future, fn, args, kwargs))
            if not self._idle.acquire(blocking=False) and len(self._threads) < self._max_workers:
                thread = threading.Thread(target=self._work, daemon=True,
                                          name="%s-%d" % (self._prefix, len(self._threads)))
                self._threads.append(thread)
                thread.start()
            return future

    def _work(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, fn, args, kwargs = job
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
            self._idle.release()
            del job, future, fn, args, kwargs

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    while True:
                        try:
                            job = self._jobs.get_nowait()
                        except queue.Empty:
                            break
                        if job is not None:
                            job[0].cancel()
                for _thread in self._threads:
                    self._jobs.put(None)
        if wait:
            for thread in self._threads:
                thread.join()
