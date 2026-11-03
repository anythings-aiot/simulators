"""插件基类：数值生成与周期调度。"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

DEFAULT_VALUES = [
    {"name": "temperature", "min": 15.0, "max": 35.0, "decimals": 2},
    {"name": "humidity", "min": 30.0, "max": 80.0, "decimals": 2},
]


class BaseSimulator:
    """所有模拟器插件的基类：负责按 interval_ms 周期执行 step()。"""

    def __init__(self, name: str, config: dict[str, Any], reporter: Any | None = None):
        self.name = name
        self.config = config
        self.reporter = reporter
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self._rng = random.Random(config.get("seed"))

    @property
    def interval_ms(self) -> int:
        return max(100, int(self.config.get("interval_ms", 1000)))

    @property
    def device_id(self) -> str:
        return str(self.config.get("device_id", self.name))

    def value_specs(self) -> list[dict[str, Any]]:
        specs = self.config.get("values")
        if not specs:
            return list(DEFAULT_VALUES)
        return list(specs)

    def generate_values(self) -> dict[str, float]:
        """按配置生成一组随机测点值。"""
        values: dict[str, float] = {}
        for spec in self.value_specs():
            low = float(spec.get("min", 0))
            high = float(spec.get("max", 100))
            decimals = int(spec.get("decimals", 2))
            values[str(spec["name"])] = round(self._rng.uniform(low, high), decimals)
        return values

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "device_id": self.device_id,
            "protocol": self.name,
        }
        payload.update(self.generate_values())
        return payload

    def record(self, payload: dict[str, Any]) -> None:
        if self.reporter is not None:
            self.reporter.record(payload)

    def step(self) -> None:
        """单个周期要做的事，由子类实现。"""
        raise NotImplementedError

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - 模拟器不因单次失败退出
                print(f"[{self.name}] step 失败: {exc}")
            self.stop_event.wait(self.interval_ms / 1000)

    def start(self) -> None:
        # setup 同步执行，端口占用/依赖缺失等错误可直接反馈给调用方
        self._run_guarded_setup()
        thread = threading.Thread(target=self._run_loop, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _run_guarded_setup(self) -> None:
        """启动前的初始化（如起服务），子类可覆盖。"""

    def _teardown(self) -> None:
        """停止时的清理，子类可覆盖。"""

    def stop(self) -> None:
        self.stop_event.set()
        self._teardown()
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=2)
