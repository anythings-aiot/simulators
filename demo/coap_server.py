"""最小 CoAP 服务端：实现 RFC 7252 的资源读取与写入，响应 JSON/纯文本。"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

TYPE_CON = 0
TYPE_NON = 1
TYPE_ACK = 2
TYPE_RST = 3

CODE_GET = 1
CODE_POST = 2
CODE_PUT = 3
CODE_CONTENT = 69
CODE_CHANGED = 68
CODE_BAD_REQUEST = 128
CODE_NOT_FOUND = 132
CODE_METHOD_NOT_ALLOWED = 133

OPTION_CONTENT_FORMAT = 12
OPTION_URI_PATH = 11
OPTION_URI_QUERY = 15

CONTENT_FORMAT_TEXT = 0
CONTENT_FORMAT_JSON = 50


@dataclass
class CoapMessage:
    msg_type: int
    code: int
    message_id: int
    token: bytes
    options: dict[int, list[bytes]]
    payload: bytes

    @property
    def path(self) -> str:
        parts = [value.decode("utf-8", errors="replace") for value in self.options.get(OPTION_URI_PATH, [])]
        return "/" + "/".join(parts)


def parse_message(data: bytes) -> CoapMessage | None:
    if len(data) < 4:
        return None
    first, code, message_id = data[0], data[1], int.from_bytes(data[2:4], "big")
    version = first >> 6
    if version != 1:
        return None
    msg_type = (first >> 4) & 0x03
    token_length = first & 0x0F
    if token_length > 8 or len(data) < 4 + token_length:
        return None
    token = data[4:4 + token_length]
    options, payload = _parse_options(data[4 + token_length:])
    return CoapMessage(msg_type, code, message_id, token, options, payload)


def _parse_options(raw: bytes) -> tuple[dict[int, list[bytes]], bytes]:
    options: dict[int, list[bytes]] = {}
    cursor = 0
    option_number = 0
    while cursor < len(raw):
        header = raw[cursor]
        cursor += 1
        if header == 0xFF:
            return options, raw[cursor:]
        delta = (header >> 4) & 0x0F
        length = header & 0x0F
        delta, cursor = _read_extended(delta, raw, cursor)
        length, cursor = _read_extended(length, raw, cursor)
        if cursor + length > len(raw):
            break
        option_number += delta
        options.setdefault(option_number, []).append(raw[cursor:cursor + length])
        cursor += length
    return options, b""


def _read_extended(value: int, raw: bytes, cursor: int) -> tuple[int, int]:
    if value == 13:
        if cursor >= len(raw):
            return value, cursor
        return raw[cursor] + 13, cursor + 1
    if value == 14:
        if cursor + 2 > len(raw):
            return value, cursor
        return int.from_bytes(raw[cursor:cursor + 2], "big") + 269, cursor + 2
    return value, cursor


def _extended(value: int) -> tuple[int, bytes]:
    if value < 13:
        return value, b""
    if value < 269:
        return 13, bytes([value - 13])
    if value < 65805:
        return 14, struct.pack("!H", value - 269)
    raise ValueError(f"CoAP 选项长度超限: {value}")


def build_message(
    msg_type: int,
    code: int,
    message_id: int,
    token: bytes,
    options: list[tuple[int, bytes]],
    payload: bytes,
) -> bytes:
    header = bytes([(1 << 6) | (msg_type << 4) | len(token), code])
    body = bytearray(header + message_id.to_bytes(2, "big") + token)
    last_number = 0
    for number, value in sorted(options, key=lambda item: item[0]):
        delta, delta_extra = _extended(number - last_number)
        length, length_extra = _extended(len(value))
        body.append((delta << 4) | length)
        body.extend(delta_extra)
        body.extend(length_extra)
        body.extend(value)
        last_number = number
    if payload:
        body.append(0xFF)
        body.extend(payload)
    return bytes(body)


def build_response(request: CoapMessage, code: int, payload: bytes, content_format: int) -> bytes:
    msg_type = TYPE_ACK if request.msg_type == TYPE_CON else TYPE_NON
    return build_message(
        msg_type, code, request.message_id, request.token,
        [(OPTION_CONTENT_FORMAT, bytes([content_format]) if content_format else b"")],
        payload,
    )


class CoapServer:
    def __init__(
        self,
        host: str,
        port: int,
        state_provider: Callable[[], dict[str, Any]],
        write_handler: Callable[[str, bytes], tuple[int, str]] | None = None,
        verbose: bool = False,
    ):
        self.host = host
        self.port = port
        self._state_provider = state_provider
        self._write_handler = write_handler
        self._verbose = verbose
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._log_lock = threading.Lock()
        self._seen_paths: set[str] = set()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(0.5)
        self._socket = sock
        self._thread = threading.Thread(target=self._serve, name="coap-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                request = parse_message(data)
                if request is None:
                    continue
                response = self._handle(request)
                if response:
                    self._socket.sendto(response, address)
            except Exception as exc:  # noqa: BLE001 - 单个请求失败不退出服务
                self._safe_log(f"[CoAP] 请求处理失败: {exc}")

    def _handle(self, request: CoapMessage) -> bytes | None:
        if request.msg_type not in {TYPE_CON, TYPE_NON}:
            return None
        path = request.path or "/"
        if self._verbose and path not in self._seen_paths:
            self._seen_paths.add(path)
            self._safe_log(f"[CoAP] 首次请求 {path}")
        if request.code in {CODE_PUT, CODE_POST}:
            if self._write_handler is None:
                return build_response(request, CODE_METHOD_NOT_ALLOWED,
                                      "仅支持 GET".encode(), CONTENT_FORMAT_TEXT)
            code, write_body = self._write_handler(path, request.payload)
            return build_response(request, code, write_body.encode("utf-8"), CONTENT_FORMAT_TEXT)
        if request.code != CODE_GET:
            return build_response(request, CODE_METHOD_NOT_ALLOWED,
                                  "仅支持 GET".encode(), CONTENT_FORMAT_TEXT)
        state = self._state_provider()
        if path == "/spray/system":
            system_body = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return build_response(request, CODE_CONTENT, system_body, CONTENT_FORMAT_JSON)
        if path == "/spray/humidity":
            return build_response(request, CODE_CONTENT, f"{state['humidity']:.2f}".encode(), CONTENT_FORMAT_TEXT)
        if path == "/spray/pm10":
            return build_response(request, CODE_CONTENT, f"{state['pm10']:.2f}".encode(), CONTENT_FORMAT_TEXT)
        if path == "/spray/tsp":
            return build_response(request, CODE_CONTENT, f"{state['tsp']:.2f}".encode(), CONTENT_FORMAT_TEXT)
        if path == "/spray/duration":
            return build_response(request, CODE_CONTENT, str(state["spray_duration_s"]).encode(), CONTENT_FORMAT_TEXT)
        if path == "/spray/extend":
            remaining = int(state.get("spray_remaining_s", 0))
            return build_response(request, CODE_CONTENT, str(remaining).encode(), CONTENT_FORMAT_TEXT)
        if path == "/health":
            return build_response(request, CODE_CONTENT, b"ok", CONTENT_FORMAT_TEXT)
        if path == "/":
            root_body = (
                "CoAP 喷淋测试设备\n"
                "资源: /spray/system /spray/humidity /spray/pm10 /spray/tsp "
                "/spray/duration /spray/extend\n"
                "写入: PUT /spray/extend <秒数>，PUT /spray/sim {\"pm10\":180,\"hold_s\":60}\n"
            )
            return build_response(request, CODE_CONTENT, root_body.encode("utf-8"), CONTENT_FORMAT_TEXT)
        return build_response(request, CODE_NOT_FOUND, b"not found", CONTENT_FORMAT_TEXT)

    def _safe_log(self, message: str) -> None:
        with self._log_lock:
            print(message, flush=True)


def coap_get(host: str, port: int, path: str, timeout: float = 2.0) -> str:
    """供本地自检使用的极简 CoAP 客户端。"""
    return coap_exchange(host, port, path, CODE_GET, b"", timeout)


def coap_write(host: str, port: int, path: str, payload: bytes,
               timeout: float = 2.0, code: int = CODE_PUT) -> str:
    """发送 CoAP 写请求，返回响应文本，用于验证命令下发后的设备状态。"""
    return coap_exchange(host, port, path, code, payload, timeout)


def coap_exchange(host: str, port: int, path: str, code: int, payload: bytes,
                  timeout: float = 2.0) -> str:
    token = os.urandom(4)
    message_id = int.from_bytes(os.urandom(2), "big")
    options = [
        (OPTION_URI_PATH, segment.encode("utf-8"))
        for segment in path.strip("/").split("/") if segment
    ]
    packet = build_message(TYPE_CON, code, message_id, token, options, payload)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (host, port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data, _ = sock.recvfrom(65535)
            response = parse_message(data)
            if response and response.token == token:
                if response.code >= 128:
                    raise RuntimeError(
                        f"CoAP 返回错误码 {response.code}: "
                        f"{response.payload.decode('utf-8', errors='replace')}"
                    )
                return response.payload.decode("utf-8", errors="replace")
    raise TimeoutError("CoAP 请求超时")
