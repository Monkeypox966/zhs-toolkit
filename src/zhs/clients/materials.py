"""Download course materials and manage only files uploaded by this project."""

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from openai import OpenAI

from zhs.common.locking import FileLock
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
        local_max_cache_files=500,
        local_max_cache_size_mb=1024,
        local_max_age_days=30,
        download_timeout_seconds=300,
    ):
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in (
                max_file_size_mb,
                max_cache_files,
                max_cache_size_gb,
                local_max_cache_files,
                local_max_cache_size_mb,
                local_max_age_days,
            )
        ):
            raise ValueError("Cache limits must be nonnegative numbers")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (max_cache_files, local_max_cache_files)
        ):
            raise ValueError("Cache file counts must be nonnegative integers")
        if (
            isinstance(download_timeout_seconds, bool)
            or not isinstance(download_timeout_seconds, (int, float))
            or not math.isfinite(download_timeout_seconds)
            or download_timeout_seconds <= 0
        ):
            raise ValueError("Download timeout must be a finite positive number")
        if not isinstance(delete_immediately, bool):
            raise ValueError("delete_immediately must be a boolean")
        self._root = Path(cache_dir or getRealPath("AiDownloadCache"))
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_file_size = max_file_size_mb * 1024 * 1024
        self._max_cache_files = max_cache_files
        self._max_cache_size = max_cache_size_gb * 1024**3
        self._delete_immediately = delete_immediately
        self._download_timeout = download_timeout_seconds
        self._local_limits = (
            local_max_cache_files,
            local_max_cache_size_mb * 1024 * 1024,
            local_max_age_days * 86400,
        )
        # Scope manifests to an endpoint and account; never enumerate the user's
        # remote files. A missing manifest means we own no remote files.
        scope = hashlib.sha256(f"{baseUrl.rstrip('/')}/{moonShotKey}".encode()).hexdigest()
        self._manifest = self._root / f"remote-{scope}.json"
        self._lock_path = self._root / ".materials.lock"
        self._load_manifest()
        self._session = create_session()
        try:
            self._client = OpenAI(api_key=moonShotKey, base_url=baseUrl, timeout=30, max_retries=2)
        except BaseException:
            self._session.close()
            raise

    def _load_manifest(self):
        self._files = {}
        if self._manifest.exists():
            data = json.loads(self._manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
                or isinstance(item.get("size"), bool)
                or not isinstance(item.get("size"), int)
                or item["size"] < 0
                or isinstance(item.get("created_at"), bool)
                or not isinstance(item.get("created_at"), (int, float))
                or not math.isfinite(item["created_at"])
                or item["created_at"] < 0
                for item in data.values()
            ):
                raise ValueError("Invalid project upload manifest")
            self._files = data

    def close(self):
        try:
            self._session.close()
        finally:
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
        suffix = Path(parsed.path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,11}", suffix):
            suffix = ".bin"
        name = hashlib.sha256(url.encode()).hexdigest() + suffix
        destination = self._root / "downloads" / name
        if destination.exists():
            if destination.stat().st_size > self._max_file_size:
                raise ValueError("Material exceeds maximum file size")
            if destination.stat().st_size:
                return destination
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        deadline = time.monotonic() + self._download_timeout
        try:
            with self._session.get(
                url,
                stream=True,
                timeout=(min(10, self._download_timeout), min(30, self._download_timeout)),
            ) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not re.fullmatch(r"[0-9]+", str(length)):
                        raise ValueError("Invalid material Content-Length")
                    length = int(length)
                if length is not None and length > self._max_file_size:
                    raise ValueError("Material exceeds maximum file size")
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=".zhs-", suffix=".part", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    size = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Material download exceeded its time budget")
                        size += len(chunk)
                        if size > self._max_file_size:
                            raise ValueError("Material exceeds maximum file size")
                        stream.write(chunk)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Material download exceeded its time budget")
                    if size == 0:
                        raise ValueError("Material download is empty")
                    encoding = response.headers.get("Content-Encoding", "identity").lower()
                    if length is not None and encoding in {"", "identity"} and size != length:
                        raise ValueError("Material download is incomplete: Content-Length mismatch")
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
        with FileLock(self._lock_path, timeout=30):
            self._load_manifest()
            try:
                return self._parse_txt(url)
            finally:
                self._manage_local_cache()

    def _manage_local_cache(self):
        root = self._root.resolve()
        files = []
        max_files, max_size, max_age = self._local_limits
        now = time.time()
        for name in ("downloads", "texts"):
            directory = self._root / name
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(root)
                ):
                    continue
                owned = re.fullmatch(r"[0-9a-f]{64}\.[A-Za-z0-9.]{1,12}", path.name)
                partial = path.name.startswith(".zhs-") and path.suffix == ".part"
                if not owned and not partial:
                    continue
                stat = path.stat()
                if (partial and now - stat.st_mtime > 86400) or (
                    owned and max_age and now - stat.st_mtime > max_age
                ):
                    path.unlink()
                elif owned:
                    files.append((stat.st_mtime, path, stat.st_size))
        total_size = sum(item[2] for item in files)
        count = len(files)
        for _, path, size in sorted(files):
            if count <= max_files and total_size <= max_size:
                break
            path.unlink()
            count -= 1
            total_size -= size

    def _parse_txt(self, url):
        local_path = self._download(url)
        os.utime(local_path, None)
        with local_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        text_path = self._root / "texts" / f"{digest}.json"
        if text_path.exists():
            try:
                text = json.loads(text_path.read_text(encoding="utf-8"))["content"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Empty cached text")
            except ValueError, KeyError, TypeError:
                text = None
            if text is not None:
                os.utime(text_path, None)
                if self._delete_immediately and digest in self._files:
                    self._delete_owned(digest)
                self._manage_cache()
                return text
        if digest not in self._files:
            self._upload_owned(local_path, digest)
        try:
            try:
                content = self._client.files.content(file_id=self._files[digest]["id"]).text
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise
                # The user or service may have removed a previously owned upload.
                # Forget only that stale entry, then recreate it once.
                del self._files[digest]
                self._save_manifest()
                self._upload_owned(local_path, digest)
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

    def _upload_owned(self, local_path, digest):
        uploaded = self._client.files.create(file=local_path, purpose="file-extract")
        self._files[digest] = {
            "id": uploaded.id,
            "size": local_path.stat().st_size,
            "created_at": time.time(),
        }
        self._save_manifest()
