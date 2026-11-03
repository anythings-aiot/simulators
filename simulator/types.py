"""模拟器公共类型定义。"""
from __future__ import annotations

from typing import Any

PluginConfig = dict[str, Any]


class Reporter:
    """简单的内存上报记录器，便于本地观察与测试断言。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, payload: dict[str, Any]) -> None:
        self.records.append(payload)
