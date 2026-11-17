#!/usr/bin/env python3
"""GB28181 camera simulator: SIP registration/control and H.264 PS over RTP."""
import argparse
import base64
from functools import lru_cache
import hashlib
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFont

STOP = threading.Event()
RTP_PAYLOAD_SIZE = 1300
# 编码器强制插入 AUD，按其起始码切分访问单元，才能为每帧写入独立 PTS。
AUD_START_CODE = b"\x00\x00\x01\x09"
MAX_AU_BUFFER = 512 * 1024
PS_MUX_RATE = 6106


def _pack_header(scr: int) -> bytes:
    scr &= (1 << 33) - 1
    return (
        b"\x00\x00\x01\xba"
        + bytes([0x44 | ((scr >> 30) & 0x07)])
        + bytes([(scr >> 22) & 0xFF])
        + bytes([(((scr >> 15) & 0x7F) << 1) | 1])
        + bytes([(scr >> 7) & 0xFF])
        + bytes([((scr & 0x7F) << 1) | 1])
        + b"\x01"
        + bytes([0xC0 | ((PS_MUX_RATE >> 15) & 0x3F)])
        + bytes([(PS_MUX_RATE >> 7) & 0xFF])
        + bytes([((PS_MUX_RATE & 0x7F) << 1) | 1])
        + b"\xf8"
    )


def _system_header() -> bytes:
    return (
        b"\x00\x00\x01\xbb"
        + (6).to_bytes(2, "big")
        + b"\x80\x00\x00"
        + bytes([0xE0 | ((PS_MUX_RATE >> 15) & 0x3F), (PS_MUX_RATE >> 7) & 0xFF, ((PS_MUX_RATE & 0x7F) << 1) | 1])
    )


def _psm() -> bytes:
    # 节目流映射：声明 0xE0 为 H.264。SRS 等平台依赖 PSM 判断编码类型，缺失会报 codec=0。
    body = (
        b"\xe0\xff"  # current_next_indicator / version / marker
        + (0).to_bytes(2, "big")  # program_stream_info_length
        + (4).to_bytes(2, "big")  # elementary_stream_map_length
        + b"\x1b\xe0"  # stream_type=H.264, elementary_stream_id=0xE0
        + (0).to_bytes(2, "big")  # elementary_stream_info_length
        + b"\x00\x00\x00\x00"  # CRC32（平台不校验）
    )
    return b"\x00\x00\x01\xbc" + len(body).to_bytes(2, "big") + body


def _split_nals(access_unit: bytes) -> list[bytes]:
    # 按 3/4 字节起始码切分 NAL，返回含 NAL 头的原始片段。
    starts = []
    i = 0
    while True:
        i = access_unit.find(b"\x00\x00\x01", i)
        if i < 0:
            break
        starts.append(i + 3)
        i += 3
    nals = []
    for idx, begin in enumerate(starts):
        end = starts[idx + 1] - 3 if idx + 1 < len(starts) else len(access_unit)
        if end > begin:
            nals.append(access_unit[begin:end])
    return nals


def _pes(access_unit: bytes, pts: int) -> bytes:
    value = pts & ((1 << 33) - 1)
    pts_field = bytes([
        0x21 | ((value >> 29) & 0x0E),
        (value >> 22) & 0xFF,
        ((value >> 14) & 0xFE) | 0x01,
        (value >> 7) & 0xFF,
        ((value << 1) & 0xFE) | 0x01,
    ])
    payload = b"\x80\x80\x05" + pts_field + access_unit
    length = len(payload) if len(payload) <= 0xFFFF else 0
    return b"\x00\x00\x01\xe0" + length.to_bytes(2, "big") + payload


def _handle_stop(_signum, _frame):
    STOP.set()


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


