"""通用 TCP/UDP 应答模拟器：收到请求后返回 JSON 遥测，供网关 tcp/udp 驱动验证。

网关端点配置示例：
`protocol_identifier: {"request": "read", "response_format": "json", "path": "$.temperature"}`，
`connection` 中 `framing: "line"` 时响应以换行结尾。
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any

from .base import BaseSimulator

_DELIMITERS = {"\\n": "\n", "\\r": "\r", "\\t": "\t", "\\0": "\0"}


def decode_delimiter(raw: str) -> bytes:
    return _DELIMITERS.get(raw, raw).encode("utf-8")


def build_response(values: dict[str, Any], request: bytes, framing: str, delimiter: bytes) -> bytes:
    """按网关 tcp/udp 驱动的约定构造应答：JSON 体 + 可选行分隔符。"""
    payload: dict[str, Any] = dict(values)
    payload["echo"] = request.decode("utf-8", errors="replace").strip()
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if framing == "line":
        return body + delimiter
    return body


class _BaseTcpUdpSimulator(BaseSimulator):
    """周期生成最新值；网络服务线程负责应答。"""

    proto = "tcp"

    def __init__(self, name: str, config: dict[str, Any], reporter: Any | None = None):
        super().__init__(name, config, reporter)
        self._latest: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._server_socket: socket.socket | None = None

    @property
    def framing(self) -> str:
        return str(self.config.get("framing", "line"))

    @property
    def delimiter(self) -> bytes:
        return decode_delimiter(str(self.config.get("delimiter", "\\n")))

    def step(self) -> None:
        values = self.payload()
        with self._lock:
            self._latest = values
        self.record(values)

    def _current_values(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._latest)

    def _run_guarded_setup(self) -> None:
        host = str(self.config.get("host", "127.0.0.1"))
        port = int(self.config.get("port"))
        self._server_socket = self._bind(host, port)
        thread = threading.Thread(target=self._serve, daemon=True)
        thread.start()
        self.threads.append(thread)
        print(f"[{self.name}] 应答服务监听 {host}:{port} framing={self.framing}")

    def _bind(self, host: str, port: int) -> socket.socket:
        raise NotImplementedError

    def _serve(self) -> None:
        raise NotImplementedError

    def _teardown(self) -> None:
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None


class TcpSimulator(_BaseTcpUdpSimulator):
    proto = "tcp"

    def _bind(self, host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(8)
        sock.settimeout(0.5)
        return sock

    def _serve(self) -> None:
        assert self._server_socket is not None
        while not self.stop_event.is_set():
            try:
                conn, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _handle_conn(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        conn.settimeout(0.5)
        buffer = b""
        try:
            while not self.stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                requests, buffer = self._extract_requests(buffer)
                for request in requests:
                    response = build_response(self._current_values(), request, self.framing, self.delimiter)
                    conn.sendall(response)
        finally:
            conn.close()

    def _extract_requests(self, buffer: bytes) -> tuple[list[bytes], bytes]:
        if self.framing == "line":
            parts = buffer.split(self.delimiter)
            return [part for part in parts[:-1] if part], parts[-1]
        # raw：一次 recv 视为一个请求
        if buffer:
            return [buffer], b""
        return [], buffer


class UdpSimulator(_BaseTcpUdpSimulator):
    proto = "udp"

    def _bind(self, host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, port))
        sock.settimeout(0.5)
        return sock

    def _serve(self) -> None:
        assert self._server_socket is not None
        while not self.stop_event.is_set():
            try:
                data, addr = self._server_socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            request = data.rstrip(self.delimiter) if self.framing == "line" else data
            response = build_response(self._current_values(), request, self.framing, self.delimiter)
            try:
                self._server_socket.sendto(response, addr)
            except OSError:
                break
