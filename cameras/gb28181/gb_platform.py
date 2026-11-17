#!/usr/bin/env python3
"""GB28181 信令平台模拟器（仅用于本地联调）。

职责：
- SIP 5060/UDP：设备注册（Digest）、心跳、目录查询、PTZ DeviceControl、INVITE/BYE。
- HTTP 8025：网关使用的 `/api/v1/devices`、`/api/v1/ptz`、`/api/v1/streams`。
- 媒体：向 ZLMediaKit 申请 RTP 收流端口，把 SDP 回给设备，返回 `rtsp://<zlm>/rtp/<stream_id>`。

本文件是开发态等价实现，不参与生产部署；生产使用 srs-sip + SRS 6。
"""
import argparse
import base64
import json
import queue
import random
import re
import signal
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gb28181_sim import digest_response, encode_ptz_cmd, parse_sip, sip_response

STOP = threading.Event()

PTZ_CODES = {
    "stop": 0x00,
    "right": 0x08,
    "left": 0x04,
    "up": 0x02,
    "down": 0x01,
    "up_left": 0x06,
    "up_right": 0x0A,
    "down_left": 0x05,
    "down_right": 0x09,
    "zoom_in": 0x10,
    "zoom_out": 0x20,
}


def log(message: str) -> None:
    print(f"[platform] {message}", flush=True)


class Device:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.name = device_id
        self.addr: tuple[str, int] | None = None
        self.expires_at = 0.0
        self.channels: dict[str, str] = {}

    def online(self) -> bool:
        return self.addr is not None and time.monotonic() < self.expires_at

    def to_json(self) -> dict:
        online = self.online()
        channels = [
            {"channel_id": channel_id, "name": name, "status": "online" if online else "offline", "ptz": True}
            for channel_id, name in self.channels.items()
        ]
        return {
            "device_id": self.device_id,
            "name": self.name,
            "status": "online" if online else "offline",
            "channels": channels,
        }