def sip_response(request: bytes, status: int, reason: str, body: str = "", extra: list[str] | None = None) -> bytes:
    _, headers, _ = parse_sip(request)
    vias = [item.strip() for item in headers.get("via", "").split(",") if item.strip()]
    values = [f"SIP/2.0 {status} {reason}"]
    values.extend(f"Via: {via}" for via in vias)
    for name in ("from", "to", "call-id", "cseq"):
        value = headers.get(name, "")
        if name == "to" and value and ";tag=" not in value:
            value += f";tag={uuid.uuid4().hex[:8]}"
        if value:
            values.append(f"{name.title()}: {value}")
    values.extend(extra or [])
    if body:
        values.append(f"Content-Type: application/sdp")
    values += [f"Content-Length: {len(body.encode())}", "", body]
    return "\r\n".join(values).encode()


def _digest_fields(header: str) -> dict[str, str]:
    return {
        key.lower(): quoted if quoted else unquoted
        for key, quoted, unquoted in re.findall(r'([\w-]+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', header)
    }


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def digest_response(
    username: str,
    password: str,
    realm: str,
    nonce: str,
    method: str,
    uri: str,
    qop: str = "",
    nonce_count: str = "",
    client_nonce: str = "",
) -> str:
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        return _md5(f"{ha1}:{nonce}:{nonce_count}:{client_nonce}:{qop}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def encode_ptz_cmd(command: int, pan_speed: int = 100, tilt_speed: int = 100, zoom_speed: int = 12) -> bytes:
    """Encode the standard A5 0F 01 cmdCode pan tilt zoom checksum command."""
    values = [0xA5, 0x0F, 0x01, command, pan_speed, tilt_speed, zoom_speed << 4]
    values.append(sum(values) & 0xFF)
    return bytes(values)


def ptz_xml(channel_id: str, command: int, serial: int = 1) -> str:
    encoded = base64.b64encode(encode_ptz_cmd(command)).decode()
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<Control><CmdType>DeviceControl</CmdType><SN>{serial}</SN>"
        f"<DeviceID>{channel_id}</DeviceID><PTZCmd>{encoded}</PTZCmd></Control>"
    )


