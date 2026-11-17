#!/usr/bin/env python3
"""Run a local GB28181 platform, control PTZ, capture PS/RTP, and verify the MP4."""
import argparse
import base64
import hashlib
import os
import re
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def parse_sip(data: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        name = key.strip().lower()
        value = value.strip()
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return lines[0], headers, body


def sip_response(request: bytes, status: int, reason: str, extra=None) -> bytes:
    _, headers, _ = parse_sip(request)
    values = [f"SIP/2.0 {status} {reason}"]
    values.extend(f"Via: {via.strip()}" for via in headers.get("via", "").split(",") if via.strip())
    for name in ("from", "to", "call-id", "cseq"):
        value = headers.get(name, "")
        if name == "to" and value and ";tag=" not in value:
            value += f";tag={uuid.uuid4().hex[:8]}"
        if value:
            values.append(f"{name.title()}: {value}")
    values.extend(extra or [])
    values += ["Content-Length: 0", "", ""]
    return "\r\n".join(values).encode()


def digest_response(username: str, password: str, realm: str, nonce: str, method: str, uri: str, qop: str, nc: str, cnonce: str) -> str:
    md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    return md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")


def ptz_xml(channel_id: str, command: int, serial: int) -> str:
    values = [0xA5, 0x0F, 0x01, command, 100, 100, 12 << 4]
    values.append(sum(values) & 0xFF)
    encoded = base64.b64encode(bytes(values)).decode()
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Control><CmdType>DeviceControl</CmdType>'
        f"<SN>{serial}</SN><DeviceID>{channel_id}</DeviceID><PTZCmd>{encoded}</PTZCmd></Control>"
    )


def find_ffmpeg(explicit: str) -> str:
    candidate = explicit or os.environ.get("VZ_FFMPEG_PATH", "") or shutil.which("ffmpeg")
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate
    from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise

    return get_or_fetch_platform_executables_else_raise()[0]


class Capture:
    def __init__(self, host: str, port: int, output: Path):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((host, port))
        self.socket.settimeout(0.25)
        self.output = output.open("wb")
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.first_packet_at = 0.0
        self.packets = 0
        self.bytes = 0
        self.last_sequence: int | None = None
        self.sequence_gaps = 0
        self.sources: set[tuple[str, int]] = set()

    @property
    def address(self) -> tuple[str, int]:
        return self.socket.getsockname()

    def start(self) -> None:
        self.thread.start()

    def _receive(self) -> None:
        while not self.stop.is_set():
            try:
                packet, peer = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(packet) < 12 or packet[0] >> 6 != 2:
                continue
            csrc_count = packet[0] & 0x0F
            offset = 12 + csrc_count * 4
            if packet[0] & 0x10:
                if len(packet) < offset + 4:
                    continue
                offset += 4 + int.from_bytes(packet[offset + 2:offset + 4], "big") * 4
            if offset >= len(packet):
                continue
            sequence = int.from_bytes(packet[2:4], "big")
            self.sources.add(peer)
            if self.last_sequence is not None and sequence != (self.last_sequence + 1) & 0xFFFF:
                self.sequence_gaps += 1
            self.last_sequence = sequence
            if not self.first_packet_at:
                self.first_packet_at = time.monotonic()
            payload = packet[offset:]
            self.output.write(payload)
            self.packets += 1
            self.bytes += len(payload)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.output.close()
        self.socket.close()


