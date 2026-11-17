#!/usr/bin/env python3
"""Discover ONVIF cameras using WS-Discovery multicast on the local network."""
import argparse
import ipaddress
import math
import socket
import time
import uuid
from dataclasses import dataclass
from xml.etree import ElementTree as ET

DISCOVERY_GROUP = "239.255.255.250"
DISCOVERY_PORT = 3702
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
ADDRESSING_NS = "http://www.w3.org/2005/08/addressing"
DISCOVERY_NS = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NETWORK_NS = "http://www.onvif.org/ver10/network/wsdl"

ET.register_namespace("s", SOAP_NS)
ET.register_namespace("a", ADDRESSING_NS)
ET.register_namespace("d", DISCOVERY_NS)
ET.register_namespace("dn", NETWORK_NS)


@dataclass(frozen=True)
class Camera:
    endpoint: str
    xaddrs: tuple[str, ...]
    scopes: tuple[str, ...]


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def parse_matches(payload: bytes) -> list[Camera]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    cameras = []
    for element in root.iter():
        if local_name(element) != "ProbeMatch":
            continue
        endpoint = next(
            (node.text.strip() for node in element.iter() if local_name(node) == "Address" and node.text),
            "",
        )
        xaddrs = next(
            (node.text.split() for node in element.iter() if local_name(node) == "XAddrs" and node.text),
            [],
        )
        scopes = next(
            (node.text.split() for node in element.iter() if local_name(node) == "Scopes" and node.text),
            [],
        )
        if endpoint or xaddrs:
            cameras.append(Camera(endpoint, tuple(xaddrs), tuple(scopes)))
    return cameras


def probe_message() -> bytes:
    envelope = ET.Element(ET.QName(SOAP_NS, "Envelope"))
    header = ET.SubElement(envelope, ET.QName(SOAP_NS, "Header"))
    ET.SubElement(header, ET.QName(ADDRESSING_NS, "To")).text = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
    ET.SubElement(header, ET.QName(ADDRESSING_NS, "Action")).text = f"{DISCOVERY_NS}/Probe"
    ET.SubElement(header, ET.QName(ADDRESSING_NS, "MessageID")).text = f"urn:uuid:{uuid.uuid4()}"
    reply_to = ET.SubElement(header, ET.QName(ADDRESSING_NS, "ReplyTo"))
    ET.SubElement(reply_to, ET.QName(ADDRESSING_NS, "Address")).text = f"{ADDRESSING_NS}/role/anonymous"
    body = ET.SubElement(envelope, ET.QName(SOAP_NS, "Body"))
    probe = ET.SubElement(body, ET.QName(DISCOVERY_NS, "Probe"))
    ET.SubElement(probe, ET.QName(DISCOVERY_NS, "Types")).text = "dn:NetworkVideoTransmitter"
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def discover(timeout: float = 3.0, interface: str = "0.0.0.0") -> list[Camera]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than zero")
    if interface != "0.0.0.0":
        try:
            if ipaddress.ip_address(interface).version != 4:
                raise ValueError("interface must be an IPv4 address")
        except ValueError as exc:
            raise ValueError("interface must be an IPv4 address") from exc

    found: dict[str, Camera] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        if interface != "0.0.0.0":
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
            sock.bind((interface, 0))
        sock.sendto(probe_message(), (DISCOVERY_GROUP, DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            for camera in parse_matches(payload):
                key = camera.endpoint or " ".join(camera.xaddrs)
                previous = found.get(key)
                if previous:
                    found[key] = Camera(
                        camera.endpoint or previous.endpoint,
                        tuple(dict.fromkeys((*previous.xaddrs, *camera.xaddrs))),
                        tuple(dict.fromkeys((*previous.scopes, *camera.scopes))),
                    )
                else:
                    found[key] = camera
    return sorted(found.values(), key=lambda camera: camera.endpoint or " ".join(camera.xaddrs))


def main() -> int:
    parser = argparse.ArgumentParser(description="在当前局域网通过 WS-Discovery 查找 ONVIF 摄像头")
    parser.add_argument("--timeout", type=float, default=3.0, help="等待发现响应的秒数（默认 3）")
    parser.add_argument("--interface", default="0.0.0.0", help="指定用于发送组播的本机 IPv4 地址")
    args = parser.parse_args()
    try:
        cameras = discover(args.timeout, args.interface)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not cameras:
        print("未发现 ONVIF 摄像头。请检查设备与本机是否在同一局域网，以及组播/UDP 3702 是否可达。")
        return 0
    for index, camera in enumerate(cameras, 1):
        print(f"[{index}] {camera.endpoint or '(no endpoint reference)'}")
        for address in camera.xaddrs:
            print(f"    XAddr: {address}")
        for scope in camera.scopes:
            print(f"    Scope: {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