class PTZState:
    def __init__(self):
        self.lock = threading.Lock()
        self.x = self.y = 0.0
        self.zoom = 1.0
        self.pan = self.tilt = self.zoom_rate = 0.0
        self.updated = time.monotonic()

    def _advance(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.x = max(-0.9, min(0.9, self.x + self.pan * elapsed))
        self.y = max(-0.9, min(0.9, self.y + self.tilt * elapsed))
        self.zoom = max(0.55, min(2.4, self.zoom * (1.0 + self.zoom_rate * elapsed)))
        self.updated = now

    def apply_command(self, command: bytes) -> None:
        if len(command) != 8 or command[:3] != b"\xa5\x0f\x01":
            raise ValueError("PTZCmd must contain the GB28181 A5 0F 01 header and 8 bytes")
        if sum(command[:7]) & 0xFF != command[7]:
            raise ValueError("PTZCmd checksum mismatch")
        code = command[3]
        pan_speed, tilt_speed = command[4] / 255.0, command[5] / 255.0
        zoom_code, zoom_speed = code & 0x30, (command[6] >> 4) / 15.0
        pan = (1 if code & 0x01 else 0) - (1 if code & 0x02 else 0)
        tilt = (1 if code & 0x04 else 0) - (1 if code & 0x08 else 0)
        with self.lock:
            now = time.monotonic()
            self._advance(now)
            self.pan = pan * pan_speed * 0.8
            self.tilt = tilt * tilt_speed * 0.65
            self.zoom_rate = (1 if zoom_code == 0x10 else -1 if zoom_code == 0x20 else 0) * zoom_speed * 0.75

    def snapshot(self) -> tuple[float, float, float]:
        with self.lock:
            self._advance(time.monotonic())
            return self.x, self.y, self.zoom


@lru_cache(maxsize=32)
def load_font(size: int):
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_frame(width: int, height: int, state: PTZState) -> bytes:
    image = Image.new("RGB", (width, height), (0, 144, 0))
    draw = ImageDraw.Draw(image)
    x_offset, y_offset, zoom = state.snapshot()
    title_font = load_font(max(12, int(min(width, height) * 0.15 * zoom)))
    time_font = load_font(max(10, int(min(width, height) * 0.055 * zoom)))
    title = "Vizbox"
    timestamp = str(int(time.time() * 1000))
    title_box = draw.textbbox((0, 0), title, font=title_font)
    time_box = draw.textbbox((0, 0), timestamp, font=time_font)
    title_height = title_box[3] - title_box[1]
    time_height = time_box[3] - time_box[1]
    gap = max(8, int(height * 0.025 * zoom))
    group_height = title_height + gap + time_height
    center_x = width * (0.5 + x_offset)
    center_y = height * (0.5 + y_offset)
    title_x = int(center_x - (title_box[2] - title_box[0]) / 2 - title_box[0])
    title_y = int(center_y - group_height / 2 - title_box[1])
    time_x = int(center_x - (time_box[2] - time_box[0]) / 2 - time_box[0])
    time_y = title_y + title_height + gap
    draw.text((title_x, title_y), title, fill=(255, 255, 255), font=title_font)
    draw.text((time_x, time_y), timestamp, fill=(255, 255, 255), font=time_font)
    return image.tobytes()


def find_ffmpeg(explicit: str) -> str:
    candidate = explicit or os.environ.get("VZ_FFMPEG_PATH", "") or shutil.which("ffmpeg", path=os.environ.get("PATH"))
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate
    try:
        from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise

        return get_or_fetch_platform_executables_else_raise()[0]
    except Exception as exc:
        raise SystemExit("未找到 ffmpeg；请安装 ffmpeg，或执行 pip install static-ffmpeg") from exc


class MediaSession:
    def __init__(
        self,
        ffmpeg: str,
        width: int,
        height: int,
        fps: float,
        state: PTZState,
        remote: tuple[str, int],
        local: tuple[str, int],
        transport: str = "udp",
        ssrc: int | None = None,
    ):
        self.ffmpeg = ffmpeg
        self.width = width
        self.height = height
        self.fps = fps
        self.state = state
        self.remote = remote
        self.local = local
        self.transport = transport
        # 真实设备使用 INVITE SDP 中 y= 指定的 SSRC；平台按 SSRC 识别收流会话。
        self.ssrc = ssrc if ssrc is not None else random.randrange(0x100000000)
        self.stop_event = threading.Event()
        self.process: subprocess.Popen | None = None
        self.thread = threading.Thread(target=self._stream, daemon=True)
        self.packet_count = 0
        self.stderr_log = ""

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.thread.join(timeout=5)
        if self.process and self.process.poll() is None:
            self.process.kill()

    def _stream(self) -> None:
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps), "-i", "pipe:0", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", str(max(2, int(self.fps * 2))),
            # sliced-threads 会把一帧拆成多个 slice，SRS 的 GB28181 转封装按 NAL 逐条写成 RTMP 帧，
            # 多 slice 会被下游当成多帧解码（大面积花屏），因此固定单 slice。
            "-x264-params", "aud=1:sliced-threads=0", "-f", "h264", "pipe:1",
        ]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            print(f"启动 ffmpeg 失败：{exc}", file=sys.stderr, flush=True)
            self.stop_event.set()
            return
        writer = threading.Thread(target=self._write_frames, daemon=True)
        writer.start()
        # ffmpeg 的告警会写入 stderr；不持续读取会写满管道并阻塞编码，必须实时排空。
        stderr_drain = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_drain.start()
        rtp_socket: socket.socket | None = None
        if self.transport == "tcp":
            try:
                rtp_socket = socket.create_connection(self.remote, timeout=5)
            except OSError as exc:
                print(f"连接 RTP TCP 端口 {self.remote[0]}:{self.remote[1]} 失败：{exc}", file=sys.stderr, flush=True)
                self.stop_event.set()
                self.process.terminate()
                self.process.wait()
                writer.join(timeout=2)
                return
        else:
            rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                rtp_socket.bind(self.local)
            except OSError as exc:
                print(f"绑定设备 RTP 端口 {self.local[0]}:{self.local[1]} 失败：{exc}", file=sys.stderr, flush=True)
                self.stop_event.set()
                self.process.terminate()
                self.process.wait()
                writer.join(timeout=2)
                rtp_socket.close()
                return
        sequence = random.randrange(0x10000)
        timestamp_base = random.randrange(0x100000000)
        ssrc = self.ssrc
        stream_started = time.monotonic()
        last_report = stream_started
        buffer = b""
        pending = b""
        frame_interval = max(1, round(90000 / self.fps))
        frame_index = 0
        header_sent = False

        def send_frame(access_unit: bytes, pts: int) -> bool:
            nonlocal sequence, header_sent
            nals = _split_nals(access_unit)
            # 首包可能因为平台还没完成会话绑定而被丢弃，因此在每个关键帧重复系统头与 PSM。
            keyframe = any(n and (n[0] & 0x1F) == 5 for n in nals)
            pack = _pack_header(pts)
            if not header_sent or keyframe:
                pack += _system_header() + _psm()
            if not header_sent:
                # SPS/PPS 单独成一个小 PS 包，避免平台从大 IDR 帧里解析参数集失败。
                # 每个 NAL 必须带起始码，否则平台无法切分，会把参数集当成 AUD 的载荷丢弃。
                params = b"".join(b"\x00\x00\x00\x01" + n for n in nals if n and (n[0] & 0x1F) in (7, 8))
                if params:
                    pack += _pes(b"\x00\x00\x00\x01\x09\x10" + params, pts)
                    pack += _pack_header(pts)
                header_sent = True
            pack += _pes(access_unit, pts)
            offset = 0
            while offset < len(pack):
                piece = pack[offset:offset + RTP_PAYLOAD_SIZE]
                offset += len(piece)
                marker = 0x80 if offset >= len(pack) else 0x00
                header = (
                    bytes((0x80, 96 | marker))
                    + sequence.to_bytes(2, "big")
                    + ((timestamp_base + pts) & 0xFFFFFFFF).to_bytes(4, "big")
                    + ssrc.to_bytes(4, "big")
                )
                packet = header + piece
                try:
                    if self.transport == "tcp":
                        rtp_socket.sendall(len(packet).to_bytes(2, "big") + packet)
                    else:
                        rtp_socket.sendto(packet, self.remote)
                except OSError as exc:
                    if not self.stop_event.is_set():
                        print(f"RTP 发送失败：{exc}", file=sys.stderr, flush=True)
                    return False
                self.packet_count += 1
                sequence = (sequence + 1) & 0xFFFF
            return True

        try:
            while not self.stop_event.is_set() and self.process.poll() is None:
                chunk = self.process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                sending = True
                while True:
                    start = buffer.find(AUD_START_CODE)
                    if start < 0:
                        if len(buffer) > MAX_AU_BUFFER:
                            buffer = b""
                        break
                    # AUD 之前的 SPS/PPS 属于该访问单元，需缓存下来，否则平台拿不到参数集。
                    # 保留 4 字节起始码：部分平台（如 SRS）按 4 字节码回溯解析，3 字节码易被判错。
                    if start > 0 and buffer[start - 1] == 0:
                        start -= 1
                    pending += buffer[:start]
                    buffer = buffer[start:]
                    index = buffer.find(AUD_START_CODE, len(AUD_START_CODE))
                    if index < 0:
                        break
                    if index > 0 and buffer[index - 1] == 0:
                        index -= 1
                    if not send_frame(pending + buffer[:index], frame_index * frame_interval):
                        sending = False
                        break
                    pending = b""
                    buffer = buffer[index:]
                    frame_index += 1
                if not sending:
                    break
                now = time.monotonic()
                if now - last_report >= 5:
                    last_report = now
                    print(f"媒体发送中 {self.remote[0]}:{self.remote[1]} packets={self.packet_count}", flush=True)
        finally:
            print(f"媒体发送结束 共 {self.packet_count} 个 RTP 包", flush=True)
            self.stop_event.set()
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            if self.process.stderr:
                detail = self.stderr_log.strip()
                if detail:
                    print(f"ffmpeg: {detail}", file=sys.stderr, flush=True)
            rtp_socket.close()
            writer.join(timeout=2)

    def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            for line in self.process.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.stderr_log = (self.stderr_log + "\n" + text)[-4000:]
        except (OSError, ValueError):
            pass

    def _write_frames(self) -> None:
        assert self.process is not None and self.process.stdin is not None
        deadline = time.monotonic()
        interval = 1.0 / self.fps
        try:
            while not self.stop_event.is_set() and self.process.poll() is None:
                now = time.monotonic()
                if now < deadline:
                    self.stop_event.wait(min(deadline - now, 0.05))
                    continue
                self.process.stdin.write(render_frame(self.width, self.height, self.state))
                deadline += interval
                if now - deadline > interval:
                    deadline = now
        except (BrokenPipeError, OSError, ValueError):
            self.stop_event.set()
        finally:
            try:
                self.process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass


