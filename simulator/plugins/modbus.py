"""Modbus TCP 从站模拟器：保持寄存器周期更新，供网关 modbus-tcp 主站轮询验证。

寄存器映射由 config.registers 决定：`{"name": "temperature", "addr": 0, "scale": 10}`
表示将 temperature 值乘以 scale 取整后写入保持寄存器 addr。
网关端点配置示例：`protocol_identifier: {"slave": 1, "fc": 3, "addr": 0, "type": "uint16"}`。
"""
from __future__ import annotations

import threading
from typing import Any

from .base import BaseSimulator

FC_HOLDING_REGISTER = 3


class ModbusTcpSimulator(BaseSimulator):
    def __init__(self, name: str, config: dict[str, Any], reporter: Any | None = None):
        super().__init__(name, config, reporter)
        self._context: ModbusServerContext | None = None
        self._server_thread: threading.Thread | None = None

    def register_specs(self) -> list[dict[str, Any]]:
        specs = self.config.get("registers")
        if not specs:
            return [
                {"name": "temperature", "addr": 0, "scale": 10},
                {"name": "humidity", "addr": 1, "scale": 10},
            ]
        return list(specs)

    def _run_guarded_setup(self) -> None:
        try:
            from pymodbus.datastore import (
                ModbusSequentialDataBlock,
                ModbusServerContext,
                ModbusSlaveContext,
            )
            from pymodbus.server import StartTcpServer
        except ImportError as exc:
            raise RuntimeError("modbus-tcp 模拟器需要 pymodbus：pip install pymodbus") from exc

        host = str(self.config.get("host", "127.0.0.1"))
        port = int(self.config.get("port", 1502))
        slave_id = int(self.config.get("slave", 1))
        size = int(self.config.get("register_count", 100))
        block = ModbusSequentialDataBlock(0, [0] * size)
        slave = ModbusSlaveContext(hr=block)
        self._context = ModbusServerContext(slaves={slave_id: slave}, single=False)

        def serve() -> None:
            StartTcpServer(context=self._context, address=(host, port))

        self._server_thread = threading.Thread(target=serve, daemon=True)
        self._server_thread.start()
        print(f"[modbus-tcp] 从站 {slave_id} 监听 {host}:{port}")

    def step(self) -> None:
        assert self._context is not None
        slave_id = int(self.config.get("slave", 1))
        values = self.generate_values()
        written: dict[int, int] = {}
        for spec in self.register_specs():
            name = str(spec["name"])
            if name not in values:
                continue
            scale = float(spec.get("scale", 1))
            addr = int(spec["addr"])
            raw = int(round(values[name] * scale)) & 0xFFFF
            self._context[slave_id].setValues(FC_HOLDING_REGISTER, addr, [raw])
            written[addr] = raw
        payload = self.payload()
        payload["registers"] = written
        print(f"[modbus-tcp] 寄存器更新 {written}")
        self.record(payload)
