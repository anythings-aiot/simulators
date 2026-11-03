"""模拟器插件系统测试：覆盖配置加载、插件注册、数值生成与 TCP/UDP 真实收发。"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from simulator import registry
from simulator.config import load_config
from simulator.plugins.base import BaseSimulator
from simulator.plugins.tcpudp import TcpSimulator, UdpSimulator, build_response, decode_delimiter
from simulator.types import Reporter

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestConfig:
    def test_load_default_config(self) -> None:
        data = load_config(CONFIG_PATH)
        assert "plugins" in data
        assert "modbus-tcp" in data["plugins"]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "absent.yaml")

    def test_invalid_plugins_section(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("plugins: [1, 2]\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(path)


class TestRegistry:
    def test_available_plugins(self) -> None:
        assert set(registry.available()) >= {"modbus-tcp", "mqtt", "tcp", "udp", "opc-ua"}

    def test_unknown_plugin_raises(self) -> None:
        with pytest.raises(ValueError, match="未知模拟器插件"):
            registry.create("not-exist", {})

    def test_create_tcp_simulator(self) -> None:
        sim = registry.create("tcp", {"port": 9999})
        assert isinstance(sim, TcpSimulator)


class TestBaseSimulator:
    def test_values_within_configured_range(self) -> None:
        sim = BaseSimulator(
            "demo",
            {"values": [{"name": "temp", "min": 10, "max": 12, "decimals": 1}]},
        )
        for _ in range(20):
            values = sim.generate_values()
            assert 10 <= values["temp"] <= 12
            assert round(values["temp"], 1) == values["temp"]

    def test_default_values_when_unset(self) -> None:
        sim = BaseSimulator("demo", {})
        values = sim.generate_values()
        assert set(values) == {"temperature", "humidity"}

    def test_seed_makes_output_deterministic(self) -> None:
        first = BaseSimulator("demo", {"seed": 42})
        second = BaseSimulator("demo", {"seed": 42})
        assert [first.generate_values() for _ in range(3)] == [
            second.generate_values() for _ in range(3)
        ]

    def test_payload_contains_metadata(self) -> None:
        sim = BaseSimulator("demo", {"device_id": "DEV-9", "values": [{"name": "v", "min": 1, "max": 2}]})
        payload = sim.payload()
        assert payload["device_id"] == "DEV-9"
        assert payload["protocol"] == "demo"
        assert isinstance(payload["ts"], int)
        assert "v" in payload

    def test_record_forwarded_to_reporter(self) -> None:
        reporter = Reporter()
        sim = BaseSimulator(
            "demo",
            {"interval_ms": 100, "values": [{"name": "v", "min": 1, "max": 2}]},
            reporter,
        )
        sim.step = lambda: sim.record(sim.payload())  # type: ignore[method-assign]
        sim.start()
        assert wait_until(lambda: len(reporter.records) > 0)
        sim.stop()
        assert reporter.records[0]["protocol"] == "demo"

    def test_setup_failure_propagates_to_caller(self) -> None:
        class Broken(BaseSimulator):
            def _run_guarded_setup(self) -> None:
                raise RuntimeError("端口被占用")

        with pytest.raises(RuntimeError, match="端口被占用"):
            Broken("broken", {}).start()


class TestFramingHelpers:
    def test_decode_delimiter_escapes(self) -> None:
        assert decode_delimiter("\\n") == b"\n"
        assert decode_delimiter("\\r") == b"\r"
        assert decode_delimiter("|") == b"|"

    def test_build_response_line_framing(self) -> None:
        body = build_response({"temperature": 25.5}, b"read", "line", b"\n")
        assert body.endswith(b"\n")
        payload = json.loads(body.decode("utf-8"))
        assert payload["temperature"] == 25.5
        assert payload["echo"] == "read"

    def test_build_response_raw_framing(self) -> None:
        body = build_response({"temperature": 1}, b"read", "raw", b"\n")
        assert not body.endswith(b"\n")
        assert json.loads(body.decode("utf-8"))["temperature"] == 1


class TestTcpSimulator:
    def test_request_response_roundtrip(self) -> None:
        port = free_port()
        sim = TcpSimulator(
            "tcp",
            {
                "host": "127.0.0.1",
                "port": port,
                "framing": "line",
                "interval_ms": 100,
                "values": [{"name": "temperature", "min": 20, "max": 21, "decimals": 1}],
            },
        )
        sim.start()
        try:
            assert wait_until(lambda: bool(sim._current_values()))
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(b"read\n")
                data = sock.recv(4096)
            payload = json.loads(data.decode("utf-8"))
            assert 20 <= payload["temperature"] <= 21
            assert payload["echo"] == "read"
        finally:
            sim.stop()

    def test_raw_framing_echoes_single_request(self) -> None:
        port = free_port()
        sim = TcpSimulator(
            "tcp",
            {
                "host": "127.0.0.1",
                "port": port,
                "framing": "raw",
                "interval_ms": 100,
                "values": [{"name": "v", "min": 1, "max": 2}],
            },
        )
        sim.start()
        try:
            assert wait_until(lambda: bool(sim._current_values()))
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(b"ping")
                payload = json.loads(sock.recv(4096).decode("utf-8"))
            assert payload["echo"] == "ping"
        finally:
            sim.stop()

    def test_stop_closes_listener(self) -> None:
        port = free_port()
        sim = TcpSimulator("tcp", {"host": "127.0.0.1", "port": port, "interval_ms": 100})
        sim.start()
        sim.stop()
        assert wait_until(
            lambda: _port_free(port), timeout=3
        ), "停止后端口应可重新绑定"


class TestUdpSimulator:
    def test_request_response_roundtrip(self) -> None:
        port = free_port()
        sim = UdpSimulator(
            "udp",
            {
                "host": "127.0.0.1",
                "port": port,
                "framing": "line",
                "interval_ms": 100,
                "values": [{"name": "humidity", "min": 40, "max": 41, "decimals": 1}],
            },
        )
        sim.start()
        try:
            assert wait_until(lambda: bool(sim._current_values()))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                sock.sendto(b"read\n", ("127.0.0.1", port))
                data, _ = sock.recvfrom(4096)
            payload = json.loads(data.decode("utf-8"))
            assert 40 <= payload["humidity"] <= 41
        finally:
            sim.stop()


class TestModbusSimulator:
    def test_register_specs_defaults(self) -> None:
        from simulator.plugins.modbus import ModbusTcpSimulator

        sim = ModbusTcpSimulator("modbus-tcp", {})
        specs = sim.register_specs()
        assert specs[0] == {"name": "temperature", "addr": 0, "scale": 10}

    def test_register_specs_from_config(self) -> None:
        from simulator.plugins.modbus import ModbusTcpSimulator

        sim = ModbusTcpSimulator(
            "modbus-tcp",
            {"registers": [{"name": "pressure", "addr": 5, "scale": 100}]},
        )
        assert sim.register_specs() == [{"name": "pressure", "addr": 5, "scale": 100}]

    def test_slave_serves_registers(self) -> None:
        """真实启动 Modbus 从站并用主站读取，验证网关轮询链路。"""
        pymodbus = pytest.importorskip("pymodbus.client")
        port = free_port()
        sim = registry.create(
            "modbus-tcp",
            {
                "host": "127.0.0.1",
                "port": port,
                "slave": 1,
                "interval_ms": 100,
                "values": [{"name": "temperature", "min": 25, "max": 25, "decimals": 0}],
                "registers": [{"name": "temperature", "addr": 0, "scale": 10}],
            },
        )
        sim.start()
        try:
            assert wait_until(lambda: bool(sim._context is not None), timeout=3)
            time.sleep(0.3)
            client = pymodbus.ModbusTcpClient("127.0.0.1", port=port, timeout=2)
            assert client.connect(), "主站应能连上模拟从站"
            try:
                response = client.read_holding_registers(0, 1, slave=1)
                assert not response.isError()
                assert response.registers[0] == 250
            finally:
                client.close()
        finally:
            sim.stop()


class TestOpcUaSimulator:
    def test_variable_readable_by_client(self) -> None:
        """真实启动 OPC UA 服务器并以客户端读取变量，验证网关客户端链路。"""
        pytest.importorskip("asyncua")
        import asyncio

        from asyncua import Client

        port = free_port()
        sim = registry.create(
            "opc-ua",
            {
                "host": "127.0.0.1",
                "port": port,
                "namespace": "urn:anythings:test",
                "interval_ms": 100,
                "device_id": "SIM-OPCUA-T",
                "values": [{"name": "temperature", "min": 20, "max": 21, "decimals": 1}],
            },
        )
        sim.start()
        try:

            async def read_value() -> float:
                async with Client(url=f"opc.tcp://127.0.0.1:{port}") as client:
                    idx = await client.get_namespace_index("urn:anythings:test")
                    node = await client.nodes.objects.get_child(
                        [f"{idx}:SIM-OPCUA-T", f"{idx}:temperature"]
                    )
                    return float(await node.read_value())

            value = asyncio.run(read_value())
            assert 20 <= value <= 21
        finally:
            sim.stop()


class TestPrometheusSimulator:
    def test_render_metrics_format(self) -> None:
        from simulator.plugins.prometheus import render_metrics

        text = render_metrics(
            {"line_temperature": 25.5, "line_output": 120.0},
            {"line": "A", "station": "SIM"},
        )
        assert "# TYPE line_temperature gauge" in text
        assert 'line_temperature{line="A",station="SIM"} 25.5' in text
        assert 'line_output{line="A",station="SIM"} 120.0' in text

    def test_label_values_are_escaped(self) -> None:
        from simulator.plugins.prometheus import render_metrics

        text = render_metrics({"m": 1.0}, {"path": 'C:\\tmp"x'})
        assert r'path="C:\\tmp\"x"' in text

    def test_exporter_serves_metrics(self) -> None:
        port = free_port()
        sim = registry.create(
            "prometheus",
            {
                "host": "127.0.0.1",
                "port": port,
                "interval_ms": 100,
                "device_id": "SIM-PROM-T",
                "labels": {"line": "A"},
                "values": [{"name": "line_temperature", "min": 20, "max": 21, "decimals": 1}],
            },
        )
        sim.start()
        try:
            body = _http_get(port, "/metrics")
            assert "# TYPE line_temperature gauge" in body
            line = next(l for l in body.splitlines() if l.startswith("line_temperature{"))
            value = float(line.rsplit(" ", 1)[1])
            assert 20 <= value <= 21
            assert 'line="A"' in line
        finally:
            sim.stop()

    def test_unknown_path_returns_404(self) -> None:
        port = free_port()
        sim = registry.create(
            "prometheus",
            {"host": "127.0.0.1", "port": port, "interval_ms": 100, "path": "/custom"},
        )
        sim.start()
        try:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _http_get(port, "/metrics")
            assert excinfo.value.code == 404
            assert "line_temperature" not in _http_get(port, "/custom")
        finally:
            sim.stop()


def _http_get(port: int, path: str) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return response.read().decode("utf-8")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