def _message_body(body: bytes) -> str:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return ""
    return root.findtext("CmdType", default="")


def _sdp_value(sdp: str, prefix: str) -> str:
    for line in sdp.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


class GB28181Device:
    def __init__(self, args):
        self.args = args
        self.platform = (socket.gethostbyname(args.platform_host), args.platform_port)
        self.local_ip = args.local_ip or self._local_ip()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((args.listen_host, args.sip_port))
        self.state = PTZState()
        # 按 Call-ID 维护多路媒体会话，支持 AI 通道与预览通道同时收流。
        self.media: dict[str, MediaSession] = {}
        self.register_call_id = uuid.uuid4().hex
        self.register_cseq = 0
        self.auth_header = ""
        self.register_at = 0.0

    def _local_ip(self) -> str:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(self.platform)
            return probe.getsockname()[0]
        finally:
            probe.close()

    def _send_register(self) -> None:
        self.register_cseq += 1
        branch = uuid.uuid4().hex
        aor = f"sip:{self.args.device_id}@{self.args.realm}"
        headers = [
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.args.sip_port};branch=z9hG4bK{branch};rport",
            "Max-Forwards: 70",
            f'From: <{aor}>;tag={uuid.uuid4().hex[:8]}',
            f"To: <{aor}>",
            f"Call-ID: {self.register_call_id}",
            f"CSeq: {self.register_cseq} REGISTER",
            f"Contact: <sip:{self.args.device_id}@{self.local_ip}:{self.args.sip_port}>",
            f"Expires: {self.args.expires}",
            "User-Agent: Vizbox-GB28181-Simulator/1.0",
        ]
        if self.auth_header:
            headers.append(f"Authorization: {self.auth_header}")
        payload = "\r\n".join([f"REGISTER {aor} SIP/2.0", *headers, "Content-Length: 0", "", ""]).encode()
        self.socket.sendto(payload, self.platform)

    def _register(self, initial: bool = False) -> None:
        aor = f"sip:{self.args.device_id}@{self.args.realm}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self._send_register()
            while time.monotonic() < deadline:
                self.socket.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    response, _ = self.socket.recvfrom(65535)
                except socket.timeout:
                    break
                start, response_headers, _ = parse_sip(response)
                if not start.startswith("SIP/2.0") or response_headers.get("call-id") != self.register_call_id:
                    continue
                if start.startswith("SIP/2.0 401"):
                    challenge = _digest_fields(response_headers.get("www-authenticate", ""))
                    realm = challenge.get("realm", self.args.realm)
                    nonce = challenge.get("nonce", "")
                    uri = aor
                    algorithm = challenge.get("algorithm", "MD5").upper()
                    if algorithm != "MD5":
                        raise RuntimeError(f"平台使用不支持的 Digest 算法：{algorithm}")
                    qop_values = challenge.get("qop", "").split(",")
                    qop = "auth" if "auth" in qop_values else ""
                    if challenge.get("qop") and not qop:
                        raise RuntimeError(f"平台未提供可用的 Digest qop：{challenge['qop']}")
                    nonce_count = "00000001"
                    client_nonce = uuid.uuid4().hex[:16] if qop else ""
                    digest = digest_response(
                        self.args.device_id, self.args.password, realm, nonce, "REGISTER", uri,
                        qop, nonce_count, client_nonce,
                    )
                    authorization = (
                        f'Digest username="{self.args.device_id}", realm="{realm}", nonce="{nonce}", '
                        f'uri="{uri}", response="{digest}", algorithm=MD5'
                    )
                    if qop:
                        authorization += f', qop={qop}, nc={nonce_count}, cnonce="{client_nonce}"'
                    opaque = challenge.get("opaque", "")
                    if opaque:
                        authorization += f', opaque="{opaque}"'
                    self.auth_header = authorization
                    break
                if start.startswith("SIP/2.0 200"):
                    self.register_at = time.monotonic()
                    self.socket.settimeout(0.5)
                    if initial:
                        print(
                            f"已注册 GB28181 设备 {self.args.device_id}，通道 {self.args.channel_id} "
                            f"-> {self.platform[0]}:{self.platform[1]}",
                            flush=True,
                        )
                    return
                raise RuntimeError(f"平台注册失败：{start}")
            else:
                break
        raise TimeoutError("等待平台 REGISTER 响应超时")

    def _send_keepalive(self) -> str:
        xml = (
            f'<?xml version="1.0" encoding="UTF-8"?><Notify><CmdType>Keepalive</CmdType>'
            f"<SN>{int(time.time())}</SN><DeviceID>{self.args.device_id}</DeviceID><Status>OK</Status></Notify>"
        )
        return self._send_message(xml)

    def _send_message(self, body: str) -> str:
        call_id = uuid.uuid4().hex
        aor = f"sip:{self.args.platform_id}@{self.args.realm}"
        device_aor = f"sip:{self.args.device_id}@{self.args.realm}"
        headers = [
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.args.sip_port};branch=z9hG4bK{uuid.uuid4().hex};rport",
            "Max-Forwards: 70",
            f"From: <{device_aor}>;tag={uuid.uuid4().hex[:8]}",
            f"To: <{aor}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 MESSAGE",
            f"Contact: <{device_aor}>",
            "Content-Type: Application/MANSCDP+xml",
            f"Content-Length: {len(body.encode())}",
        ]
        self.socket.sendto("\r\n".join([f"MESSAGE {aor} SIP/2.0", *headers, "", body]).encode(), self.platform)
        return call_id

    def _catalog_response(self, request: bytes) -> None:
        _, _, body = parse_sip(request)
        try:
            serial = ElementTree.fromstring(body).findtext("SN", "1")
        except ElementTree.ParseError:
            serial = "1"
        self._send_message(
            f'<?xml version="1.0" encoding="UTF-8"?><Response><CmdType>Catalog</CmdType><SN>{serial}</SN>'
            f"<DeviceID>{self.args.device_id}</DeviceID><SumNum>1</SumNum><DeviceList Num=\"1\">"
            f"<Item><DeviceID>{self.args.channel_id}</DeviceID><Name>{self.args.name}</Name>"
            f"<Manufacturer>Vizbox</Manufacturer><Model>GB28181 Simulator</Model>"
            f"<Owner>Vizbox</Owner><CivilCode>{self.args.realm}</CivilCode><Address>Simulator</Address>"
            f"<Parental>0</Parental><ParentID>{self.args.device_id}</ParentID><Secrecy>0</Secrecy>"
            f"<Status>ON</Status><Info><PTZType>1</PTZType></Info></Item></DeviceList></Response>"
        )

    def _handle_request(self, packet: bytes, peer: tuple[str, int]) -> None:
        start, headers, body = parse_sip(packet)
        method = start.split(" ", 1)[0].upper()
        if method == "INVITE":
            self.socket.sendto(sip_response(packet, 100, "Trying"), peer)
            sdp = body.decode("utf-8", "replace")
            connection = _sdp_value(sdp, "c=IN IP4 ") or peer[0]
            media = _sdp_value(sdp, "m=video ")
            if not media:
                self.socket.sendto(sip_response(packet, 488, "Not Acceptable Here"), peer)
                return
            target_port = int(media.split()[0])
            transport = "tcp" if "a=setup:passive" in sdp or media.split()[2].upper() == "TCP/RTP/AVP" else "udp"
            ssrc_text = _sdp_value(sdp, "y=")
            ssrc = int(ssrc_text) if ssrc_text.isdigit() else None
            answer = (
                "v=0\r\n"
                f"o={self.args.device_id} 0 0 IN IP4 {self.local_ip}\r\n"
                f"s={self.args.name}\r\n"
                f"c=IN IP4 {self.local_ip}\r\n"
                "t=0 0\r\n"
                f"m=video {self.args.rtp_port} RTP/AVP 96\r\n"
                "a=rtpmap:96 PS/90000\r\n"
                "a=sendonly\r\n"
            )
            if ssrc is not None:
                answer += f"y={ssrc}\r\n"
            self.socket.sendto(sip_response(packet, 200, "OK", answer), peer)
            call_id = headers.get("call-id", uuid.uuid4().hex)
            previous = self.media.pop(call_id, None)
            if previous:
                previous.stop()
            session = MediaSession(
                self.args.ffmpeg, self.args.width, self.args.height, self.args.fps, self.state,
                (connection, target_port), (self.local_ip, 0), transport, ssrc,
            )
            self.media[call_id] = session
            session.start()
            print(
                f"已响应 INVITE（{call_id[:8]}），开始向 {connection}:{target_port} "
                f"以 {transport.upper()} 发送 PS/RTP，当前会话 {len(self.media)} 路",
                flush=True,
            )
            return
        if method == "MESSAGE":
            kind = _message_body(body)
            self.socket.sendto(sip_response(packet, 200, "OK"), peer)
            if kind == "DeviceControl":
                try:
                    root = ElementTree.fromstring(body)
                    target = root.findtext("DeviceID", "")
                    if target and target not in (self.args.channel_id, self.args.device_id):
                        raise ValueError(f"PTZ 控制通道不匹配：{target}")
                    data = base64.b64decode(root.findtext("PTZCmd", ""), validate=True)
                    self.state.apply_command(data)
                    print(f"收到 PTZ 控制 {data.hex().upper()}（通道 {target or self.args.channel_id}）", flush=True)
                except (ElementTree.ParseError, ValueError) as exc:
                    print(f"忽略无效 DeviceControl：{exc}", file=sys.stderr, flush=True)
            elif kind == "Catalog":
                self._catalog_response(packet)
            return
        if method == "BYE":
            self.socket.sendto(sip_response(packet, 200, "OK"), peer)
            session = self.media.pop(headers.get("call-id", ""), None)
            if session:
                session.stop()
                print(f"已停止一路媒体会话，剩余 {len(self.media)} 路", flush=True)
            return
        if method == "ACK":
            return
        if method in ("OPTIONS", "INFO"):
            self.socket.sendto(sip_response(packet, 200, "OK"), peer)
            return
        self.socket.sendto(sip_response(packet, 405, "Method Not Allowed"), peer)

    def _try_register(self, reason: str) -> None:
        print(f"检测到注册需要刷新（{reason}），重新注册", flush=True)
        try:
            self._register()
        except (OSError, RuntimeError, TimeoutError) as exc:
            print(f"GB28181 重新注册失败：{exc}", file=sys.stderr, flush=True)

    def run(self) -> None:
        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)
        self._register(initial=True)
        refresh_interval = max(30.0, self.args.expires / 2)
        next_register = time.monotonic() + refresh_interval
        next_keepalive = time.monotonic() + self.args.keepalive
        pending_keepalive = ""
        keepalive_deadline = 0.0
        try:
            while not STOP.is_set():
                now = time.monotonic()
                if now >= next_register:
                    self._try_register("注册有效期过半")
                    next_register = time.monotonic() + refresh_interval
                if now >= next_keepalive:
                    pending_keepalive = self._send_keepalive()
                    keepalive_deadline = now + 15
                    next_keepalive = now + self.args.keepalive
                # 平台重启会丢失内存中的设备与通道，心跳会被拒绝或无人应答，此时必须重新注册。
                if pending_keepalive and now >= keepalive_deadline:
                    pending_keepalive = ""
                    self._try_register("心跳无响应")
                    next_register = time.monotonic() + refresh_interval
                self.socket.settimeout(0.5)
                try:
                    packet, peer = self.socket.recvfrom(65535)
                except socket.timeout:
                    continue
                start, headers, _ = parse_sip(packet)
                if start.startswith("SIP/2.0"):
                    if pending_keepalive and headers.get("call-id") == pending_keepalive:
                        pending_keepalive = ""
                        if not start.startswith("SIP/2.0 2"):
                            self._try_register(f"心跳被拒绝 {start.split(' ', 2)[1]}")
                            next_register = time.monotonic() + refresh_interval
                    continue
                self._handle_request(packet, peer)
        finally:
            for session in list(self.media.values()):
                session.stop()
            self.media.clear()
            self.socket.close()
            print("GB28181 设备模拟器已停止", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="GB28181 SIP/RTP camera simulator")
    parser.add_argument("--platform-host", default="127.0.0.1", help="平台 SIP 主机名/IP")
    parser.add_argument("--platform-port", type=int, default=5060, help="平台 SIP UDP 端口")
    parser.add_argument("--listen-host", default="0.0.0.0", help="本地 SIP 监听地址")
    parser.add_argument("--local-ip", default="", help="设备对外通告的本机 IP，默认自动探测")
    parser.add_argument("--sip-port", type=int, default=5062, help="设备 SIP UDP 端口")
    parser.add_argument("--rtp-port", type=int, default=30000, help="SDP 中通告的设备 RTP 端口")
    parser.add_argument("--device-id", default="34020000001320000001", help="20 位设备国标编码")
    parser.add_argument("--channel-id", default="34020000001320000001", help="20 位通道国标编码")
    parser.add_argument("--platform-id", default="34020000002000000001", help="平台国标编码")
    parser.add_argument("--realm", default="3402000000", help="SIP realm / 国标域")
    parser.add_argument("--password", default="12345678", help="SIP Digest 注册密码")
    parser.add_argument("--name", default="Vizbox", help="目录中的摄像头名称")
    parser.add_argument("--expires", type=int, default=3600, help="注册有效期秒数")
    parser.add_argument("--keepalive", type=float, default=60, help="心跳周期秒数")
    parser.add_argument("--fps", type=float, default=25.0, help="视频帧率")
    parser.add_argument("--width", type=int, default=1280, help="视频宽度")
    parser.add_argument("--height", type=int, default=720, help="视频高度")
    parser.add_argument("--ffmpeg", default="", help="ffmpeg 可执行文件路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fps <= 0 or args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
        raise SystemExit("fps 和宽高必须大于 0，且 H.264 宽高必须为偶数")
    args.ffmpeg = find_ffmpeg(args.ffmpeg)
    try:
        GB28181Device(args).run()
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"GB28181 模拟器失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
