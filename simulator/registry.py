"""插件注册表：按名称惰性加载，避免可选依赖缺失影响其它插件。"""
from __future__ import annotations

from importlib import import_module
from typing import Any

_PLUGINS = {
    "modbus-tcp": "simulator.plugins.modbus:ModbusTcpSimulator",
    "mqtt": "simulator.plugins.mqtt:MqttSimulator",
    "tcp": "simulator.plugins.tcpudp:TcpSimulator",
    "udp": "simulator.plugins.tcpudp:UdpSimulator",
    "opc-ua": "simulator.plugins.opcua:OpcUaSimulator",
    "prometheus": "simulator.plugins.prometheus:PrometheusSimulator",
}


def available() -> list[str]:
    return sorted(_PLUGINS)


def create(name: str, config: dict[str, Any], reporter: Any | None = None):
    if name not in _PLUGINS:
        raise ValueError(f"未知模拟器插件: {name}，可选: {', '.join(available())}")
    module_name, class_name = _PLUGINS[name].split(":")
    module = import_module(module_name)
    return getattr(module, class_name)(name, config, reporter)
