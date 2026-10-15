import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from zhs.common.logging import MonoLogger, logger
from zhs.common.transport import api_query


def setUpModule():
    logger.setLevel("CRITICAL")


class LoggingTransportTests(unittest.TestCase):
    def test_logger_retains_previous_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "info.log"
            path.write_text("previous run\n", encoding="utf-8")
            instance = MonoLogger(
                "test-append", path=directory, to_console=False, level=logging.INFO
            )
            try:
                instance.info("new run")
            finally:
                instance.close()
            self.assertIn("previous run", path.read_text(encoding="utf-8"))
            self.assertIn("new run", path.read_text(encoding="utf-8"))

    def test_json_request_does_not_mutate_shared_headers(self):
        session = MagicMock()
        session.headers = {"Existing": "value"}
        session.request.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            api_query(session, "https://example.test/api", {"x": 1}, content_type="json")
        self.assertEqual(session.headers, {"Existing": "value"})
        self.assertEqual(session.request.call_args.kwargs["json"], {"x": 1})

    def test_transport_reports_http_failure_before_json(self):
        session = MagicMock()
        response = session.request.return_value.__enter__.return_value
        response.raise_for_status.side_effect = RuntimeError("503")
        with self.assertRaises(RuntimeError):
            api_query(session, "https://example.test/api", {})
        response.json.assert_not_called()
