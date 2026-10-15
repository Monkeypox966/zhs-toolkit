import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from zhs.clients.materials import PptToTxt
from zhs.common.logging import logger


def setUpModule():
    logger.setLevel("CRITICAL")


class MaterialsTests(unittest.TestCase):
    def create_client(self, directory, **kwargs):
        with patch("zhs.clients.materials.OpenAI") as factory:
            client = PptToTxt("test-key", cache_dir=directory, **kwargs)
        self.addCleanup(client.close)
        return client, factory.return_value

    def test_never_enumerates_or_deletes_unowned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            client, api = self.create_client(directory, max_cache_files=0)
            client._manage_cache()
            api.files.list.assert_not_called()
            api.files.delete.assert_not_called()
            client._files["owned"] = {"id": "our-id", "size": 1, "created_at": 1}
            client._save_manifest()
            client._manage_cache()
            api.files.delete.assert_called_once_with(file_id="our-id")

    def test_failed_deletion_keeps_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            client, api = self.create_client(directory, max_cache_files=0)
            client._files["owned"] = {"id": "our-id", "size": 1, "created_at": 1}
            client._save_manifest()
            api.files.delete.side_effect = RuntimeError("offline")
            with self.assertRaises(RuntimeError):
                client._manage_cache()
            self.assertIn("owned", json.loads(client._manifest.read_text()))

    def test_size_limit_and_partial_download_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            client, api = self.create_client(directory, max_file_size_mb=0.000001)
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.iter_content.return_value = [b"too large"]
            client._session.get = Mock(return_value=response)
            with self.assertRaises(ValueError):
                client._download("https://example.test/../../material.pptx")
            self.assertEqual(list(Path(directory).rglob("*.part")), [])
            self.assertEqual(list((Path(directory) / "downloads").iterdir()), [])

    def test_conversion_uses_text_cache_and_deletes_owned_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            client, api = self.create_client(directory, delete_immediately=True)
            local = Path(directory) / "sample.pptx"
            local.write_bytes(b"example")
            client._download = Mock(return_value=local)
            api.files.create.return_value.id = "our-id"
            api.files.content.return_value.text = '{"content": "converted"}'
            self.assertEqual(client.parseTxt("https://example.test/material"), "converted")
            self.assertEqual(client.parseTxt("https://example.test/material"), "converted")
            api.files.create.assert_called_once()
            api.files.delete.assert_called_once_with(file_id="our-id")

    def test_deleted_remote_upload_is_recreated_once(self):
        import hashlib
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            client, api = self.create_client(directory)
            local = Path(directory) / "sample.pptx"
            local.write_bytes(b"example")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            client._files[digest] = {"id": "stale", "size": 7, "created_at": 1}
            client._save_manifest()
            client._download = Mock(return_value=local)
            error = RuntimeError("missing upload")
            error.status_code = 404
            api.files.content.side_effect = [error, SimpleNamespace(text="restored text")]
            api.files.create.return_value.id = "new-owned"
            self.assertEqual(client.parseTxt("https://example.test/ppt"), "restored text")
            api.files.create.assert_called_once()
            self.assertEqual(client._files[digest]["id"], "new-owned")
