"""三协议测试设备模拟器：线性趋近随机目标，参数之间保持业务关联。"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import config as config
from coap_server import CODE_BAD_REQUEST, CODE_CHANGED, CODE_NOT_FOUND, CoapServer
from common import log

MEMORY_TOTAL_GIB = 8.0


class LinearSignal:
    def __init__(self, value: float, low: float, high: float, max_step: float):
        self.value = value
        self.low = low
        self.high = high
        self.max_step = max_step
        self.target = value

    def set_target(self, target: float) -> None:
        self.target = min(max(target, self.low), self.high)

    def step(self, dt: float) -> float:
        delta = self.target - self.value
        limit = self.max_step * dt
        if abs(delta) <= limit:
            self.value = self.target
        else:
            self.value += math.copysign(limit, delta)
        self.value = min(max(self.value, self.low), self.high)
        return self.value


class BaseSimulator:
    label = "SIM"
    name = "simulator"

    def __init__(self, host: str, port: int, interval: float = 1.0, seed: int | None = None):
        self.host = host
        self.port = port
        self.interval = max(0.1, interval)
        self.rng = random.Random(seed)
        self._next_target_at = time.monotonic() + self.rng.uniform(2.0, 5.0)

    def start(self) -> None:
        raise NotImplementedError

    def step(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def _retarget_due(self, now: float) -> bool:
        if now < self._next_target_at:
            return False
        self._next_target_at = now + self.rng.uniform(10.0, 20.0)
        return True


class MqttFanSimulator(BaseSimulator):
    label = "MQTT"
    name = "温控风扇"

    def __init__(self, host: str, port: int, interval: float = 1.0, seed: int | None = None):
        super().__init__(host, port, interval, seed)
        self.temperature = LinearSignal(25.0, 18.0, 40.0, 0.35)
        self.fan_speed = LinearSignal(self._fan_curve(25.0), 0.0, 2200.0, 80.0)
        self.temperature.set_target(self.rng.uniform(22.0, 38.0))
        self._client: Any = None
        self._mqtt: Any = None

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("MQTT 模拟器需要 paho-mqtt：pip install paho-mqtt") from exc
        self._mqtt = mqtt
        client_id = f"anythings-test-fan-{int(time.time())}"
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
        except (AttributeError, TypeError):
            client = mqtt.Client(client_id=client_id)
        try:
            client.connect(self.host, self.port, keepalive=30)
        except OSError as exc:
            raise RuntimeError(
                f"无法连接 MQTT Broker {self.host}:{self.port}，请确认数据站已启动"
            ) from exc
        client.loop_start()
        self._client = client
        log(f"[MQTT] 已连接 {self.host}:{self.port}，发布主题 {config.MQTT_TOPIC}")

    def step(self) -> None:
        now = time.monotonic()
        if self._retarget_due(now):
            target = self.rng.uniform(22.0, 38.0)
            self.temperature.set_target(target)
            log(f"[MQTT] 随机目标温度 {target:.1f}℃")
        temperature = self.temperature.step(self.interval)
        self.fan_speed.set_target(self._fan_curve(temperature))
        fan_speed = self.fan_speed.step(self.interval)
        payload = {
            "ts": int(time.time() * 1000),
            "device_id": config.TEST_DEVICE_IDS["mqtt"],
            "temperature": round(temperature, 2),
            "fan_speed": round(fan_speed, 0),
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if self._client is None:
            raise RuntimeError("MQTT 客户端未启动")
        info = self._client.publish(config.MQTT_TOPIC, body, qos=1)
        if info.rc != self._mqtt.MQTT_ERR_SUCCESS:
            log(f"[MQTT] 发布失败 rc={info.rc}", "WARN")
        log(f"[MQTT] 温度={temperature:.1f}℃ 风扇={fan_speed:.0f}rpm")

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    @staticmethod
    def _fan_curve(temperature: float) -> float:
        if temperature <= 20.0:
            return 0.0
        if temperature >= 38.0:
            return 2200.0
        return (temperature - 20.0) / 18.0 * 2200.0


class CoapSpraySimulator(BaseSimulator):
    label = "CoAP"
    name = "喷淋系统"

    def __init__(self, host: str, port: int, interval: float = 1.0, seed: int | None = None):
        super().__init__(host, port, interval, seed)
        self.humidity = LinearSignal(50.0, 25.0, 95.0, 2.0)
        self.pm10 = LinearSignal(90.0, 5.0, 220.0, 8.0)
        self.humidity.set_target(self.rng.uniform(32.0, 65.0))
        self.pm10.set_target(self.rng.uniform(120.0, 190.0))
        self.tsp_ratio = self.rng.uniform(2.0, 3.0)
        self.tsp = LinearSignal(self.pm10.value * self.tsp_ratio, 10.0, 700.0, 24.0)
        self.spraying = False
        self.spray_duration = 0.0
        self.planned_duration = 0.0
        # 记录控制中心下发动作累计延长的喷淋秒数，供验证脚本核对。
        self.extension_total = 0.0
        self._forced_pm10: float | None = None
        self._forced_until = 0.0
        self._cooldown_until = 0.0
        now = time.monotonic()
        self._next_humidity_target = now + self.rng.uniform(6.0, 14.0)
        self._next_pm10_target = now + self.rng.uniform(6.0, 14.0)
        self._server: CoapServer | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._server = CoapServer(
            self.host, self.port, self.snapshot, write_handler=self.handle_write,
        )
        try:
            self._server.start()
        except OSError as exc:
            raise RuntimeError(
                f"无法监听 CoAP {self.host}:{self.port}，端口可能被占用"
            ) from exc
        log(f"[CoAP] 喷淋系统已监听 coap://{self.host}:{self.port}{config.COAP_PATH}")

    def step(self) -> None:
        now = time.monotonic()
        forced = False
        with self._lock:
            if self._forced_pm10 is not None:
                if now < self._forced_until:
                    forced = True
                    self.pm10.set_target(self._forced_pm10)
                else:
                    self._forced_pm10 = None
                    self._forced_until = 0.0
        if now >= self._next_pm10_target and not self.spraying and not forced:
            target = self.rng.uniform(70.0, 190.0)
            self.pm10.set_target(target)
            self._next_pm10_target = now + self.rng.uniform(10.0, 20.0)
            log(f"[CoAP] 随机目标 PM10 {target:.1f}µg/m³")
        if now >= self._next_humidity_target and not self.spraying:
            target = self.rng.uniform(32.0, 68.0)
            self.humidity.set_target(target)
            self._next_humidity_target = now + self.rng.uniform(10.0, 20.0)
            log(f"[CoAP] 随机目标湿度 {target:.1f}%")

        if not self.spraying and now >= self._cooldown_until and self.pm10.value >= 130.0:
            self.spraying = True
            self.spray_duration = 0.0
            self.planned_duration = self.rng.uniform(15.0, 45.0)
            self.humidity.set_target(self.rng.uniform(80.0, 92.0))
            self.pm10.set_target(self.rng.uniform(30.0, 70.0))
            log(f"[CoAP] PM10 超标，开始喷淋，计划 {self.planned_duration:.0f}s")

        if self.spraying:
            self.spray_duration += self.interval
            if self.spray_duration >= self.planned_duration or self.pm10.value <= 40.0:
                reason = "达到计划时长" if self.spray_duration >= self.planned_duration else "PM10 已降至阈值以下"
                self.spraying = False
                self.spray_duration = 0.0
                self._cooldown_until = now + self.rng.uniform(5.0, 10.0)
                self.humidity.set_target(self.rng.uniform(45.0, 70.0))
                self.pm10.set_target(self.rng.uniform(120.0, 190.0))
                log(f"[CoAP] {reason}，停止喷淋")

        humidity = self.humidity.step(self.interval)
        pm10 = self.pm10.step(self.interval)
        if forced and self._forced_pm10 is not None:
            # 验证场景下锁定粉尘浓度，避免自动喷淋把读数拉低。
            self.pm10.value = self._forced_pm10
            pm10 = self.pm10.value
        self.tsp.set_target(pm10 * self.tsp_ratio)
        tsp = self.tsp.step(self.interval)
        remaining = self._spray_remaining()
        with self._lock:
            self._latest = {
                "humidity": round(humidity, 2),
                "pm10": round(pm10, 2),
                "tsp": round(tsp, 2),
                "spray_duration_s": int(round(self.spray_duration)),
                "spray_planned_s": int(round(self.planned_duration)),
                "spray_remaining_s": remaining,
                "spray_extend_total_s": int(round(self.extension_total)),
                "spraying": self.spraying,
                "ts": int(time.time() * 1000),
            }
        state = "喷淋中" if self.spraying else "待机"
        log(
            f"[CoAP] 湿度={humidity:.1f}% PM10={pm10:.1f}µg/m³ "
            f"TSP={tsp:.1f}µg/m³ 喷淋={int(round(self.spray_duration))}s {state} "
            f"剩余={remaining}s 累计延长={int(round(self.extension_total))}s"
        )

    def _spray_remaining(self) -> int:
        if not self.spraying:
            return 0
        return max(0, int(round(self.planned_duration - self.spray_duration)))

    def handle_write(self, path: str, payload: bytes) -> tuple[int, str]:
        """处理 CoAP 写请求：延长喷淋时长，或按验证脚本要求锁定 PM10。"""
        text = payload.decode("utf-8", errors="replace").strip()
        if path == config.COAP_EXTEND_PATH:
            try:
                seconds = float(text)
            except ValueError:
                return CODE_BAD_REQUEST, "invalid seconds"
            seconds = min(max(seconds, 1.0), 600.0)
            with self._lock:
                self.extension_total += seconds
                if self.spraying:
                    self.planned_duration += seconds
                else:
                    self.spraying = True
                    self.spray_duration = 0.0
                    self.planned_duration = seconds
                    self.humidity.set_target(self.rng.uniform(80.0, 92.0))
                    self.pm10.set_target(self.rng.uniform(30.0, 70.0))
            log(
                f"[CoAP] 收到延长喷淋指令 {seconds:g}s，"
                f"累计延长 {self.extension_total:g}s，计划时长 {self.planned_duration:g}s"
            )
            return CODE_CHANGED, f"{seconds:g}"
        if path == config.COAP_SIM_PATH:
            try:
                body = json.loads(text or "{}")
            except json.JSONDecodeError:
                return CODE_BAD_REQUEST, "invalid json"
            hold_s = float(body.get("hold_s", 60))
            with self._lock:
                if "pm10" in body:
                    self._forced_pm10 = float(body["pm10"])
                    self._forced_until = time.monotonic() + max(hold_s, 1.0)
                    self.pm10.set_target(self._forced_pm10)
                else:
                    self._forced_pm10 = None
                    self._forced_until = 0.0
            log(f"[CoAP] 验证脚本锁定 PM10={self._forced_pm10}，持续 {hold_s:g}s")
            return CODE_CHANGED, "locked" if self._forced_pm10 is not None else "released"
        return CODE_NOT_FOUND, "not found"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latest = getattr(self, "_latest", None)
            if latest is not None:
                return dict(latest)
        return {
            "humidity": round(self.humidity.value, 2),
            "pm10": round(self.pm10.value, 2),
            "tsp": round(self.tsp.value, 2),
            "spray_duration_s": int(round(self.spray_duration)),
            "spray_planned_s": int(round(self.planned_duration)),
            "spray_remaining_s": self._spray_remaining(),
            "spray_extend_total_s": int(round(self.extension_total)),
            "spraying": self.spraying,
            "ts": int(time.time() * 1000),
        }

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        simulator: PrometheusIpcSimulator = self.server.simulator  # type: ignore[attr-defined]
        if self.path.split("?", 1)[0] != config.PROMETHEUS_PATH:
            self.send_response(404)
            self.end_headers()
            return
        body = simulator.render_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """静默访问日志，避免每秒拉取时刷屏。"""


class _MetricsServer(ThreadingHTTPServer):
    allow_reuse_address = True


class PrometheusIpcSimulator(BaseSimulator):
    label = "PROM"
    name = "工控机"

    def __init__(self, host: str, port: int, interval: float = 1.0, seed: int | None = None):
        super().__init__(host, port, interval, seed)
        self.cpu = LinearSignal(35.0, 5.0, 95.0, 4.0)
        self.memory = LinearSignal(55.0, 20.0, 90.0, 2.0)
        self.cpu_temperature = LinearSignal(50.0, 30.0, 90.0, 2.5)
        cpu_target = self.rng.uniform(10.0, 90.0)
        self.cpu.set_target(cpu_target)
        self.memory.set_target(cpu_target * 0.55 + self.rng.uniform(20.0, 40.0))
        offsets = [self.rng.uniform(-12.0, 12.0) for _ in range(4)]
        average = sum(offsets) / len(offsets)
        self.core_offsets = [offset - average for offset in offsets]
        self._cores = [self.cpu.value + offset for offset in self.core_offsets]
        self._server: _MetricsServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            server = _MetricsServer((self.host, self.port), _MetricsHandler)
        except OSError as exc:
            raise RuntimeError(
                f"无法监听 Prometheus {self.host}:{self.port}，端口可能被占用"
            ) from exc
        server.simulator = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="prometheus-server", daemon=True)
        self._thread.start()
        log(
            f"[PROM] 工控机 exporter 已监听 "
            f"http://{self.host}:{self.port}{config.PROMETHEUS_PATH}"
        )

    def step(self) -> None:
        now = time.monotonic()
        if self._retarget_due(now):
            cpu_target = self.rng.uniform(10.0, 90.0)
            self.cpu.set_target(cpu_target)
            memory_target = min(max(cpu_target * 0.55 + self.rng.uniform(20.0, 40.0), 20.0), 90.0)
            self.memory.set_target(memory_target)
            log(f"[PROM] 随机目标 CPU={cpu_target:.1f}% 内存={memory_target:.1f}%")
        cpu = self.cpu.step(self.interval)
        memory = self.memory.step(self.interval)
        self.cpu_temperature.set_target(32.0 + cpu * 0.55)
        cpu_temperature = self.cpu_temperature.step(self.interval)
        cores = [min(max(cpu + offset, 0.0), 100.0) for offset in self.core_offsets]
        with self._lock:
            self._cores = cores
        used_gib = memory / 100.0 * MEMORY_TOTAL_GIB
        core_text = ",".join(f"{value:.0f}" for value in cores)
        log(
            f"[PROM] CPU温度={cpu_temperature:.1f}℃ CPU={cpu:.1f}% "
            f"核心=[{core_text}]% 内存={memory:.1f}% ({used_gib:.1f}/8.0GiB)"
        )

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            cores = list(self._cores)
        return {
            "cpu_temperature": round(self.cpu_temperature.value, 2),
            "cpu_usage_total": round(self.cpu.value, 2),
            "cpu_usage_core_0": round(cores[0], 2),
            "cpu_usage_core_1": round(cores[1], 2),
            "cpu_usage_core_2": round(cores[2], 2),
            "cpu_usage_core_3": round(cores[3], 2),
            "memory_usage": round(self.memory.value, 2),
            "memory_used_gib": round(self.memory.value / 100.0 * MEMORY_TOTAL_GIB, 3),
        }

    def render_metrics(self) -> str:
        values = self.snapshot()
        lines = [
            "# HELP test_ipc_cpu_temperature_celsius CPU 温度（摄氏度）",
            "# TYPE test_ipc_cpu_temperature_celsius gauge",
            f"test_ipc_cpu_temperature_celsius {values['cpu_temperature']}",
            "# HELP test_ipc_cpu_usage_percent CPU 使用率，core=total 为整机平均",
            "# TYPE test_ipc_cpu_usage_percent gauge",
            f'test_ipc_cpu_usage_percent{{core="total"}} {values["cpu_usage_total"]}',
        ]
        for core in range(4):
            lines.append(f'test_ipc_cpu_usage_percent{{core="{core}"}} {values[f"cpu_usage_core_{core}"]}')
        lines.extend([
            "# HELP test_ipc_memory_usage_percent 内存使用率（最大 8GiB）",
            "# TYPE test_ipc_memory_usage_percent gauge",
            f"test_ipc_memory_usage_percent {values['memory_usage']}",
            "# HELP test_ipc_memory_used_gigabytes 已使用内存（GiB）",
            "# TYPE test_ipc_memory_used_gigabytes gauge",
            f"test_ipc_memory_used_gigabytes {values['memory_used_gib']}",
            "# HELP test_ipc_memory_total_gigabytes 内存总量（GiB）",
            "# TYPE test_ipc_memory_total_gigabytes gauge",
            f"test_ipc_memory_total_gigabytes {MEMORY_TOTAL_GIB}",
        ])
        return "\n".join(lines) + "\n"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
            self._thread = None


def create_simulator(
    protocol: str,
    host: str,
    mqtt_port: int,
    coap_port: int,
    prometheus_port: int,
    interval: float,
    seed: int | None,
) -> BaseSimulator:
    protocol = config.normalize_protocol(protocol)
    factories: dict[str, Callable[[str, int, float, int | None], BaseSimulator]] = {
        "mqtt": MqttFanSimulator,
        "coap": CoapSpraySimulator,
        "prometheus": PrometheusIpcSimulator,
    }
    port = {"mqtt": mqtt_port, "coap": coap_port, "prometheus": prometheus_port}[protocol]
    return factories[protocol](host, port, interval, seed)
