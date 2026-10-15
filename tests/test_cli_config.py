import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhs.app import option
from zhs.cli import build_parser
from zhs.common.logging import logger
from zhs.common.storage import atomic_write_json
from zhs.config import DEFAULT_CONFIG, load_config, validate_ai_config


def setUpModule():
    logger.setLevel("CRITICAL")


class CliConfigTests(unittest.TestCase):
    def test_boolean_values_and_negative_switches(self):
        args = build_parser().parse_args(
            ["--tree_view", "False", "--no-progressbar-view", "--noexam", "false"]
        )
        self.assertFalse(args.tree_view)
        self.assertFalse(args.progressbar_view)
        self.assertFalse(args.noexam)
        self.assertTrue(build_parser().parse_args(["--noexam"]).noexam)
        self.assertFalse(option(False, True))
        self.assertIsNone(build_parser().parse_args([]).tree_view)

    def test_invalid_boolean_and_nonfinite_speed(self):
        for arguments in (["--tree-view", "maybe"], ["--speed", "nan"], ["--limit", "-1"]):
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(arguments)

    def test_configuration_deep_merge_preserves_user_fields(self):
        original_defaults = copy.deepcopy(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            atomic_write_json(
                path,
                {
                    "username": "test-user",
                    "password": "test-password",
                    "ai": {"enabled": False},
                    "tree_view": False,
                },
            )
            config = load_config(path)
            self.assertFalse(config.ai.enabled)
            self.assertIn("ppt_processing", config.ai)
            self.assertFalse(config.tree_view)
            self.assertEqual(config.username, "test-user")
            self.assertEqual(config.password, "test-password")
        self.assertEqual(DEFAULT_CONFIG, original_defaults)

    def test_ai_validation_routes(self):
        validate_ai_config({"ai": {"enabled": True, "use_zhidao_ai": True}})
        with self.assertRaises(ValueError):
            validate_ai_config({"ai": {"enabled": True, "use_zhidao_ai": False}})
        validate_ai_config({"ai": {"enabled": True}}, no_exam=True)

    def test_atomic_write_failure_keeps_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            atomic_write_json(path, {"old": True})
            with self.assertRaises(TypeError):
                atomic_write_json(path, {"invalid": object()})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_import_main_has_no_files_or_network_side_effects(self):
        root = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=root)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import main"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
