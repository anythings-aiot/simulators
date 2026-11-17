#!/usr/bin/env python3
"""ONVIF 摄像头模拟器：WS-Discovery、ONVIF SOAP 服务及可拉取的测试视频。

前置条件
========
* Python 3.10 或更高版本；脚本只使用 Python 标准库。
* 默认视频模式需要本机安装 FFmpeg，且 FFmpeg 编译支持 lavfi、libx264 和
  RTSP 输出。可用 ``ffmpeg -version`` 检查安装；用 ``--ffmpeg`` 指定路径，
  或通过 ``VZ_FFMPEG_PATH`` 环境变量指定可执行文件。
* 默认视频模式还需要一个可接收 RTSP 发布流的服务。项目默认使用
  ZLMediaKit，启动命令（在仓库根目录执行）：

      docker compose -f deploy/compose/compose.dev.yaml up -d zlm

  默认向 ``rtsp://127.0.0.1:8554/live/onvif/<UUID>`` 发布。如果 RTSP 服务
  不是本机的 ZLMediaKit，请使用 ``--rtsp-publish-url`` 设置发布地址。
* ONVIF 客户端与模拟器跨主机时，设置 ``--advertise-host`` 为运行模拟器主机
  可被客户端访问的 IPv4 地址。确保 UDP 3702 组播以及 ONVIF HTTP 端口可达；
  RTSP 发布端口也必须能从模拟器访问，RTSP 播放端口必须能从客户端访问。

快速开始
========
在一个终端启动模拟器（默认 1280x720、25 fps）：

    python3 simulators/onvif/onvif_sim.py --resolution 1920x1080 --fps 30

此命令会启动 WS-Discovery 和 HTTP ONVIF Device/Media/PTZ 服务，并用 FFmpeg
生成测试图案，推送到本机 ZLMediaKit。另一个终端扫描设备：

    python3 test/camera/onvif/scan_onvif.py --timeout 5

从发现结果取得 Device Service URL 后，拉取并保存 10 秒视频：

    python3 test/camera/onvif/pull_10s.py \\
      --url http://127.0.0.1:8080/onvif/device_service \\
      --output build/demo/camera-10s.mkv

常用配置与说明
==============
* ``--listen-host`` / ``--http-port`` 设置 ONVIF HTTP 监听地址和端口；默认
  ``0.0.0.0:8080``。
* ``--advertise-host`` 设置设备发现结果和 ONVIF 响应中公布的本机 IPv4。
  自动检测不适用于多网卡、容器或远程客户端场景时，应手动指定。
* ``--resolution WIDTHxHEIGHT`` 和 ``--fps`` 同时配置 ONVIF 媒体参数及生成的
  测试视频。例如 ``--resolution 640x360 --fps 15``。
* ``--rtsp-publish-url`` 仅修改 FFmpeg 推流目标；ONVIF 返回地址仍使用公布的
  主机名/IP。如需返回既有的外部 RTSP 流，使用 ``--stream-uri``；此模式不
  启动内置 FFmpeg 发布器，用户需自行保证该地址有媒体流可读。
* ``--no-video`` 仅启动发现和 SOAP 服务，不启动视频发布器；此模式下仍需
  外部媒体流才能成功录像。
* 本模拟器不实现 ONVIF 用户认证。录像脚本不要传 ``--username``；它仅适用于
  已配置认证的真实摄像头。
* 按 Ctrl+C 可停止模拟器及其启动的 FFmpeg 子进程。HTTP 默认端口为 8080，
  WS-Discovery 使用 UDP 3702，RTSP 默认端口为 8554；启动前确认端口未被占用。
"""
import argparse
from collections import deque
import ipaddress
import math
import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
DISCOVERY_NS = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
ADDRESSING_NS = "http://www.w3.org/2005/08/addressing"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
STOP = threading.Event()
MAX_REQUEST_SIZE = 1 << 20

