"""Configuration defaults, migration and validation."""

import json
from copy import deepcopy
from pathlib import Path

from zhs.common.models import ObjDict
from zhs.common.storage import atomic_write_json
from zhs.common.utils import getConfigPath, versionCmp

DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "qrlogin": True,
    "save_cookies": True,
    "proxies": {},
    "logLevel": "INFO",
    "tree_view": True,
    "progressbar_view": True,
    "qr_extra": {"show_in_terminal": None, "ensure_unicode": False},
    "image_path": "",
    "pushplus": {"enable": False, "token": ""},
    "bark": {"enable": False, "token": "https://example.com/xxxxxxxxx"},
    "config_version": "1.4.0",
    "ai": {
        "enabled": True,
        "use_zhidao_ai": True,
        "openai": {
            "api_base": "https://api.openai.com",
            "api_key": "",
            "model_name": "claude-3-5-sonnet-20240620",
        },
        "ppt_processing": {
            "provide_to_ai": False,
            "moonShot": {
                "base_url": "https://api.moonshot.cn/v1",
                "api_key": "",
                "delete_after_convert": True,
            },
        },
        "use_stream": True,
    },
}


def deep_merge(defaults, supplied):
    result = deepcopy(defaults)
    for key, value in supplied.items():
        if key in defaults and isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"配置字段 {key} 必须是对象")
            result[key] = deep_merge(defaults[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path=None):
    path = Path(path or getConfigPath())
    if not path.exists():
        merged = deepcopy(DEFAULT_CONFIG)
        atomic_write_json(path, merged)
        return ObjDict(merged, default=None)
    try:
        original = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取配置文件 {path}: {error}") from error
    if not isinstance(original, dict):
        raise ValueError("配置文件的顶层必须是对象")
    supplied = deepcopy(original)
    version = supplied.get("config_version", "1.0.0")
    if versionCmp(version, "1.0.1") < 0:
        supplied.pop("qr_extra", None)
    if versionCmp(version, "1.3.0") < 0 and "push" in supplied:
        supplied["pushplus"] = supplied.pop("push")
    merged = deep_merge(DEFAULT_CONFIG, supplied)
    if versionCmp(version, DEFAULT_CONFIG["config_version"]) < 0:
        merged["config_version"] = DEFAULT_CONFIG["config_version"]
    if merged != original:
        atomic_write_json(path, merged)
    return ObjDict(merged, default=None)


def require_fields(config, fields, label):
    if not isinstance(config, dict):
        raise ValueError(f"{label}配置必须是对象")
    missing = [field for field in fields if not config.get(field)]
    if missing:
        raise ValueError(f"{label}配置缺少字段：{', '.join(missing)}")


def validate_openai_config(config):
    require_fields(config, ["api_key", "api_base", "model_name"], "外部 AI")


def validate_ppt_config(config):
    require_fields(config, [], "PPT")
    if config.get("provide_to_ai", False):
        require_fields(config.get("moonShot", {}), ["base_url", "api_key"], "PPT 转换")


def validate_ai_config(config, no_exam=False):
    ai = config.get("ai", {})
    require_fields(ai, [], "AI")
    if no_exam:
        return
    if ai.get("enabled", False) and not ai.get("use_zhidao_ai", False):
        validate_openai_config(ai.get("openai", {}))
    attempts = ai.get("max_exam_attempts", 5)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("ai.max_exam_attempts 必须是正整数")
    validate_ppt_config(ai.get("ppt_processing", {}))
