#!/usr/bin/env python3
"""Resolve an ONVIF RTSP URI and save up to 10 seconds of video using FFmpeg."""
import argparse
import base64
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from xml.etree import ElementTree as ET

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
NONCE_ENCODING_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
MAX_SOAP_RESPONSE = 1 << 20
CAPTURE_SECONDS = 10

ET.register_namespace("s", SOAP_NS)
ET.register_namespace("tds", DEVICE_NS)
ET.register_namespace("trt", MEDIA_NS)
ET.register_namespace("tt", SCHEMA_NS)
ET.register_namespace("wsse", WSSE_NS)
ET.register_namespace("wsu", WSU_NS)


def local_name(element: ET.Element) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def normalize_device_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("ONVIF URL 必须是 http(s) URL，例如 http://192.168.1.20/onvif/device_service")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("请通过 --username 提供凭据，不要把用户名或密码放进 URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"ONVIF URL 端口无效：{exc}") from exc
    path = parsed.path.rstrip("/")
    if not path:
        path = "/onvif/device_service"
    elif path.lower().endswith("/onvif/device_service"):
        pass
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def username_token(username: str, password: str) -> ET.Element:
    header = ET.Element(ET.QName(SOAP_NS, "Header"))
    security = ET.SubElement(header, ET.QName(WSSE_NS, "Security"), {ET.QName(SOAP_NS, "mustUnderstand"): "1"})
    token = ET.SubElement(security, ET.QName(WSSE_NS, "UsernameToken"))
    ET.SubElement(token, ET.QName(WSSE_NS, "Username")).text = username
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    digest = hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    password_node = ET.SubElement(
        token,
        ET.QName(WSSE_NS, "Password"),
        {"Type": PASSWORD_DIGEST_TYPE},
    )
    password_node.text = base64.b64encode(digest).decode("ascii")
    nonce_node = ET.SubElement(
        token,
        ET.QName(WSSE_NS, "Nonce"),
        {"EncodingType": NONCE_ENCODING_TYPE},
    )
    nonce_node.text = base64.b64encode(nonce).decode("ascii")
    ET.SubElement(token, ET.QName(WSU_NS, "Created")).text = created
    return header


def soap_call(
    endpoint: str,
    operation: ET.Element,
    username: str = "",
    password: str = "",
    timeout: float = 8.0,
) -> ET.Element:
    envelope = ET.Element(ET.QName(SOAP_NS, "Envelope"))
    if username:
        envelope.append(username_token(username, password))
    body = ET.SubElement(envelope, ET.QName(SOAP_NS, "Body"))
    body.append(operation)
    qualified_name = str(operation.tag)
    namespace = qualified_name[1:].split("}", 1)[0]
    action = f"{namespace}/{local_name(operation)}"
    request = urllib.request.Request(
        endpoint,
        data=ET.tostring(envelope, encoding="utf-8", xml_declaration=True),
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
            "SOAPAction": f'"{action}"',
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener()
        if username:
            credentials = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            credentials.add_password(None, endpoint, username, password)
            opener.add_handler(urllib.request.HTTPDigestAuthHandler(credentials))
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_SOAP_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ONVIF HTTP 请求失败（HTTP {exc.code}），请检查设备地址和认证信息") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 ONVIF 服务：{exc.reason}") from exc
    if len(payload) > MAX_SOAP_RESPONSE:
        raise RuntimeError("ONVIF SOAP 响应超过 1 MiB")
    try:
        result = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError(f"ONVIF 返回无效 XML：{exc}") from exc
    fault = next((node for node in result.iter() if local_name(node) == "Fault"), None)
    if fault is not None:
        reason = " ".join(node.text.strip() for node in fault.iter() if node.text and node.text.strip())
        raise RuntimeError(f"ONVIF SOAP Fault: {reason}")
    return result


def child_text(element: ET.Element, name: str) -> str:
    node = next((item for item in element.iter() if local_name(item) == name and item.text), None)
    return node.text.strip() if node is not None and node.text else ""


def find_media_endpoint(device_url: str, username: str, password: str) -> str:
    operation = ET.Element(ET.QName(DEVICE_NS, "GetCapabilities"))
    ET.SubElement(operation, ET.QName(DEVICE_NS, "Category")).text = "All"
    response = soap_call(device_url, operation, username, password)
    media = next((node for node in response.iter() if local_name(node) == "Media"), None)
    xaddr = child_text(media, "XAddr") if media is not None else ""
    if not xaddr:
        raise RuntimeError("设备未在 GetCapabilities 中提供 Media XAddr")
    endpoint = urllib.parse.urljoin(device_url, xaddr)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"设备返回了无效 Media XAddr：{xaddr}")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("设备 Media XAddr 含有 URL 凭据，请在相机配置中移除")
    return endpoint


