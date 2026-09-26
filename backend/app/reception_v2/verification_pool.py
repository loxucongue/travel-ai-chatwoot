"""Bounded scope workers, each owning and closing its model transport."""
from concurrent.futures import Future
from queue import Queue, Full
import atexit
import threading


class ScopeVerificationPool:
    def __init__(self, workers=4):
        self.workers = workers
        self.queue = Queue(maxsize=workers * 4)
        self.threads = []
        self.lock = threading.Lock()
        self.closed = False
        atexit.register(self.close)

    def submit(self, function, *args):
        with self.lock:
            if self.closed:
                raise RuntimeError('v2_verification_pool_closed')
            if not self.threads:
                for index in range(self.workers):
                    thread = threading.Thread(target=self._worker, name=f'v2-scope-{index}', daemon=True)
                    self.threads.append(thread)
                    thread.start()
            future = Future()
            try:
                self.queue.put_nowait((future, function, args))
            except Full as exc:
                raise RuntimeError('v2_verification_queue_full') from exc
            return future

    def _worker(self):
        from app.deepseek_evaluation import close_deepseek_transport
        try:
            while True:
                task = self.queue.get()
                if task is None:
                    return
                future, function, args = task
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = function(*args)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            # Async loops/clients must be closed by the thread that owns them.
            close_deepseek_transport()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            threads = list(self.threads)
        for _ in threads:
            self.queue.put(None)
        for thread in threads:
            thread.join()