class Platform:
    def __init__(self, args):
        self.args = args
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((args.listen_host, args.sip_port))
        self.socket.settimeout(0.5)
        self.devices: dict[str, Device] = {}
        self.streams: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.pending: dict[str, queue.Queue] = {}
        self.serial = random.randrange(1, 1000)
        self.nonces: set[str] = set()

    # ---------- SIP 发送 ----------

    def _next_serial(self) -> int:
        self.serial = (self.serial + 1) % 100000
        return self.serial

    def _send_message(self, device: Device, body: str) -> None:
        if device.addr is None:
            raise RuntimeError(f"设备 {device.device_id} 尚未注册")
        call_id = uuid.uuid4().hex
        platform_aor = f"sip:{self.args.platform_id}@{self.args.realm}"
        device_aor = f"sip:{device.device_id}@{self.args.realm}"
        headers = [
            f"Via: SIP/2.0/UDP {self.args.host_ip}:{self.args.sip_port};branch=z9hG4bK{uuid.uuid4().hex};rport",
            "Max-Forwards: 70",
            f"From: <{platform_aor}>;tag={uuid.uuid4().hex[:8]}",
            f"To: <{device_aor}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 MESSAGE",
            f"Contact: <{platform_aor}>",
            "Content-Type: Application/MANSCDP+xml",
            f"Content-Length: {len(body.encode())}",
        ]
        packet = "\r\n".join([f"MESSAGE {device_aor} SIP/2.0", *headers, "", body]).encode()
        self.socket.sendto(packet, device.addr)

    def _query_catalog(self, device: Device) -> None:
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?><Query><CmdType>Catalog</CmdType>'
            f"<SN>{self._next_serial()}</SN><DeviceID>{device.device_id}</DeviceID></Query>"
        )
        try:
            self._send_message(device, body)
        except RuntimeError as exc:
            log(f"目录查询失败：{exc}")

    def _send_ptz(self, device: Device, channel_id: str, command: int, pan: int, tilt: int, zoom: int) -> None:
        encoded = encode_ptz_cmd(command, max(0, min(255, pan)), max(0, min(255, tilt)), max(0, min(15, zoom)))
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?><Control><CmdType>DeviceControl</CmdType>'
            f"<SN>{self._next_serial()}</SN><DeviceID>{channel_id}</DeviceID>"
            f"<PTZCmd>{base64.b64encode(encoded).decode()}</PTZCmd></Control>"
        )
        self._send_message(device, body)
        log(f"PTZ 下发 device={device.device_id} channel={channel_id} cmd=0x{command:02X} pan={pan} tilt={tilt} zoom={zoom}")

    # ---------- SIP 接收 ----------

    def run(self) -> None:
        while not STOP.is_set():
            try:
                packet, peer = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            start, headers, body = parse_sip(packet)
            if start.startswith("SIP/2.0"):
                self._on_response(start, headers)
            else:
                self._on_request(packet, start, headers, body, peer)

    def _on_response(self, start: str, headers: dict[str, str]) -> None:
        waiter = self.pending.get(headers.get("call-id", ""))
        if waiter is not None:
            waiter.put((start, headers))

    def _on_request(self, packet: bytes, start: str, headers: dict[str, str], body: bytes, peer) -> None:
        method = start.split(" ", 1)[0].upper()
        if method == "REGISTER":
            self._on_register(packet, headers, peer)
            return
        if method == "MESSAGE":
            self.socket.sendto(sip_response(packet, 200, "OK"), peer)
            self._on_message(headers, body, peer)
            return
        if method == "ACK":
            return
        self.socket.sendto(sip_response(packet, 200, "OK"), peer)

    def _on_register(self, packet: bytes, headers: dict[str, str], peer) -> None:
        source = headers.get("from", "")
        match = re.search(r"sip:([\w.-]+)@", source)
        device_id = match.group(1) if match else ""
        if not device_id:
            self.socket.sendto(sip_response(packet, 400, "Bad Request"), peer)
            return
        authorization = headers.get("authorization", "")
        if not authorization:
            nonce = uuid.uuid4().hex
            self.nonces.add(nonce)
            extra = [
                f'WWW-Authenticate: Digest realm="{self.args.realm}", nonce="{nonce}", algorithm=MD5, qop="auth"'
            ]
            self.socket.sendto(sip_response(packet, 401, "Unauthorized", extra=extra), peer)
            return
        if not self._check_digest(authorization, device_id):
            self.socket.sendto(sip_response(packet, 403, "Forbidden"), peer)
            log(f"注册鉴权失败 device={device_id}")
            return
        with self.lock:
            device = self.devices.get(device_id)
            first = device is None
            if device is None:
                device = Device(device_id)
                self.devices[device_id] = device
            device.addr = peer
            device.expires_at = time.monotonic() + self.args.expires
        self.socket.sendto(sip_response(packet, 200, "OK"), peer)
        log(f"设备注册成功 {device_id} -> {peer[0]}:{peer[1]}")
        if first or not device.channels:
            self._query_catalog(device)

    def _check_digest(self, authorization: str, device_id: str) -> bool:
        fields = {
            key.lower(): quoted if quoted else unquoted
            for key, quoted, unquoted in re.findall(r'([\w-]+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', authorization)
        }
        realm = fields.get("realm", "")
        nonce = fields.get("nonce", "")
        uri = fields.get("uri", "")
        qop = fields.get("qop", "")
        if realm != self.args.realm or not nonce:
            return False
        expected = digest_response(
            device_id, self.args.password, realm, nonce, "REGISTER", uri,
            qop, fields.get("nc", ""), fields.get("cnonce", ""),
        )
        return expected == fields.get("response", "")

    def _on_message(self, headers: dict[str, str], body: bytes, peer) -> None:
        text = body.decode("utf-8", "replace")
        if "<CmdType>Keepalive</CmdType>" in text:
            device_id = _xml_value(text, "DeviceID")
            with self.lock:
                device = self.devices.get(device_id)
                if device is not None:
                    device.addr = peer
                    device.expires_at = time.monotonic() + self.args.expires
            return
        if "<CmdType>Catalog</CmdType>" in text:
            device_id = _xml_value(text, "DeviceID")
            with self.lock:
                device = self.devices.get(device_id)
                if device is None:
                    return
                for item in re.findall(r"<Item>(.*?)</Item>", text, re.S):
                    channel_id = _xml_value(item, "DeviceID")
                    if channel_id:
                        device.channels[channel_id] = _xml_value(item, "Name") or channel_id
                log(f"目录同步 {device_id} 通道 {list(device.channels)}")

    # ---------- 媒体 ----------

    def open_stream(self, device_id: str, channel_id: str) -> dict:
        with self.lock:
            device = self.devices.get(device_id)
        if device is None:
            raise NotFound(f"设备 {device_id} 未注册")
        if not device.online():
            raise NotFound(f"设备 {device_id} 离线")
        if device.channels and channel_id not in device.channels:
            raise NotFound(f"通道 {channel_id} 不存在")
        existing = self.streams.get(channel_id)
        if existing is not None and self._zlm_stream_alive(channel_id):
            return existing
        if existing is not None:
            log(f"媒体流已失效，重新建立 {channel_id}")
            self.close_stream(channel_id)
        stream_id = channel_id
        port = self._zlm_open(stream_id)
        try:
            self._invite(device, channel_id, port)
        except Exception:
            self._zlm_close(stream_id)
            raise
        stream = {
            "stream_id": stream_id,
            "stream_uri": f"rtsp://{self.args.zlm_rtsp_host}:{self.args.zlm_rtsp_port}/rtp/{stream_id}",
            "port": port,
            "channel_id": channel_id,
        }
        with self.lock:
            self.streams[channel_id] = stream
        log(f"媒体流就绪 {stream_id} rtp_port={port}")
        return stream

    def close_stream(self, stream_id: str) -> None:
        with self.lock:
            stream = self.streams.pop(stream_id, None)
        if stream is None:
            return
        device = self.devices.get(stream["channel_id"])
        if device is not None and device.addr is not None:
            try:
                self._bye(device, stream_id)
            except Exception as exc:  # 设备已离线时忽略
                log(f"BYE 失败：{exc}")
        self._zlm_close(stream_id)
        log(f"媒体流释放 {stream_id}")

    def _zlm_api(self, path: str, params: dict) -> dict:
        query = urllib.parse.urlencode({"secret": self.args.zlm_secret, **params})
        url = f"{self.args.zlm_api}/index/api/{path}?{query}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode())

    def _zlm_open(self, stream_id: str) -> int:
        # 开发态统一走 TCP 被动收流：绕开 Docker Desktop 的 UDP 端口映射，链路更稳定。
        params = {"port": 0, "stream_id": stream_id, "enable_tcp": 1, "tcp_mode": 1}
        try:
            result = self._zlm_api("openRtpServer", params)
        except (urllib.error.URLError, OSError) as exc:
            raise Upstream(f"ZLMediaKit 不可达：{exc}") from exc
        if result.get("code") != 0:
            # 端口或流已存在时先关闭再重试一次。
            self._zlm_close(stream_id)
            result = self._zlm_api("openRtpServer", params)
        if result.get("code") != 0:
            raise Upstream(f"ZLMediaKit openRtpServer 失败：{result.get('msg')}")
        return int(result.get("port", 0))

    def _zlm_stream_alive(self, stream_id: str) -> bool:
        try:
            result = self._zlm_api("listRtpServer", {})
        except (urllib.error.URLError, OSError, ValueError):
            return False
        return any(item.get("stream_id") == stream_id for item in result.get("data") or [])

    def _zlm_close(self, stream_id: str) -> None:
        try:
            self._zlm_api("closeRtpServer", {"stream_id": stream_id})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"关闭 RTP 服务失败 {stream_id}：{exc}")

    def _invite(self, device: Device, channel_id: str, port: int) -> None:
        call_id = uuid.uuid4().hex
        platform_aor = f"sip:{self.args.platform_id}@{self.args.realm}"
        device_aor = f"sip:{device.device_id}@{self.args.realm}"
        sdp = (
            "v=0\r\n"
            f"o={self.args.platform_id} 0 0 IN IP4 {self.args.host_ip}\r\n"
            f"s=Play {channel_id}\r\n"
            f"c=IN IP4 {self.args.media_ip}\r\n"
            "t=0 0\r\n"
            f"m=video {port} {'TCP/RTP/AVP' if self.args.media_transport == 'tcp' else 'RTP/AVP'} 96\r\n"
            "a=recvonly\r\n"
            "a=rtpmap:96 PS/90000\r\n"
            "a=setup:passive\r\n"
            "a=connection:new\r\n"
        )
        headers = [
            f"Via: SIP/2.0/UDP {self.args.host_ip}:{self.args.sip_port};branch=z9hG4bK{uuid.uuid4().hex};rport",
            "Max-Forwards: 70",
            f"From: <{platform_aor}>;tag={uuid.uuid4().hex[:8]}",
            f"To: <{device_aor}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 INVITE",
            f"Contact: <sip:{self.args.platform_id}@{self.args.host_ip}:{self.args.sip_port}>",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp.encode())}",
        ]
        packet = "\r\n".join([f"INVITE {device_aor} SIP/2.0", *headers, "", sdp]).encode()
        waiter = queue.Queue()
        self.pending[call_id] = waiter
        try:
            self.socket.sendto(packet, device.addr)
            start, headers = waiter.get(timeout=self.args.invite_timeout)
            while start.startswith("SIP/2.0 1"):
                start, headers = waiter.get(timeout=self.args.invite_timeout)
            if not start.startswith("SIP/2.0 200"):
                raise Upstream(f"设备拒绝 INVITE：{start}")
            to = headers.get("to", "")
            ack_headers = [
                f"Via: SIP/2.0/UDP {self.args.host_ip}:{self.args.sip_port};branch=z9hG4bK{uuid.uuid4().hex};rport",
                "Max-Forwards: 70",
                f"From: <{platform_aor}>;tag={uuid.uuid4().hex[:8]}",
                f"To: {to}",
                f"Call-ID: {call_id}",
                "CSeq: 1 ACK",
                "Content-Length: 0",
            ]
            ack = "\r\n".join([f"ACK {device_aor} SIP/2.0", *ack_headers, "", ""]).encode()
            self.socket.sendto(ack, device.addr)
        except queue.Empty as exc:
            raise Upstream("设备未响应 INVITE") from exc
        finally:
            self.pending.pop(call_id, None)

    def _bye(self, device: Device, stream_id: str) -> None:
        call_id = uuid.uuid4().hex
        platform_aor = f"sip:{self.args.platform_id}@{self.args.realm}"
        device_aor = f"sip:{device.device_id}@{self.args.realm}"
        headers = [
            f"Via: SIP/2.0/UDP {self.args.host_ip}:{self.args.sip_port};branch=z9hG4bK{uuid.uuid4().hex};rport",
            "Max-Forwards: 70",
            f"From: <{platform_aor}>;tag={uuid.uuid4().hex[:8]}",
            f"To: <{device_aor}>",
            f"Call-ID: {call_id}",
            "CSeq: 2 BYE",
            f"Contact: <{platform_aor}>",
            "Content-Length: 0",
        ]
        self.socket.sendto("\r\n".join([f"BYE {device_aor} SIP/2.0", *headers, "", ""]).encode(), device.addr)
        log(f"BYE 已发送 {stream_id}")