def get_stream_uri(device_url: str, username: str = "", password: str = "") -> str:
    media_url = find_media_endpoint(device_url, username, password)
    profiles_request = ET.Element(ET.QName(MEDIA_NS, "GetProfiles"))
    profiles_response = soap_call(media_url, profiles_request, username, password)
    profile = next((node for node in profiles_response.iter() if local_name(node) == "Profiles"), None)
    if profile is None or not profile.attrib.get("token"):
        raise RuntimeError("设备未返回可用的 ONVIF Media Profile")

    stream_request = ET.Element(ET.QName(MEDIA_NS, "GetStreamUri"))
    setup = ET.SubElement(stream_request, ET.QName(MEDIA_NS, "StreamSetup"))
    ET.SubElement(setup, ET.QName(SCHEMA_NS, "Stream")).text = "RTP-Unicast"
    transport = ET.SubElement(setup, ET.QName(SCHEMA_NS, "Transport"))
    ET.SubElement(transport, ET.QName(SCHEMA_NS, "Protocol")).text = "RTSP"
    ET.SubElement(stream_request, ET.QName(MEDIA_NS, "ProfileToken")).text = profile.attrib["token"]
    stream_response = soap_call(media_url, stream_request, username, password)
    uri = child_text(stream_response, "Uri")
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme.lower() != "rtsp" or not parsed.netloc:
        raise RuntimeError(f"设备返回了无效 RTSP URI：{uri or '(empty)'}")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("设备返回的 RTSP URI 含有 URL 凭据；请将凭据通过脚本参数单独提供")
    return uri


def find_ffmpeg(explicit: str) -> str:
    candidate = explicit or os.environ.get("VZ_FFMPEG_PATH", "") or shutil.which("ffmpeg") or ""
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    raise RuntimeError("找不到 FFmpeg；请安装 FFmpeg，或使用 --ffmpeg 指定可执行文件路径")


def add_rtsp_credentials(stream_uri: str, username: str, password: str) -> str:
    if not username:
        return stream_uri
    parsed = urllib.parse.urlsplit(stream_uri)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("设备返回了无效 RTSP 主机名")
    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        if parsed.port is not None:
            host += f":{parsed.port}"
    except ValueError as exc:
        raise ValueError(f"设备返回了无效 RTSP 端口：{exc}") from exc
    userinfo = f"{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(password, safe='')}@"
    return parsed._replace(netloc=userinfo + host).geturl()


def capture_10_seconds(
    ffmpeg: str,
    stream_uri: str,
    output: Path,
    username: str = "",
    password: str = "",
    overwrite: bool = False,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise RuntimeError(f"输出文件已存在（如需覆盖请加 --overwrite）：{output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".mkv",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary_output = Path(temporary_name)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
    ]
    if username:
        ffmpeg_uri = add_rtsp_credentials(stream_uri, username, password)
    else:
        ffmpeg_uri = stream_uri
    command.extend(
        [
            "-i",
            ffmpeg_uri,
            "-map",
            "0:v:0",
            "-t",
            str(CAPTURE_SECONDS),
            "-c",
            "copy",
            "-f",
            "matroska",
            "-y",
            str(temporary_output),
        ]
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            details = re.sub(r"rtsp://\S+", stream_uri, result.stderr.strip())
            raise RuntimeError(f"FFmpeg 拉流失败（退出码 {result.returncode}）" + (f":\n{details}" if details else ""))
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise RuntimeError("FFmpeg 未生成有效视频文件")
        if overwrite:
            os.replace(temporary_output, output)
        else:
            try:
                os.link(temporary_output, output)
            except FileExistsError as exc:
                raise RuntimeError(f"输出文件已存在（如需覆盖请加 --overwrite）：{output}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg 拉流超时（30 秒）") from exc
    finally:
        temporary_output.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 ONVIF 摄像头拉取 10 秒视频并保存为 MKV")
    parser.add_argument("--url", required=True, help="ONVIF Device Service URL 或设备 HTTP(S) URL")
    parser.add_argument("--username", default="", help="ONVIF/RTSP 用户名（设置后会提示输入密码）")
    parser.add_argument("--output", type=Path, default=Path("build/demo/onvif-10s.mkv"), help="输出文件路径")
    parser.add_argument("--ffmpeg", default="", help="FFmpeg 可执行文件路径")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有输出文件")
    args = parser.parse_args()
    if (args.username and not sys.stdin.isatty()) and not os.environ.get("ONVIF_PASSWORD"):
        parser.error("非交互运行时请通过 ONVIF_PASSWORD 环境变量提供密码")
    try:
        device_url = normalize_device_url(args.url)
        password = os.environ.get("ONVIF_PASSWORD", "") if args.username else ""
        if args.username and not password:
            password = getpass("ONVIF/RTSP password: ")
        ffmpeg = find_ffmpeg(args.ffmpeg)
        stream_uri = get_stream_uri(device_url, args.username, password)
        print(f"ONVIF RTSP URI resolved; saving up to {CAPTURE_SECONDS}s to {args.output}")
        if args.username:
            print("提示：FFmpeg 需要通过进程参数接收 RTSP 凭据；同一主机上的进程查看者可能看到它们。")
        capture_10_seconds(ffmpeg, stream_uri, args.output, args.username, password, args.overwrite)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"错误：{exc}\n")
    print(f"视频已保存：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
