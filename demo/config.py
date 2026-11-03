"""测试环境固定配置：设备树、三协议设备、端点与告警规则。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

API_DEFAULT = "http://127.0.0.1:8080"
SPACE_DEFAULT = "default"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "anythings_admin"

SITE_ID = "SZ01"
STATION_ID = "ST01"
GATEWAY_ID = "GW03"
TEST_SUITE = "anythings-tools-test"
MANAGED_BY = "tools/test"

DASHBOARD_PROJECT_ID = "anythings-test"
DASHBOARD_PROJECT_NAME = "AnyThings 测试设备"
DASHBOARD_ID = "anythings-test-overview"
DASHBOARD_NAME = "测试设备实时数据"

MQTT_TOPIC = "anythings/test/fan"
COAP_PATH = "/spray/system"
COAP_EXTEND_PATH = "/spray/extend"
COAP_SIM_PATH = "/spray/sim"
PROMETHEUS_PATH = "/metrics"

# 喷淋自动延长：粉尘超限时由控制中心处理单元下发动作，网关写入 CoAP 设备。
SPRAY_ACTION_CODE = "extend_spray"
SPRAY_UNIT_ID = "spray_extend"
SPRAY_CHAIN_ID = "test-spray-extend"
SPRAY_EXTEND_SECONDS = 30
SPRAY_PM10_THRESHOLD = 150
SPRAY_TSP_THRESHOLD = 400
SPRAY_COOLDOWN_MS = 60000

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
COAP_HOST = "127.0.0.1"
COAP_PORT = 5683
PROMETHEUS_HOST = "127.0.0.1"
PROMETHEUS_PORT = 9901

PROTOCOL_ALIASES = {
    "mqtt": "mqtt",
    "coap": "coap",
    "prometheus": "prometheus",
    "promethus": "prometheus",
    "prom": "prometheus",
}


@dataclass(frozen=True)
class EndpointSpec:
    attribute_key: str
    unit: str
    description: str
    protocol_identifier: dict[str, Any]


@dataclass(frozen=True)
class AlarmSpec:
    attribute_key: str
    operator: str
    threshold: float
    level: str
    name: str
    message: str
    code: str


@dataclass(frozen=True)
class DeviceSpec:
    protocol: str
    device_id: str
    name: str
    source_id: str
    connection: dict[str, Any]
    endpoints: tuple[EndpointSpec, ...]
    alarms: tuple[AlarmSpec, ...]


ORG_TREE: tuple[dict[str, str], ...] = (
    {"node_id": "TEST-SITE", "node_type": "factory", "parent_id": "", "name": "测试工厂"},
    {"node_id": "TEST-WORKSHOP", "node_type": "workshop", "parent_id": "TEST-SITE", "name": "测试车间"},
    {"node_id": "TEST-LINE", "node_type": "line", "parent_id": "TEST-WORKSHOP", "name": "测试产线"},
)

TEST_DEVICE_IDS = {
    "mqtt": "TEST-MQTT-FAN",
    "coap": "TEST-COAP-SPRAY",
    "prometheus": "TEST-PROM-IPC",
}

TEST_DEVICE_NAMES = {
    "mqtt": "测试温控风扇",
    "coap": "测试喷淋系统",
    "prometheus": "测试工控机",
}

ALARM_BY_PROTOCOL: dict[str, tuple[AlarmSpec, ...]] = {
    "mqtt": (
        AlarmSpec(
            "temperature", ">", 35, "major", "温控风扇环境温度过高",
            "环境温度超过 35℃", "test_fan_temperature_high",
        ),
        AlarmSpec(
            "fan_speed", ">", 1800, "warning", "温控风扇转速偏高",
            "风扇转速超过 1800rpm", "test_fan_speed_high",
        ),
    ),
    "coap": (
        AlarmSpec(
            "pm10", ">", 150, "major", "喷淋系统 PM10 超标",
            "PM10 超过 150µg/m³", "test_spray_pm10_high",
        ),
        AlarmSpec(
            "tsp", ">", 400, "major", "喷淋系统 TSP 超标",
            "TSP 超过 400µg/m³", "test_spray_tsp_high",
        ),
        AlarmSpec(
            "humidity", "<", 35, "warning", "喷淋系统环境湿度过低",
            "环境湿度低于 35%", "test_spray_humidity_low",
        ),
        AlarmSpec(
            "spray_duration", ">", 30, "warning", "喷淋系统持续喷淋超时",
            "单次喷淋时长超过 30s", "test_spray_duration_high",
        ),
    ),
    "prometheus": (
        AlarmSpec(
            "cpu_temperature", ">", 75, "major", "工控机 CPU 温度过高",
            "CPU 温度超过 75℃", "test_ipc_cpu_temperature_high",
        ),
        AlarmSpec(
            "cpu_usage_total", ">", 85, "major", "工控机 CPU 使用率过高",
            "CPU 总使用率超过 85%", "test_ipc_cpu_usage_high",
        ),
        AlarmSpec(
            "memory_usage", ">", 85, "warning", "工控机内存使用率过高",
            "内存使用率超过 85%（最大 8GiB）", "test_ipc_memory_usage_high",
        ),
    ),
}


def normalize_protocol(value: str) -> str:
    key = value.strip().lower()
    protocol = PROTOCOL_ALIASES.get(key)
    if protocol is None:
        raise ValueError(f"不支持的协议: {value}（可选 mqtt/coap/prometheus）")
    return protocol


def protocol_label(protocol: str) -> str:
    return {"mqtt": "MQTT", "coap": "CoAP", "prometheus": "Prometheus"}[protocol]


def endpoint_id(protocol: str, device_id: str, attribute_key: str) -> str:
    return f"{SITE_ID}.{STATION_ID}.{GATEWAY_ID}.{protocol.upper()}.{device_id}.{attribute_key}"


def endpoint_prefix(protocol: str, device_id: str) -> str:
    """链路的端点前缀，匹配该设备的全部端点（不含属性键）。"""
    return f"{SITE_ID}.{STATION_ID}.{GATEWAY_ID}.{protocol.upper()}.{device_id}"


def build_device_spec(
    protocol: str,
    host: str = "127.0.0.1",
    mqtt_port: int = MQTT_PORT,
    coap_port: int = COAP_PORT,
    prometheus_port: int = PROMETHEUS_PORT,
) -> DeviceSpec:
    protocol = normalize_protocol(protocol)
    if protocol == "mqtt":
        connection = {"host": host, "port": mqtt_port, "client_id": "cc-test-fan"}
        endpoints = (
            EndpointSpec("temperature", "℃", "环境温度", {"topic": MQTT_TOPIC, "path": "$.temperature"}),
            EndpointSpec("fan_speed", "rpm", "风扇转速", {"topic": MQTT_TOPIC, "path": "$.fan_speed"}),
        )
    elif protocol == "coap":
        connection = {
            "host": host, "port": coap_port, "timeout_ms": 2000, "confirmable": True,
        }
        endpoints = (
            EndpointSpec("humidity", "%", "环境湿度", _coap_point("$.humidity")),
            EndpointSpec("pm10", "µg/m³", "PM10 浓度", _coap_point("$.pm10")),
            EndpointSpec("tsp", "µg/m³", "TSP 浓度", _coap_point("$.tsp")),
            EndpointSpec("spray_duration", "s", "当前喷淋时长", _coap_point("$.spray_duration_s")),
            EndpointSpec("spray_extend", "s", "延长喷淋指令", _coap_write_point()),
        )
    else:
        connection = {
            "url": f"http://{host}:{prometheus_port}{PROMETHEUS_PATH}", "timeout_ms": 2000,
        }
        endpoints = (
            EndpointSpec(
                "cpu_temperature", "℃", "CPU 温度",
                {"metric": "test_ipc_cpu_temperature_celsius", "aggregate": "first"},
            ),
            EndpointSpec(
                "cpu_usage_total", "%", "CPU 总使用率",
                {"metric": "test_ipc_cpu_usage_percent", "labels": {"core": "total"}, "aggregate": "first"},
            ),
            *(
                EndpointSpec(
                    f"cpu_usage_core_{core}", "%", f"CPU {core} 号核心使用率",
                    {"metric": "test_ipc_cpu_usage_percent", "labels": {"core": str(core)}, "aggregate": "first"},
                )
                for core in range(4)
            ),
            EndpointSpec(
                "memory_usage", "%", "内存使用率（最大 8GiB）",
                {"metric": "test_ipc_memory_usage_percent", "aggregate": "first"},
            ),
        )
    return DeviceSpec(
        protocol=protocol,
        device_id=TEST_DEVICE_IDS[protocol],
        name=TEST_DEVICE_NAMES[protocol],
        source_id=f"TEST-{protocol.upper()}",
        connection=connection,
        endpoints=endpoints,
        alarms=ALARM_BY_PROTOCOL[protocol],
    )


def _coap_point(json_path: str) -> dict[str, Any]:
    return {
        "path": COAP_PATH,
        "method": "get",
        "response_format": "json",
        "json_path": json_path,
    }


def _coap_write_point() -> dict[str, Any]:
    """写端点：GET 返回剩余延长秒数，命令下发时 PUT 秒数到同一路径。"""
    return {
        "path": COAP_EXTEND_PATH,
        "method": "get",
        "response_format": "text",
        "write_method": "put",
        "write_body": "${value}",
    }


def spray_endpoint_id() -> str:
    return endpoint_id("coap", TEST_DEVICE_IDS["coap"], "spray_extend")


def device_payload(spec: DeviceSpec, org_id: str) -> dict[str, Any]:
    endpoints = []
    for endpoint in spec.endpoints:
        endpoints.append({
            "endpoint_id": endpoint_id(spec.protocol, spec.device_id, endpoint.attribute_key),
            "station_id": STATION_ID,
            "gateway_id": GATEWAY_ID,
            "source_id": spec.source_id,
            "attribute_key": endpoint.attribute_key,
            "protocol": spec.protocol,
            "protocol_identifier": endpoint.protocol_identifier,
            "connection": spec.connection,
            "change_policy": {"mode": "always", "value": 0},
            "collect_interval_ms": 1000,
            "scale": 1,
            "offset": 0,
            "unit": endpoint.unit,
            "enabled": True,
            "attributes": {
                "test_suite": TEST_SUITE,
                "description": endpoint.description,
            },
        })
    return {
        "device_id": spec.device_id,
        "name": spec.name,
        "org_id": org_id,
        "enabled": True,
        "attributes": {
            "test_suite": TEST_SUITE,
            "managed_by": MANAGED_BY,
            "protocol": spec.protocol,
        },
        "endpoints": endpoints,
    }


def alarm_payload(spec: DeviceSpec, alarm: AlarmSpec) -> dict[str, Any]:
    return {
        "name": alarm.name,
        "data_type": "point.telemetry",
        "device_id": spec.device_id,
        "attribute_key": alarm.attribute_key,
        "operator": alarm.operator,
        "threshold": alarm.threshold,
        "level": alarm.level,
        "code": alarm.code,
        "message": alarm.message,
        "enabled": True,
    }


def is_test_device(device: dict[str, Any]) -> bool:
    attributes = device.get("attributes") or {}
    return attributes.get("test_suite") == TEST_SUITE