def _xml_value(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return match.group(1).strip() if match else ""


class NotFound(Exception):
    pass


class Upstream(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):  # 静默默认访问日志
        return

    @property
    def platform(self) -> Platform:
        return self.server.platform

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if self.path == "/api/v1/devices":
            with self.platform.lock:
                devices = [device.to_json() for device in self.platform.devices.values()]
            self._json(200, {"devices": devices})
            return
        self._json(404, {"message": "not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/api/v1/ptz":
                self._ptz()
                return
            if self.path == "/api/v1/streams":
                self._open_stream()
                return
        except NotFound as exc:
            self._json(404, {"message": str(exc)})
            return
        except (Upstream, KeyError, ValueError, RuntimeError) as exc:
            self._json(502, {"message": str(exc)})
            return
        self._json(404, {"message": "not found"})

    def do_DELETE(self) -> None:
        prefix = "/api/v1/streams/"
        if self.path.startswith(prefix):
            self.platform.close_stream(self.path[len(prefix):])
            self._json(200, {"status": "released"})
            return
        self._json(404, {"message": "not found"})

    def _ptz(self) -> None:
        body = self._body()
        device_id = str(body.get("device_id", ""))
        channel_id = str(body.get("channel_id", ""))
        command = str(body.get("command", "stop"))
        if command not in PTZ_CODES:
            raise ValueError(f"不支持的云台命令 {command}")
        with self.platform.lock:
            device = self.platform.devices.get(device_id)
        if device is None or not device.online():
            raise NotFound(f"设备 {device_id} 未在线")
        speed = int(body.get("speed") or 0)
        zoom_speed = int(body.get("zoom_speed") or 0)
        if command.startswith("zoom_"):
            self.platform._send_ptz(device, channel_id, PTZ_CODES[command], 0, 0, zoom_speed)
        else:
            self.platform._send_ptz(device, channel_id, PTZ_CODES[command], speed, speed, 0)
        self._json(200, {"status": "sent"})

    def _open_stream(self) -> None:
        body = self._body()
        stream = self.platform.open_stream(str(body.get("device_id", "")), str(body.get("channel_id", "")))
        self._json(200, stream)


def parse_args():
    parser = argparse.ArgumentParser(description="GB28181 SIP platform simulator")
    parser.add_argument("--listen-host", default="0.0.0.0", help="SIP 监听地址")
    parser.add_argument("--host-ip", default="127.0.0.1", help="SIP 消息中通告的本机 IP")
    parser.add_argument("--sip-port", type=int, default=5060, help="平台 SIP UDP 端口")
    parser.add_argument("--http-port", type=int, default=8025, help="HTTP API 端口")
    parser.add_argument("--platform-id", default="34020000002000000001", help="平台国标编码")
    parser.add_argument("--realm", default="3402000000", help="SIP realm / 国标域")
    parser.add_argument("--password", default="12345678", help="设备注册密码")
    parser.add_argument("--expires", type=int, default=3600, help="注册有效期秒数")
    parser.add_argument("--zlm-api", default="http://127.0.0.1:8090", help="ZLMediaKit HTTP API 地址")
    parser.add_argument("--zlm-secret", default="vs-dev-zlm-secret", help="ZLMediaKit API secret")
    parser.add_argument("--zlm-rtsp-host", default="127.0.0.1", help="对外返回的 RTSP 主机")
    parser.add_argument("--zlm-rtsp-port", type=int, default=8554, help="对外返回的 RTSP 端口")
    parser.add_argument("--media-ip", default="127.0.0.1", help="SDP 中设备应发送 RTP 的目的 IP")
    parser.add_argument("--media-transport", default="tcp", choices=["tcp", "udp"], help="RTP 收流传输方式")
    parser.add_argument("--invite-timeout", type=float, default=6.0, help="等待设备 INVITE 响应秒数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    platform = Platform(args)
    server = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)
    server.platform = platform

    def shutdown(_signum, _frame):
        STOP.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"SIP 监听 0.0.0.0:{args.sip_port}，HTTP 监听 0.0.0.0:{args.http_port}，ZLM {args.zlm_api}")
    try:
        platform.run()
    finally:
        server.shutdown()
        platform.socket.close()
        log("平台模拟器已停止")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
