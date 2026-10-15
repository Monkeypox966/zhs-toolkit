"""Keep application code, configuration and mutable state in separate places."""

import os
from pathlib import Path


def application_home():
    override = os.environ.get("ZHS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src" / "zhs").is_dir():
        return checkout
    return Path.home() / ".zhs"


def resolve_path(value):
    """Resolve relative paths against the application home, independent of cwd.

    Legacy logical names remain accepted by course and cache code, but always
    resolve to the new layout. They never create files alongside source code.
    """
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if path.is_absolute():
        return path.resolve()
    parts = path.parts
    if not parts:
        return application_home()
    targets = {
        "config.json": Path("config/config.json"),
        "cookies.json": Path("var/cookies.json"),
        "execution.json": Path("var/execution.json"),
        "aiexamAnswer": Path("var/cache/answers"),
        "AiDownloadCache": Path("var/cache/materials"),
        "logs": Path("var/logs"),
    }
    prefix = targets.get(parts[0])
    if prefix is not None:
        path = prefix.joinpath(*parts[1:])
    return (application_home() / path).resolve()


def config_path():
    return resolve_path("config.json")
