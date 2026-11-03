"""配置加载：读取执行目录下的 config.yaml。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml 顶层必须是对象")
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("config.yaml plugins 必须是对象")
    return data
