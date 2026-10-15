"""OS-managed advisory locks; a crashed process never leaves a held lock."""

import errno
import math
import os
import time
from pathlib import Path


class LockBusyError(RuntimeError):
    pass


class FileLock:
    def __init__(self, path, timeout=0):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("Lock timeout must be a finite nonnegative number")
        self.path = Path(path)
        self.timeout = timeout
        self._stream = None

    def __enter__(self):
        if self._stream is not None:
            raise RuntimeError("FileLock instances are not reentrant")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._stream = stream
                    return self
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise LockBusyError(f"资源正在使用，请稍后重试：{self.path}") from error
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except BaseException:
            stream.close()
            raise

    def __exit__(self, *args):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        # Retain the lock file: deleting it would allow a second inode to be
        # locked concurrently by another process already waiting on this one.
