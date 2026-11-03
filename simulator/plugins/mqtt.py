"""MQTT 模拟器：向 Broker 周期发布 JSON 遥测，供网关 mqtt 驱动订阅验证。

网关端点配置示例：`protocol_identifier: {"topic": "iot/sim/temp", "path": "$.temperature"}`。
"""
from __future__ import annotations

import json
from typing import Any

from .base import BaseSimulator


class MqttSimulator(BaseSimulator):
    def __init__(self, name: str, config: dict[str, Any], reporter: Any | None = None):
        super().__init__(name, config, reporter)
        self._client: Any = None
        self._mqtt: Any = None

    def _run_guarded_setup(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("mqtt 模拟器需要 paho-mqtt：pip install paho-mqtt") from exc
        self._mqtt = mqtt
        host = str(self.config.get("host", "127.0.0.1"))
        port = int(self.config.get("port", 1883))
        client_id = str(self.config.get("client_id", f"anythings-sim-{self.name}"))
        client = mqtt.Client(client_id=client_id)
        username = self.config.get("username")
        if username:
            client.username_pw_set(str(username), str(self.config.get("password", "")))
        client.connect(host, port, keepalive=30)
        client.loop_start()
        self._client = client
        print(f"[mqtt] 已连接 {host}:{port}，主题 {self.config.get('topic', 'iot/sim/data')}")

    def step(self) -> None:
        topic = str(self.config.get("topic", "iot/sim/data"))
        payload = self.payload()
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        assert self._client is not None
        result = self._client.publish(topic, body, qos=1)
        payload["status"] = "sent" if result.rc == self._mqtt.MQTT_ERR_SUCCESS else "error"
        print(f"[mqtt] publish {topic} {body}")
        self.record(payload)

    def _teardown(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