for prefix, namespace in (
    ("s", SOAP_NS),
    ("a", ADDRESSING_NS),
    ("d", DISCOVERY_NS),
    ("tds", DEVICE_NS),
    ("trt", MEDIA_NS),
    ("tt", SCHEMA_NS),
    ("tptz", PTZ_NS),
):
    ET.register_namespace(prefix, namespace)


def parse_resolution(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().split("x", 1)
        resolution = int(width), int(height)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("分辨率格式应为 WIDTHxHEIGHT，例如 1920x1080") from exc
    if min(resolution) <= 0:
        raise argparse.ArgumentTypeError("分辨率宽高必须大于 0")
    return resolution


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def child_text(element: ET.Element, name: str, default: str = "") -> str:
    for child in element.iter():
        if local_name(child) == name and child.text:
            return child.text.strip()
    return default


def endpoint_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class Camera:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.uuid = args.uuid or str(uuid.uuid4())
        self.endpoint = f"urn:uuid:{self.uuid}"
        self.device_url = f"http://{args.advertise_host}:{args.http_port}/onvif/device_service"
        self.media_url = f"http://{args.advertise_host}:{args.http_port}/onvif/media_service"
        self.ptz_url = f"http://{args.advertise_host}:{args.http_port}/onvif/ptz_service"
        publish_uri = getattr(args, "rtsp_publish_url", "") or f"rtsp://127.0.0.1:{args.rtsp_port}/live/onvif/{self.uuid}"
        publish_parts = urlsplit(publish_uri)
        if publish_parts.scheme.lower() != "rtsp" or not publish_parts.hostname:
            raise ValueError("--rtsp-publish-url 必须是有效的 rtsp:// URL")
        if publish_parts.username is not None or publish_parts.password is not None:
            raise ValueError("--rtsp-publish-url 不支持 URL 内嵌凭据")
        try:
            publish_port = publish_parts.port or 554
        except ValueError as exc:
            raise ValueError(f"--rtsp-publish-url 端口无效：{exc}") from exc
        if not 1 <= publish_port <= 65535:
            raise ValueError("--rtsp-publish-url 端口范围必须为 1-65535")
        self.publish_uri = publish_uri
        self.stream_uri = args.stream_uri or urlunsplit(
            ("rtsp", f"{args.advertise_host}:{publish_port}", publish_parts.path or "/", "", "")
        )

    def handle_soap(self, payload: bytes) -> bytes:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ValueError(f"invalid SOAP XML: {exc}") from exc
        body = next((item for item in root if local_name(item) == "Body"), None)
        if body is None or not list(body):
            raise ValueError("SOAP Body is required")
        request = list(body)[0]
        operation = local_name(request)
        namespace = {
            "GetCapabilities": DEVICE_NS,
            "GetServices": DEVICE_NS,
            "GetDeviceInformation": DEVICE_NS,
            "GetProfiles": MEDIA_NS,
            "GetVideoSources": MEDIA_NS,
            "GetVideoEncoderConfigurations": MEDIA_NS,
            "GetVideoEncoderConfigurationOptions": MEDIA_NS,
            "GetStreamUri": MEDIA_NS,
            "GetSnapshotUri": MEDIA_NS,
            "GetNodes": PTZ_NS,
            "GetConfigurations": PTZ_NS,
            "GetConfigurationOptions": PTZ_NS,
            "GetStatus": PTZ_NS,
            "ContinuousMove": PTZ_NS,
            "RelativeMove": PTZ_NS,
            "AbsoluteMove": PTZ_NS,
            "Stop": PTZ_NS,
        }.get(operation)
        if namespace is None:
            raise NotImplementedError(f"unsupported ONVIF operation: {operation}")
        return self._response(operation, namespace)

    def _response(self, operation: str, namespace: str) -> bytes:
        envelope = ET.Element(ET.QName(SOAP_NS, "Envelope"))
        body = ET.SubElement(envelope, ET.QName(SOAP_NS, "Body"))
        response = ET.SubElement(body, ET.QName(namespace, operation + "Response"))

        if operation == "GetCapabilities":
            capabilities = ET.SubElement(response, ET.QName(DEVICE_NS, "Capabilities"))
            for name, address in (("Device", self.device_url), ("Media", self.media_url), ("PTZ", self.ptz_url)):
                service = ET.SubElement(capabilities, ET.QName(SCHEMA_NS, name))
                ET.SubElement(service, ET.QName(SCHEMA_NS, "XAddr")).text = address
        elif operation == "GetServices":
            for namespace_uri, version, address in (
                ("http://www.onvif.org/ver10/device/wsdl", "1.0", self.device_url),
                ("http://www.onvif.org/ver10/media/wsdl", "1.0", self.media_url),
                ("http://www.onvif.org/ver20/ptz/wsdl", "2.0", self.ptz_url),
            ):
                service = ET.SubElement(response, ET.QName(SCHEMA_NS, "Service"))
                ET.SubElement(service, ET.QName(SCHEMA_NS, "Namespace")).text = namespace_uri
                ET.SubElement(service, ET.QName(SCHEMA_NS, "XAddr")).text = address
                version_node = ET.SubElement(service, ET.QName(SCHEMA_NS, "Version"))
                ET.SubElement(version_node, ET.QName(SCHEMA_NS, "Major")).text = version.split(".")[0]
                ET.SubElement(version_node, ET.QName(SCHEMA_NS, "Minor")).text = version.split(".")[1]
        elif operation == "GetDeviceInformation":
            for name, value in (
                ("Manufacturer", "Vizbox"),
                ("Model", self.args.model),
                ("FirmwareVersion", "1.0"),
                ("SerialNumber", self.uuid),
                ("HardwareId", "ONVIF-Simulator"),
            ):
                ET.SubElement(response, ET.QName(DEVICE_NS, name)).text = value
        elif operation == "GetProfiles":
            profile = ET.SubElement(response, ET.QName(MEDIA_NS, "Profiles"), {"token": "Profile_1", "fixed": "true"})
            ET.SubElement(profile, ET.QName(SCHEMA_NS, "Name")).text = "MainStream"
            source = ET.SubElement(profile, ET.QName(SCHEMA_NS, "VideoSourceConfiguration"), {"token": "VideoSource_1"})
            ET.SubElement(source, ET.QName(SCHEMA_NS, "Name")).text = "VideoSource"
            ET.SubElement(source, ET.QName(SCHEMA_NS, "UseCount")).text = "1"
            ET.SubElement(source, ET.QName(SCHEMA_NS, "SourceToken")).text = "VideoSource_1"
            bounds = ET.SubElement(source, ET.QName(SCHEMA_NS, "Bounds"))
            for name, value in (("x", "0"), ("y", "0"), ("width", str(self.args.width)), ("height", str(self.args.height))):
                bounds.set(name, value)
            encoder = ET.SubElement(profile, ET.QName(SCHEMA_NS, "VideoEncoderConfiguration"), {"token": "VideoEncoder_1"})
            ET.SubElement(encoder, ET.QName(SCHEMA_NS, "Name")).text = "H264"
            ET.SubElement(encoder, ET.QName(SCHEMA_NS, "UseCount")).text = "1"
            ET.SubElement(encoder, ET.QName(SCHEMA_NS, "Encoding")).text = "H264"
            resolution = ET.SubElement(encoder, ET.QName(SCHEMA_NS, "Resolution"))
            ET.SubElement(resolution, ET.QName(SCHEMA_NS, "Width")).text = str(self.args.width)
            ET.SubElement(resolution, ET.QName(SCHEMA_NS, "Height")).text = str(self.args.height)
            rate = ET.SubElement(encoder, ET.QName(SCHEMA_NS, "RateControl"))
            ET.SubElement(rate, ET.QName(SCHEMA_NS, "FrameRateLimit")).text = f"{self.args.fps:g}"
            ET.SubElement(rate, ET.QName(SCHEMA_NS, "EncodingInterval")).text = "1"
            ET.SubElement(rate, ET.QName(SCHEMA_NS, "BitrateLimit")).text = "4096"
            ET.SubElement(profile, ET.QName(SCHEMA_NS, "PTZConfiguration"), {"token": "PTZConfiguration_1"})
        elif operation in ("GetVideoSources", "GetVideoEncoderConfigurations"):
            if operation == "GetVideoSources":
                source = ET.SubElement(response, ET.QName(SCHEMA_NS, "VideoSources"), {"token": "VideoSource_1"})
                ET.SubElement(source, ET.QName(SCHEMA_NS, "Framerate")).text = f"{self.args.fps:g}"
                resolution = ET.SubElement(source, ET.QName(SCHEMA_NS, "Resolution"))
                ET.SubElement(resolution, ET.QName(SCHEMA_NS, "Width")).text = str(self.args.width)
                ET.SubElement(resolution, ET.QName(SCHEMA_NS, "Height")).text = str(self.args.height)
            else:
                configuration = ET.SubElement(response, ET.QName(SCHEMA_NS, "Configurations"), {"token": "VideoEncoder_1"})
                ET.SubElement(configuration, ET.QName(SCHEMA_NS, "Name")).text = "H264"
                ET.SubElement(configuration, ET.QName(SCHEMA_NS, "UseCount")).text = "1"
                ET.SubElement(configuration, ET.QName(SCHEMA_NS, "Encoding")).text = "H264"
                resolution = ET.SubElement(configuration, ET.QName(SCHEMA_NS, "Resolution"))
                ET.SubElement(resolution, ET.QName(SCHEMA_NS, "Width")).text = str(self.args.width)
                ET.SubElement(resolution, ET.QName(SCHEMA_NS, "Height")).text = str(self.args.height)
                rate = ET.SubElement(configuration, ET.QName(SCHEMA_NS, "RateControl"))
                ET.SubElement(rate, ET.QName(SCHEMA_NS, "FrameRateLimit")).text = f"{self.args.fps:g}"
                ET.SubElement(rate, ET.QName(SCHEMA_NS, "EncodingInterval")).text = "1"
                ET.SubElement(rate, ET.QName(SCHEMA_NS, "BitrateLimit")).text = "4096"
        elif operation == "GetVideoEncoderConfigurationOptions":
            options = ET.SubElement(response, ET.QName(SCHEMA_NS, "Options"))
            resolutions = ET.SubElement(options, ET.QName(SCHEMA_NS, "ResolutionsAvailable"))
            ET.SubElement(resolutions, ET.QName(SCHEMA_NS, "Width")).text = str(self.args.width)
            ET.SubElement(resolutions, ET.QName(SCHEMA_NS, "Height")).text = str(self.args.height)
            rate = ET.SubElement(options, ET.QName(SCHEMA_NS, "RateControl"))
            ET.SubElement(rate, ET.QName(SCHEMA_NS, "FrameRateRange"))
            ET.SubElement(rate[-1], ET.QName(SCHEMA_NS, "Min")).text = f"{self.args.fps:g}"
            ET.SubElement(rate[-1], ET.QName(SCHEMA_NS, "Max")).text = f"{self.args.fps:g}"
        elif operation in ("GetStreamUri", "GetSnapshotUri"):
            media_uri = ET.SubElement(response, ET.QName(SCHEMA_NS, "MediaUri"))
            ET.SubElement(media_uri, ET.QName(SCHEMA_NS, "Uri")).text = self.stream_uri
            ET.SubElement(media_uri, ET.QName(SCHEMA_NS, "InvalidAfterConnect")).text = "false"
            ET.SubElement(media_uri, ET.QName(SCHEMA_NS, "InvalidAfterReboot")).text = "false"
            ET.SubElement(media_uri, ET.QName(SCHEMA_NS, "Timeout")).text = "PT0S"
        elif operation == "GetNodes":
            node = ET.SubElement(response, ET.QName(PTZ_NS, "PTZNode"), {"token": "PTZNode_1"})
            ET.SubElement(node, ET.QName(SCHEMA_NS, "Name")).text = "PTZ"
            ET.SubElement(node, ET.QName(SCHEMA_NS, "MaximumNumberOfPresets")).text = "0"
        elif operation == "GetConfigurations":
            configuration = ET.SubElement(response, ET.QName(PTZ_NS, "PTZConfiguration"), {"token": "PTZConfiguration_1"})
            ET.SubElement(configuration, ET.QName(SCHEMA_NS, "Name")).text = "PTZ"
            ET.SubElement(configuration, ET.QName(SCHEMA_NS, "UseCount")).text = "1"
            ET.SubElement(configuration, ET.QName(SCHEMA_NS, "NodeToken")).text = "PTZNode_1"
        elif operation == "GetStatus":
            status = ET.SubElement(response, ET.QName(PTZ_NS, "PTZStatus"))
            position = ET.SubElement(status, ET.QName(SCHEMA_NS, "Position"))
            ET.SubElement(position, ET.QName(SCHEMA_NS, "PanTilt"), {"x": "0", "y": "0"})
            ET.SubElement(position, ET.QName(SCHEMA_NS, "Zoom"), {"x": "0"})
            ET.SubElement(status, ET.QName(SCHEMA_NS, "MoveStatus"))
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def discovery_response(self, request: ET.Element, operation: str) -> bytes:
        envelope = ET.Element(ET.QName(SOAP_NS, "Envelope"))
        header = ET.SubElement(envelope, ET.QName(SOAP_NS, "Header"))
        message_id = child_text(request, "MessageID")
        request_header = next((item for item in request if local_name(item) == "Header"), None)
        reply_to = ""
        if request_header is not None:
            reply_to_node = next((item for item in request_header if local_name(item) == "ReplyTo"), None)
            if reply_to_node is not None:
                reply_to = child_text(reply_to_node, "Address")
        reply_to = reply_to or f"{ADDRESSING_NS}/role/anonymous"
        to = ET.SubElement(header, ET.QName(ADDRESSING_NS, "To"))
        to.text = reply_to
        ET.SubElement(header, ET.QName(ADDRESSING_NS, "Action")).text = f"{DISCOVERY_NS}/{operation}Matches"
        ET.SubElement(header, ET.QName(ADDRESSING_NS, "MessageID")).text = f"urn:uuid:{uuid.uuid4()}"
        if message_id:
            ET.SubElement(header, ET.QName(ADDRESSING_NS, "RelatesTo")).text = message_id
        body = ET.SubElement(envelope, ET.QName(SOAP_NS, "Body"))
        matches = ET.SubElement(body, ET.QName(DISCOVERY_NS, operation + "Matches"))
        match = ET.SubElement(matches, ET.QName(DISCOVERY_NS, "ProbeMatch" if operation == "Probe" else "ResolveMatch"))
        reference = ET.SubElement(match, ET.QName(ADDRESSING_NS, "EndpointReference"))
        ET.SubElement(reference, ET.QName(ADDRESSING_NS, "Address")).text = self.endpoint
        types = ET.SubElement(match, ET.QName(DISCOVERY_NS, "Types"), {
            "xmlns:dn": "http://www.onvif.org/ver10/network/wsdl",
        })
        types.text = "dn:NetworkVideoTransmitter"
        ET.SubElement(match, ET.QName(DISCOVERY_NS, "Scopes")).text = (
            f"onvif://www.onvif.org/name/{self.args.name.replace(' ', '%20')} "
            "onvif://www.onvif.org/type/NetworkVideoTransmitter "
            "onvif://www.onvif.org/hardware/ONVIF-Simulator"
        )
        ET.SubElement(match, ET.QName(DISCOVERY_NS, "XAddrs")).text = self.device_url
        ET.SubElement(match, ET.QName(DISCOVERY_NS, "MetadataVersion")).text = "1"
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


class SOAPHandler(BaseHTTPRequestHandler):
    camera: Camera

    def do_POST(self) -> None:
        if urlparse(self.path).path not in ("/onvif/device_service", "/onvif/media_service", "/onvif/ptz_service"):
            self.send_error(404)
            return
        length = self.headers.get("Content-Length")
        if not length:
            self.send_error(411, "Content-Length required")
            return
        try:
            size = int(length)
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if size < 0 or size > MAX_REQUEST_SIZE:
            self.send_error(413, "SOAP request too large")
            return
        try:
            response = self.camera.handle_soap(self.rfile.read(size))
        except NotImplementedError as exc:
            self.send_error(400, str(exc))
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[http] {self.address_string()} - {fmt % args}", file=sys.stderr, flush=True)


def discovery_loop(camera: Camera, sock: socket.socket) -> None:
    try:
        while not STOP.is_set():
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                request = ET.fromstring(data)
            except ET.ParseError:
                continue
            body = next((item for item in request if local_name(item) == "Body"), None)
            if body is None or not list(body):
                continue
            operation = local_name(list(body)[0])
            if operation == "Probe":
                types = child_text(list(body)[0], "Types")
                if types and not any(kind in types for kind in ("NetworkVideoTransmitter", "Device")):
                    continue
            elif operation == "Resolve":
                address = child_text(list(body)[0], "Address")
                if address and address != camera.endpoint:
                    continue
            else:
                continue
            response = camera.discovery_response(request, operation)
            try:
                sock.sendto(response, peer)
            except OSError as exc:
                print(f"WS-Discovery response failed: {exc}", file=sys.stderr, flush=True)
    finally:
        sock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ONVIF 摄像头模拟器（WS-Discovery + Device/Media/PTZ SOAP + H.264 测试视频）",
        epilog=(
            "前置条件：默认视频模式需要支持 lavfi/libx264/RTSP 的 FFmpeg，以及本机可访问的 RTSP 服务"
            "（默认项目 ZLMediaKit，127.0.0.1:8554）。启动 ZLMediaKit：\n"
            "  docker compose -f deploy/compose/compose.dev.yaml up -d zlm\n"
            "另请参阅脚本顶部的完整前置条件、局域网端口说明和录像示例。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--listen-host", default="0.0.0.0", help="HTTP 监听地址（默认 0.0.0.0）")
    parser.add_argument("--http-port", type=int, default=8080, help="ONVIF HTTP 端口（默认 8080）")
    parser.add_argument("--advertise-host", default="", help="WS-Discovery/ONVIF 中公布的 IP，默认自动检测")
    parser.add_argument("--name", default="Vizbox ONVIF Camera", help="摄像头名称")
    parser.add_argument("--model", default="ONVIF-Simulator", help="设备型号")
    parser.add_argument("--uuid", default="", help="固定设备 UUID，默认启动时生成")
    parser.add_argument("--resolution", type=parse_resolution, default=(1280, 720), help="视频分辨率 WIDTHxHEIGHT（默认 1280x720）")
    parser.add_argument("--fps", type=float, default=25.0, help="视频帧率（默认 25）")
    parser.add_argument("--rtsp-port", type=int, default=8554, help="RTSP 服务端口（默认 8554）")
    parser.add_argument("--rtsp-publish-url", default="", help="FFmpeg 发布测试视频的 RTSP 地址；默认发布到本机 ZLMediaKit")
    parser.add_argument("--stream-uri", default="", help="GetStreamUri 返回的 RTSP 地址；指定后不自动生成测试视频")
    parser.add_argument("--ffmpeg", default="", help="FFmpeg 可执行文件路径")
    parser.add_argument("--no-video", action="store_true", help="仅运行 ONVIF 控制面，不生成/发布测试视频")
    args = parser.parse_args()
    args.width, args.height = args.resolution
    if not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("--fps 必须是大于 0 的有限数值")
    if not 1 <= args.http_port <= 65535 or not 1 <= args.rtsp_port <= 65535:
        parser.error("端口范围必须为 1-65535")
    args.advertise_host = args.advertise_host or endpoint_ip()
    try:
        if ipaddress.ip_address(args.advertise_host).version != 4:
            raise ValueError("IPv4 required")
    except ValueError:
        parser.error("--advertise-host 必须是 IPv4 地址")
    return args


def find_ffmpeg(explicit: str) -> str:
    candidate = explicit or os.environ.get("VZ_FFMPEG_PATH", "") or shutil.which("ffmpeg") or ""
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    raise RuntimeError("找不到 FFmpeg；请安装 FFmpeg，或使用 --ffmpeg 指定路径")


def build_video_command(ffmpeg: str, camera: Camera) -> list[str]:
    args = camera.args
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={args.width}x{args.height}:rate={args.fps:g}",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(max(2, round(args.fps * 2))),
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        camera.publish_uri,
    ]


