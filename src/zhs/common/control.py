"""Cooperative runner shutdown, scoped to one data directory and one owner token."""

import json
import os
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from uuid import uuid4

from zhs.common.locking import FileLock, LockBusyError
from zhs.common.storage import atomic_write_json

_active = None
_local = threading.local()


class StopRequested(KeyboardInterrupt):
    """Unwind normal cleanup and journal persistence without ordinary retry handlers."""


def check_stop():
    if (
        _active is not None
        and threading.current_thread() is threading.main_thread()
        and not getattr(_local, "deferred", 0)
        and _active.is_set()
    ):
        raise StopRequested("收到停止命令")


@contextmanager
def defer_stop():
    """Finish an irreversible operation and its checkpoint before observing a stop."""
    _local.deferred = getattr(_local, "deferred", 0) + 1
    try:
        yield
    finally:
        _local.deferred -= 1


def atomic_operation(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        check_stop()
        with defer_stop():
            return method(*args, **kwargs)

    return wrapped


def sleep(seconds, closed=None):
    deadline = time.monotonic() + seconds
    while True:
        check_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if closed is not None:
            if closed.wait(min(0.2, remaining)):
                raise InterruptedError("Client closed")
        else:
            time.sleep(min(0.2, remaining))


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except OSError, ValueError:
        return {}


def _cleanup(directory):
    # Caller owns runner.lock and control.lock, or is the departing runner.
    (directory / "runner.json").unlink(missing_ok=True)
    (directory / "stop.json").unlink(missing_ok=True)


def _acquire(directory, timeout, replace):
    lock = FileLock(directory / "runner.lock")
    try:
        lock.__enter__()
        return lock, False
    except LockBusyError:
        if not replace:
            raise
    owner = _read(directory / "runner.json")
    token = owner.get("token")
    if not isinstance(token, str) or not token:
        # The process may have exited between the lock probe and metadata read.
        try:
            lock.__enter__()
            return lock, False
        except LockBusyError as error:
            raise RuntimeError(
                "正在运行的旧实例不支持停止命令，请先结束旧实例，再使用新版启动"
            ) from error
    atomic_write_json(directory / "stop.json", {"token": token})
    print(f"已通知正在运行的实例（PID {owner.get('pid')}）停止，等待保存进度并退出……")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.__enter__()
            return lock, True
        except LockBusyError as error:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "停止请求已发送，旧实例仍在处理当前操作；未启动第二个实例。"
                    "稍后可再次执行停止或启动命令"
                ) from error
            time.sleep(0.05)


def stop_runner(directory, timeout=120):
    directory = Path(directory)
    with FileLock(directory / "control.lock", timeout=timeout):
        lock, stopped = _acquire(directory, timeout, replace=True)
        try:
            _cleanup(directory)
        finally:
            lock.__exit__()
    return stopped


@contextmanager
def managed_runner(directory, *, replace=True, timeout=120):
    global _active
    directory = Path(directory)
    lock = None
    watcher = None
    previous = _active
    finished = threading.Event()
    requested = threading.Event()
    token = uuid4().hex
    try:
        # Serialize handoffs; the departing runner never needs control.lock.
        with FileLock(directory / "control.lock", timeout=timeout):
            lock, _ = _acquire(directory, timeout, replace)
            _cleanup(directory)
            atomic_write_json(directory / "runner.json", {"pid": os.getpid(), "token": token})

            def watch():
                while not finished.wait(0.1):
                    if _read(directory / "stop.json").get("token") == token:
                        requested.set()
                        return

            watcher = threading.Thread(target=watch, name="runner-control", daemon=True)
            # Only main-thread execution observes stop requests. Worker cleanup can finish.
            _active = requested
            watcher.start()
        yield
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=2)
        _active = previous
        if lock is not None:
            try:
                if _read(directory / "runner.json").get("token") == token:
                    _cleanup(directory)
            finally:
                lock.__exit__()
