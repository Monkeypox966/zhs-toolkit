"""Compatibility launcher; prefer ``uv run zhs``."""

from zhs.app import main

if __name__ == "__main__":
    raise SystemExit(main())
