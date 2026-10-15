import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhs.config import load_config
from zhs.paths import application_home, config_path, resolve_path


class LayoutTests(unittest.TestCase):
    def test_checkout_paths_do_not_depend_on_working_directory(self):
        checkout = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ):
            os.environ.pop("ZHS_HOME", None)
            with patch("pathlib.Path.cwd", return_value=Path(tempfile.gettempdir())):
                self.assertEqual(application_home(), checkout)
                self.assertEqual(config_path(), checkout / "config/config.json")

    def test_local_data_names_resolve_to_separate_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ZHS_HOME": directory}):
                root = Path(directory).resolve()
                cases = {
                    "config.json": "config/config.json",
                    "cookies.json": "var/cookies.json",
                    "execution.json": "var/execution.json",
                    "aiexamAnswer/course/exam.json": "var/cache/answers/course/exam.json",
                    "AiDownloadCache/texts/content.json": "var/cache/materials/texts/content.json",
                    "./logs": "var/logs",
                }
                for original, target in cases.items():
                    with self.subTest(original=original):
                        self.assertEqual(resolve_path(original), root / target)
                self.assertEqual(list(root.iterdir()), [])

    def test_absolute_paths_remain_absolute(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "explicit.json"
            self.assertEqual(resolve_path(path), path)

    def test_installed_package_defaults_to_user_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ):
                os.environ.pop("ZHS_HOME", None)
                with (
                    patch("zhs.paths.__file__", str(root / "lib/site-packages/zhs/paths.py")),
                    patch("pathlib.Path.home", return_value=root / "user"),
                ):
                    self.assertEqual(application_home(), root / "user/.zhs")

    def test_configuration_creation_uses_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ZHS_HOME": directory}):
                load_config()
                self.assertTrue((Path(directory) / "config/config.json").is_file())
                self.assertFalse((Path(directory) / "config.json").exists())

    def test_all_launchers_work_outside_project_without_creating_files(self):
        checkout = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            commands = (["-m", "zhs", "--help"], [str(checkout / "main.py"), "--help"])
            for command in commands:
                result = subprocess.run(
                    [sys.executable, "-B", *command],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: zhs", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])
