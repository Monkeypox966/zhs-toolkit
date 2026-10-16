import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from zhs.app import main
from zhs.common.control import (
    StopRequested,
    atomic_operation,
    check_stop,
    managed_runner,
    stop_runner,
)
from zhs.common.locking import FileLock, LockBusyError
from zhs.common.storage import atomic_write_json

WORKER = """
import sys
from pathlib import Path
from unittest.mock import patch
from zhs.app import main
from zhs.common.control import check_stop, defer_stop, sleep
root = Path(sys.argv[1])
mode = sys.argv[2]
def application(args):
    if mode == 'once':
        assert (root / 'saved').exists(), 'new runner started before old cleanup'
        return 0
    (root / 'started').write_text('ready')
    try:
        if mode == 'critical':
            with defer_stop():
                (root / 'critical').write_text('pending')
                while not (root / 'release').exists():
                    sleep(0.02)
                (root / 'committed').write_text('persisted')
        while True:
            sleep(60)
    finally:
        (root / 'saved').write_text('persisted')
with patch('zhs.app.run_application', side_effect=application):
    raise SystemExit(main([]))
"""


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.locks = self.root / "var/locks"
        self.env = dict(os.environ, ZHS_HOME=str(self.root), PYTHONDONTWRITEBYTECODE="1")

    def launch(self, mode="wait"):
        process = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", WORKER, str(self.root), mode],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup():
            if process.poll() is None:
                # Windows venv python may be a launcher with a separate worker.
                # Let the actual owner release its lock before killing a wrapper.
                (self.root / "release").write_text("finish")
                stop_runner(self.locks, timeout=5)
                if process.poll() is None:
                    process.wait(timeout=5)
            process.communicate(timeout=10)

        self.addCleanup(cleanup)
        return process

    def wait_for(self, name):
        deadline = time.monotonic() + 10
        while not (self.root / name).exists():
            if time.monotonic() >= deadline:
                self.fail(f"worker did not create {name}")
            time.sleep(0.02)

    def stop_cli(self):
        return subprocess.run(
            [sys.executable, "-B", "-m", "zhs", "stop"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_stop_without_runner_does_not_load_config_or_login(self):
        result = self.stop_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "config").exists())
        self.assertFalse((self.locks / "runner.json").exists())

    def test_stop_rejects_study_options_before_notifying_runner(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["stop", "--qrlogin"])
        self.assertEqual(caught.exception.code, 2)

    def test_stop_cli_waits_for_cleanup_and_releases_lock(self):
        worker = self.launch()
        self.wait_for("started")
        result = self.stop_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(worker.wait(timeout=5), 0)
        self.assertTrue((self.root / "saved").exists())
        self.assertFalse((self.locks / "runner.json").exists())
        self.assertFalse((self.locks / "stop.json").exists())
        with FileLock(self.locks / "runner.lock"):
            pass

    def test_default_run_stops_previous_before_starting_replacement(self):
        worker = self.launch()
        self.wait_for("started")
        replacement = self.launch("once")
        stdout, stderr = replacement.communicate(timeout=15)
        self.assertEqual(replacement.returncode, 0, stdout + stderr)
        self.assertEqual(worker.wait(timeout=5), 0)
        self.assertTrue((self.root / "saved").exists())

    def test_stop_finishes_critical_checkpoint_before_exit(self):
        worker = self.launch("critical")
        self.wait_for("critical")
        stopper = subprocess.Popen(
            [sys.executable, "-B", "-m", "zhs", "stop"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.wait_for("var/locks/stop.json")
            time.sleep(0.2)
            self.assertIsNone(stopper.poll())
            self.assertIsNone(worker.poll())
            self.assertFalse((self.root / "committed").exists())
            (self.root / "release").write_text("finish")
            stdout, stderr = stopper.communicate(timeout=15)
            self.assertEqual(stopper.returncode, 0, stdout + stderr)
            self.assertEqual(worker.wait(timeout=5), 0)
            self.assertTrue((self.root / "committed").exists())
        finally:
            if stopper.poll() is None:
                stopper.kill()
            stopper.communicate(timeout=5)

    def test_timeout_does_not_start_an_overlapping_runner(self):
        worker = self.launch("critical")
        self.wait_for("critical")
        with self.assertRaises(TimeoutError), managed_runner(self.locks, timeout=0.15):
            self.fail("replacement started while original was running")
        self.assertIsNone(worker.poll())
        with self.assertRaises(LockBusyError), FileLock(self.locks / "runner.lock"):
            pass
        (self.root / "release").write_text("finish")
        self.assertEqual(worker.wait(timeout=5), 0)

    def test_stale_request_cannot_stop_new_owner(self):
        worker = self.launch()
        self.wait_for("started")
        atomic_write_json(self.locks / "stop.json", {"token": "old-owner"})
        time.sleep(0.3)
        self.assertIsNone(worker.poll())
        self.assertEqual(self.stop_cli().returncode, 0)
        self.assertEqual(worker.wait(timeout=5), 0)

    def test_read_only_check_does_not_replace_active_execution(self):
        worker = self.launch()
        self.wait_for("started")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "zhs", "--check"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(worker.poll())
        self.assertFalse((self.locks / "stop.json").exists())
        self.assertEqual(self.stop_cli().returncode, 0)
        self.assertEqual(worker.wait(timeout=5), 0)

    def test_crashed_runner_metadata_is_cleaned_using_os_lock(self):
        worker = self.launch()
        self.wait_for("started")
        owner = json.loads((self.locks / "runner.json").read_text())
        os.kill(owner["pid"], signal.SIGTERM)
        worker.wait(timeout=5)
        self.assertFalse(stop_runner(self.locks))
        self.assertFalse((self.locks / "runner.json").exists())

    def test_legacy_runner_is_not_force_killed(self):
        with FileLock(self.locks / "runner.lock"):
            with self.assertRaisesRegex(RuntimeError, "旧实例"):
                stop_runner(self.locks, timeout=0.1)

    def test_critical_operation_defers_stop_then_next_checkpoint_stops(self):
        event = threading.Event()
        event.set()

        @atomic_operation
        def operation():
            event.set()
            check_stop()
            return "saved"

        with patch("zhs.common.control._active", event):
            with self.assertRaises(StopRequested):
                operation()
            event.clear()
            self.assertEqual(operation(), "saved")
            with self.assertRaises(StopRequested):
                check_stop()

    def test_owner_metadata_contains_no_configuration(self):
        with managed_runner(self.locks):
            owner = json.loads((self.locks / "runner.json").read_text())
            self.assertEqual(set(owner), {"pid", "token"})
            self.assertEqual(owner["pid"], os.getpid())
