"""OPC UA 服务器模拟器：暴露可读变量，供网关 opc-ua 客户端驱动验证。

网关端点配置示例：`protocol_identifier: {"node_id": "ns=2;s=temperature"}`，
`connection.endpoint_url` 指向 `opc.tcp://127.0.0.1:4840`。
依赖 asyncua（可选安装：pip install asyncua）。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from .base import BaseSimulator


class OpcUaSimulator(BaseSimulator):
    def __init__(self, name: str, config: dict[str, Any], reporter: Any | None = None):
        super().__init__(name, config, reporter)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._server = None
        self._nodes: dict[str, Any] = {}
        self._init_error: Exception | None = None

    def _run_guarded_setup(self) -> None:
        try:
            import asyncua  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("opc-ua 模拟器需要 asyncua：pip install asyncua") from exc
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError(f"OPC UA 服务器启动超时: {self._init_error}")
        self.threads.append(self._thread)

    def _serve(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_server())
            self._loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            self._init_error = exc
            self._ready.set()

    async def _start_server(self) -> None:
        from asyncua import Server

        host = str(self.config.get("host", "0.0.0.0"))
        port = int(self.config.get("port", 4840))
        namespace = str(self.config.get("namespace", "urn:anythings:simulator"))
        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://{host}:{port}")
        idx = await server.register_namespace(namespace)
        objects = server.nodes.objects
        group = await objects.add_object(idx, self.device_id)
        for spec in self.value_specs():
            name = str(spec["name"])
            node = await group.add_variable(idx, name, 0.0)
            self._nodes[name] = node
        await server.start()
        self._server = server
        print(f"[opc-ua] 服务器监听 {host}:{port} 节点基数 ns={idx}")
        for name, node in self._nodes.items():
            # NodeId 依赖注册顺序，打印出来便于直接填入网关 protocol_identifier.node_id
            print(f"[opc-ua] 变量 {name} node_id={node.nodeid.to_string()}")
        self._ready.set()

    def step(self) -> None:
        if self._loop is None or not self._nodes:
            return
        values = self.generate_values()
        for name, value in values.items():
            node = self._nodes.get(name)
            if node is None:
                continue
            asyncio.run_coroutine_threadsafe(node.write_value(float(value)), self._loop)
        payload = self.payload()
        self.record(payload)

    def _teardown(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._server is not None:
            future = asyncio.run_coroutine_threadsafe(self._server.stop(), loop)
            try:
                future.result(timeout=5)
            except Exception:  # noqa: BLE001 - 停止失败不影响进程退出
                pass
            self._server = None
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        if not loop.is_running():
            loop.close()
        self._loop = None