def start_video(camera: Camera, ffmpeg: str) -> tuple[subprocess.Popen, threading.Thread, deque[str]]:
    try:
        process = subprocess.Popen(
            build_video_command(ffmpeg, camera),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"无法启动 FFmpeg：{exc}") from exc
    logs: deque[str] = deque(maxlen=30)

    def collect_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            logs.append(line.rstrip())

    reader = threading.Thread(target=collect_stderr, daemon=True)
    reader.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            reader.join(timeout=1)
            details = "\n".join(logs)
            raise RuntimeError(
                f"FFmpeg 发布测试流失败，请确认 RTSP 服务可用：{camera.publish_uri}"
                + (f"\n{details}" if details else "")
            )
        time.sleep(0.1)
    return process, reader, logs


def stop_video(process: subprocess.Popen | None, reader: threading.Thread | None = None) -> None:
    if process is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if reader is not None:
        reader.join(timeout=1)
    if process is not None and process.stderr is not None:
        process.stderr.close()


def main() -> int:
    args = parse_args()
    try:
        camera = Camera(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    handler = type("CameraSOAPHandler", (SOAPHandler,), {"camera": camera})
    try:
        server = ThreadingHTTPServer((args.listen_host, args.http_port), handler)
    except OSError as exc:
        raise SystemExit(f"无法监听 HTTP 服务：{exc}") from exc
    try:
        discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        discovery_socket.bind(("", 3702))
        membership = socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0")
        discovery_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        discovery_socket.settimeout(0.5)
    except OSError as exc:
        server.server_close()
        raise SystemExit(f"无法启动 WS-Discovery：{exc}") from exc
    discovery = threading.Thread(target=discovery_loop, args=(camera, discovery_socket), daemon=True)
    try:
        discovery.start()
    except OSError as exc:
        discovery_socket.close()
        server.server_close()
        raise SystemExit(f"无法启动 WS-Discovery：{exc}") from exc
    server.timeout = 0.5
    signal.signal(signal.SIGINT, lambda _signum, _frame: STOP.set())
    signal.signal(signal.SIGTERM, lambda _signum, _frame: STOP.set())
    video_process = None
    stderr_reader = None
    video_logs = None
    if not args.no_video and not args.stream_uri:
        try:
            video_process, stderr_reader, video_logs = start_video(camera, find_ffmpeg(args.ffmpeg))
        except RuntimeError as exc:
            discovery_socket.close()
            server.server_close()
            raise SystemExit(str(exc)) from exc
    print(
        f"ONVIF camera {args.name!r} at http://{args.advertise_host}:{args.http_port} "
        f"({args.width}x{args.height}@{args.fps:g} fps; UUID {camera.uuid})",
        flush=True,
    )
    print(f"GetStreamUri returns {camera.stream_uri}", flush=True)
    if video_process is not None:
        print(f"Publishing generated {args.width}x{args.height}@{args.fps:g} H.264 video to {camera.publish_uri}", flush=True)
    elif args.no_video:
        print("Video publishing disabled (--no-video)", flush=True)
    elif args.stream_uri:
        print("Using external RTSP stream (--stream-uri); no video will be generated", flush=True)
    try:
        while not STOP.is_set():
            if video_process is not None and video_process.poll() is not None:
                details = "\n".join(video_logs or ())
                print(
                    "FFmpeg video publisher exited unexpectedly"
                    + (f":\n{details}" if details else ""),
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            server.handle_request()
    finally:
        server.server_close()
        STOP.set()
        discovery.join(timeout=1)
        stop_video(video_process, stderr_reader)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