def receive_sip(sock: socket.socket, timeout: float, predicate) -> tuple[bytes, tuple[str, int]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([sock], [], [], max(0.05, deadline - time.monotonic()))
        if not ready:
            break
        packet, peer = sock.recvfrom(65535)
        if predicate(packet):
            return packet, peer
    raise TimeoutError("等待设备 SIP 消息超时")


def send_register_response(sock: socket.socket, request: bytes, peer, status: int, reason: str, extra=None) -> None:
    sock.sendto(sip_response(request, status, reason, extra=extra or []), peer)


def wait_register(
    sock: socket.socket,
    device_id: str,
    password: str,
    realm: str,
    nonce: str,
    timeout: float,
):
    challenged = False
    deadline = time.monotonic() + timeout
    while True:
        request, peer = receive_sip(
            sock, max(0.1, deadline - time.monotonic()),
            lambda packet: parse_sip(packet)[0].startswith("REGISTER "),
        )
        _, headers, _ = parse_sip(request)
        if f"sip:{device_id}@" not in headers.get("to", ""):
            raise AssertionError(f"unexpected registered device: {headers.get('to')}")
        authorization = headers.get("authorization", "")
        if not challenged:
            challenged = True
            send_register_response(
                sock, request, peer, 401, "Unauthorized",
                [
                    f'WWW-Authenticate: Digest realm="{realm}", nonce="{nonce}", algorithm=MD5, '
                    'qop="auth,auth-int", opaque="platform-token"'
                ],
            )
            continue
        fields = {
            key.lower(): quoted if quoted else unquoted
            for key, quoted, unquoted in re.findall(r'([\w-]+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', authorization)
        }
        if fields.get("username") != device_id or fields.get("realm") != realm or fields.get("nonce") != nonce:
            raise AssertionError(f"missing/invalid Digest authorization: {authorization}")
        qop = fields.get("qop", "")
        if qop != "auth" or not fields.get("cnonce") or fields.get("nc") != "00000001":
            raise AssertionError(f"REGISTER did not use Digest qop=auth: {authorization}")
        if fields.get("opaque") != "platform-token":
            raise AssertionError(f"REGISTER did not echo Digest opaque: {authorization}")
        expected = digest_response(
            device_id, password, realm, nonce, "REGISTER", fields.get("uri", ""),
            qop, fields.get("nc", ""), fields.get("cnonce", ""),
        )
        if fields.get("response") != expected:
            raise AssertionError("incorrect REGISTER Digest response")
        send_register_response(sock, request, peer, 200, "OK")
        return peer


def sip_request(method: str, request_uri: str, platform_id: str, device_id: str, platform_host: str, platform_port: int, call_id: str, cseq: int, body: str = "", content_type: str = "") -> bytes:
    branch = uuid.uuid4().hex
    headers = [
        f"Via: SIP/2.0/UDP {platform_host}:{platform_port};branch=z9hG4bK{branch};rport",
        "Max-Forwards: 70",
        f"From: <sip:{platform_id}@3402000000>;tag={uuid.uuid4().hex[:8]}",
        f"To: <sip:{device_id}@3402000000>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} {method}",
        f"Contact: <sip:{platform_id}@{platform_host}:{platform_port}>",
    ]
    if content_type:
        headers.append(f"Content-Type: {content_type}")
    headers.append(f"Content-Length: {len(body.encode())}")
    return "\r\n".join([f"{method} {request_uri} SIP/2.0", *headers, "", body]).encode()


def wait_response(sock: socket.socket, call_id: str, timeout: float = 6.0) -> bytes:
    response, _ = receive_sip(
        sock, timeout,
        lambda packet: parse_sip(packet)[0].startswith("SIP/2.0")
        and parse_sip(packet)[1].get("call-id") == call_id,
    )
    return response


def send_control(sock: socket.socket, device_peer, platform_id: str, device_id: str, channel_id: str, platform_address, serial: int, command: int, call_id: str, cseq: int):
    body = ptz_xml(channel_id, command, serial)
    request = sip_request(
        "MESSAGE", f"sip:{device_id}@3402000000", platform_id, device_id,
        platform_address[0], platform_address[1], call_id, cseq, body, "Application/MANSCDP+xml",
    )
    sock.sendto(request, device_peer)
    response = wait_response(sock, call_id)
    start, _, _ = parse_sip(response)
    if not start.startswith("SIP/2.0 200"):
        raise AssertionError(f"DeviceControl returned {start}")


def image_measurement(path: Path) -> tuple[float, float, float]:
    image = Image.open(path).convert("RGB")
    width, height = image.size
    pixels = image.load()
    count = total_x = total_y = 0
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            red, green, blue = pixels[x, y]
            if red > 190 and green > 190 and blue > 190:
                count += 1
                total_x += x
                total_y += y
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
    if count < 100:
        raise AssertionError(f"captured frame has no rendered text: {path}")
    return total_x / count, total_y / count, float(max_x - min_x)


def main() -> int:
    parser = argparse.ArgumentParser(description="GB28181 platform test client; connect to a separately started simulator")
    parser.add_argument("--output", default=str(ROOT / "build/demo/gb28181-ptz.mp4"), help="captured video output path")
    parser.add_argument("--ffmpeg", default="", help="ffmpeg executable")
    parser.add_argument("--platform-bind-host", default="0.0.0.0", help="local SIP/RTP bind address")
    parser.add_argument("--platform-host", default="127.0.0.1", help="platform address advertised in SIP/SDP")
    parser.add_argument("--sip-port", type=int, default=5060, help="platform SIP UDP port")
    parser.add_argument("--media-port", type=int, default=30000, help="platform RTP capture UDP port")
    parser.add_argument("--device-host", default="127.0.0.1", help="simulator host address")
    parser.add_argument("--device-sip-port", type=int, default=5062, help="simulator SIP UDP source port")
    parser.add_argument("--device-rtp-port", type=int, default=30002, help="simulator RTP source port")
    parser.add_argument("--registration-timeout", type=float, default=180, help="seconds to wait for simulator registration")
    parser.add_argument("--password", default="12345678", help="simulator registration Digest password")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=float, default=15)
    args = parser.parse_args()
    if args.registration_timeout <= 0:
        raise SystemExit("--registration-timeout must be greater than zero")
    ffmpeg = find_ffmpeg(args.ffmpeg)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    platform = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    platform.bind((args.platform_bind_host, args.sip_port))
    platform.settimeout(0.5)
    platform_address = platform.getsockname()
    if args.platform_host in ("0.0.0.0", "::"):
        raise SystemExit("--platform-host must be reachable by the simulator, not a wildcard address")
    device_id = "34020000001320000001"
    channel_id = "34020000001320000002"
    platform_id = "34020000002000000001"
    realm = "3402000000"
    nonce = uuid.uuid4().hex
    capture_file: Path
    timeline: list[tuple[str, float, int]]
    capture: Capture | None = None

    with tempfile.TemporaryDirectory(prefix="gb28181-ptz-") as temp_dir:
        capture_file = Path(temp_dir) / "stream.ps"
        capture = Capture(args.platform_bind_host, args.media_port, capture_file)
        capture.start()
        try:
            print(
                f"等待独立模拟器注册到 {args.platform_host}:{platform_address[1]} "
                f"(SIP)，RTP 接收端口 {args.platform_host}:{args.media_port}；"
                f"请在另一个终端启动模拟器，等待 {args.registration_timeout:g}s",
                flush=True,
            )
            device_peer = wait_register(
                platform, device_id, args.password, realm, nonce, args.registration_timeout,
            )
            if device_peer[0] != args.device_host:
                raise AssertionError(f"simulator SIP source {device_peer[0]} does not match --device-host {args.device_host}")
            if device_peer[1] != args.device_sip_port:
                raise AssertionError(f"simulator SIP source port {device_peer[1]} does not match --device-sip-port {args.device_sip_port}")
            print(f"已通过 UDP 收到独立模拟器注册：{device_peer[0]}:{device_peer[1]}", flush=True)
            catalog_id = uuid.uuid4().hex
            query = (
                "<Query><CmdType>Catalog</CmdType><SN>1</SN>"
                f"<DeviceID>{device_id}</DeviceID></Query>"
            )
            platform.sendto(
                sip_request(
                    "MESSAGE", f"sip:{device_id}@{realm}", platform_id, device_id,
                    args.platform_host, args.sip_port, catalog_id, 1,
                    query, "Application/MANSCDP+xml",
                ),
                device_peer,
            )
            query_response = wait_response(platform, catalog_id)
            if not parse_sip(query_response)[0].startswith("SIP/2.0 200"):
                raise AssertionError("Catalog query was not acknowledged")
            catalog, catalog_peer = receive_sip(
                platform, 6,
                lambda packet: parse_sip(packet)[0].startswith("MESSAGE ")
                and "Catalog" in parse_sip(packet)[2].decode("utf-8", "replace"),
            )
            catalog_xml = parse_sip(catalog)[2].decode("utf-8", "replace")
            if channel_id not in catalog_xml or "PTZType" not in catalog_xml:
                raise AssertionError(f"catalog response does not advertise PTZ channel: {catalog_xml}")
            platform.sendto(sip_response(catalog, 200, "OK"), catalog_peer)
            invite_id = uuid.uuid4().hex
            sdp = (
                "v=0\r\n"
                f"o={platform_id} 1 1 IN IP4 {args.platform_host}\r\n"
                "s=Vizbox GB28181 PTZ test\r\n"
                f"c=IN IP4 {args.platform_host}\r\n"
                "t=0 0\r\n"
                f"m=video {args.media_port} RTP/AVP 96\r\n"
                "a=rtpmap:96 PS/90000\r\n"
                "a=recvonly\r\n"
            )
            invite = sip_request(
                "INVITE", f"sip:{channel_id}@{realm}", platform_id, device_id,
                args.platform_host, args.sip_port, invite_id, 1, sdp, "application/sdp",
            )
            platform.sendto(invite, device_peer)
            final = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                packet, _ = receive_sip(platform, 2, lambda item: parse_sip(item)[0].startswith("SIP/2.0") and parse_sip(item)[1].get("call-id") == invite_id)
                start, _, body = parse_sip(packet)
                if start.startswith("SIP/2.0 200"):
                    final = body.decode("utf-8", "replace")
                    break
                if not start.startswith("SIP/2.0 1"):
                    raise AssertionError(f"INVITE failed: {start}")
            if not final or "a=sendonly" not in final or "PS/90000" not in final:
                raise AssertionError(f"unexpected INVITE answer: {final}")
            platform.sendto(
                sip_request(
                    "ACK", f"sip:{channel_id}@{realm}", platform_id, device_id,
                    args.platform_host, args.sip_port, invite_id, 1,
                ),
                device_peer,
            )

            wait_deadline = time.monotonic() + 10
            while capture.packets < 5 and time.monotonic() < wait_deadline:
                time.sleep(0.1)
            if capture.packets < 5:
                raise AssertionError("device did not send RTP video after INVITE")

            timeline = []
            started_at = capture.first_packet_at

            def hold(label: str, command_code: int, seconds: float, serial: int):
                call_id = uuid.uuid4().hex
                send_control(
                    platform, device_peer, platform_id, device_id, channel_id,
                    (args.platform_host, args.sip_port), serial, command_code, call_id, serial,
                )
                offset = time.monotonic() - started_at
                timeline.append((label, offset, command_code))
                time.sleep(seconds)
                if command_code:
                    call_id = uuid.uuid4().hex
                    stop_serial = serial + 100
                    send_control(
                        platform, device_peer, platform_id, device_id, channel_id,
                        (args.platform_host, args.sip_port), stop_serial, 0, call_id, stop_serial,
                    )
                    time.sleep(0.25)

            hold("center", 0, 1.0, 1)
            hold("right", 8, 1.2, 2)
            hold("left", 4, 1.2, 3)
            hold("down", 2, 1.2, 4)
            hold("up", 1, 1.2, 5)
            hold("zoom_in", 16, 1.2, 6)
            hold("zoom_out", 32, 1.2, 7)
            time.sleep(0.5)
        finally:
            if capture:
                capture.close()
            platform.close()

        if not capture_file.is_file() or capture_file.stat().st_size < 5000 or capture.packets < 20:
            raise AssertionError(f"RTP capture is empty/incomplete ({capture.packets} packets)")
        if capture.sources != {(args.device_host, args.device_rtp_port)}:
            raise AssertionError(f"device RTP source does not match SDP: {capture.sources}")
        convert = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts",
            "-i", str(capture_file), "-an", "-c:v", "copy", "-movflags", "+faststart", str(output),
        ]
        converted = subprocess.run(convert, capture_output=True, text=True, timeout=60)
        if converted.returncode:
            raise RuntimeError(f"MP4 conversion failed: {converted.stderr.strip()}")
        frame_pattern = str(Path(temp_dir) / "frame-%03d.png")
        extracted = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(capture_file), "-vf", "fps=2", "-vsync", "0", frame_pattern],
            capture_output=True, text=True, timeout=60,
        )
        if extracted.returncode:
            raise RuntimeError(f"captured video frame extraction failed: {extracted.stderr.strip()}")
        frames = sorted(Path(temp_dir).glob("frame-*.png"))
        if len(frames) < 8:
            raise AssertionError(f"captured stream has too few decoded frames: {len(frames)}")

        frame_at = {label: frames[min(len(frames) - 1, max(0, round((offset + 0.7) * 2)))] for label, offset, _ in timeline}
        baseline = image_measurement(frame_at["center"])
        right = image_measurement(frame_at["right"])
        left = image_measurement(frame_at["left"])
        down = image_measurement(frame_at["down"])
        up = image_measurement(frame_at["up"])
        zoom_in = image_measurement(frame_at["zoom_in"])
        zoom_out = image_measurement(frame_at["zoom_out"])
        checks = [
            ("right", right[0] - baseline[0] > 25, right[0] - baseline[0]),
            ("left", left[0] < right[0] - 25, left[0] - right[0]),
            ("down", down[1] - left[1] > 15, down[1] - left[1]),
            ("up", up[1] < down[1] - 15, up[1] - down[1]),
            ("zoom_in", zoom_in[2] > up[2] * 1.15, zoom_in[2] / max(1, up[2])),
            ("zoom_out", zoom_out[2] < zoom_in[2] * 0.9, zoom_out[2] / max(1, zoom_in[2])),
        ]
        failed = [f"{name} ({metric:.2f})" for name, passed, metric in checks if not passed]
        if failed:
            raise AssertionError(f"PTZ rendered motion verification failed: {', '.join(failed)}; frames: {frames}")
        print(f"REGISTER/Digest、Catalog、INVITE/PS-RTP、6 组 PTZ 控制通过；RTP={capture.packets} 包/{capture.bytes} 字节")
        print(f"画面位移/缩放验证通过；视频：{output} ({output.stat().st_size} bytes)")
        print(f"关键帧测量：center={baseline[:2]} right={right[:2]} down={down[:2]} zoom={zoom_in[2]:.1f}px")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TimeoutError, AssertionError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))
