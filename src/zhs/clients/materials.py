"""Download course materials and manage only files uploaded by this project."""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from openai import OpenAI

from zhs.common.storage import atomic_write_json
from zhs.common.transport import create_session
from zhs.common.utils import getRealPath


class PptToTxt:
    def __init__(
        self,
        moonShotKey,
        baseUrl="https://api.moonshot.cn/v1",
        max_file_size_mb=100,
        max_cache_files=500,
        max_cache_size_gb=8,
        delete_immediately=False,
        cache_dir=None,
    ):
        self._root = Path(cache_dir or getRealPath("AiDownloadCache"))
        self._root.mkdir(parents=True, exist_ok=True)
        self._session = create_session()
        self._client = OpenAI(api_key=moonShotKey, base_url=baseUrl, timeout=30, max_retries=2)
        self._max_file_size = max_file_size_mb * 1024 * 1024
        self._max_cache_files = max_cache_files
        self._max_cache_size = max_cache_size_gb * 1024**3
        self._delete_immediately = delete_immediately
        # Scope manifests to an endpoint and account; never enumerate the user's
        # remote files. A missing manifest means we own no remote files.
        scope = hashlib.sha256(f"{baseUrl.rstrip('/')}/{moonShotKey}".encode()).hexdigest()
        self._manifest = self._root / f"remote-{scope}.json"
        self._files = {}
        if self._manifest.exists():
            data = json.loads(self._manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(item, dict) or not {"id", "size", "created_at"} <= item.keys()
                for item in data.values()
            ):
                raise ValueError("Invalid project upload manifest")
            self._files = data

    def close(self):
        self._session.close()
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _save_manifest(self):
        atomic_write_json(self._manifest, self._files)

    def _download(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Material URL must use HTTP or HTTPS")
        suffix = Path(parsed.path).suffix
        if not suffix or len(suffix) > 12:
            suffix = ".bin"
        name = hashlib.sha256(url.encode()).hexdigest() + suffix
        destination = self._root / "downloads" / name
        if destination.exists():
            if destination.stat().st_size > self._max_file_size:
                raise ValueError("Material exceeds maximum file size")
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with self._session.get(url, stream=True, timeout=(10, 30)) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > self._max_file_size:
                    raise ValueError("Material exceeds maximum file size")
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, suffix=".part", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    size = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        size += len(chunk)
                        if size > self._max_file_size:
                            raise ValueError("Material exceeds maximum file size")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _delete_owned(self, key):
        item = self._files[key]
        # Treat an already-removed owned file as cleaned up; retain the manifest
        # on transient failures so deletion can be retried safely.
        try:
            self._client.files.delete(file_id=item["id"])
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise
        del self._files[key]
        self._save_manifest()

    def _manage_cache(self):
        while self._files and (
            len(self._files) > self._max_cache_files
            or sum(item["size"] for item in self._files.values()) > self._max_cache_size
        ):
            oldest = min(self._files, key=lambda key: self._files[key]["created_at"])
            self._delete_owned(oldest)

    def parseTxt(self, url):
        local_path = self._download(url)
        digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
        text_path = self._root / "texts" / f"{digest}.json"
        if text_path.exists():
            text = json.loads(text_path.read_text(encoding="utf-8"))["content"]
            if self._delete_immediately and digest in self._files:
                self._delete_owned(digest)
            self._manage_cache()
            return text
        if digest not in self._files:
            uploaded = self._client.files.create(file=local_path, purpose="file-extract")
            self._files[digest] = {
                "id": uploaded.id,
                "size": local_path.stat().st_size,
                "created_at": time.time(),
            }
            self._save_manifest()
        try:
            content = self._client.files.content(file_id=self._files[digest]["id"]).text
            try:
                decoded = json.loads(content)
                text = decoded.get("content", content) if isinstance(decoded, dict) else content
            except ValueError:
                text = content
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Material conversion returned no text")
            atomic_write_json(text_path, {"content": text})
        finally:
            if self._delete_immediately and digest in self._files:
                self._delete_owned(digest)
        self._manage_cache()
        return text
