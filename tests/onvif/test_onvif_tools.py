import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pull_10s
import scan_onvif


class OnvifToolTests(unittest.TestCase):
    def test_discovery_probe_targets_onvif_transmitters(self) -> None:
        payload = scan_onvif.probe_message()
        request = ET.fromstring(payload)
        self.assertIn("NetworkVideoTransmitter", payload.decode())
        self.assertEqual(scan_onvif.local_name(request), "Envelope")

    def test_discovery_response_is_parsed(self) -> None:
        payload = (
            f'<s:Envelope xmlns:s="{scan_onvif.SOAP_NS}" xmlns:d="{scan_onvif.DISCOVERY_NS}" '
            f'xmlns:a="{scan_onvif.ADDRESSING_NS}"><s:Body><d:ProbeMatches><d:ProbeMatch>'
            f"<a:EndpointReference><a:Address>urn:uuid:cam-1</a:Address></a:EndpointReference>"
            f"<d:XAddrs>http://192.168.1.20/onvif/device_service</d:XAddrs>"
            f"<d:Scopes>onvif://www.onvif.org/name/Camera</d:Scopes>"
            "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
        ).encode()
        cameras = scan_onvif.parse_matches(payload)
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0].endpoint, "urn:uuid:cam-1")
        self.assertEqual(cameras[0].xaddrs, ("http://192.168.1.20/onvif/device_service",))

    def test_device_url_normalization(self) -> None:
        self.assertEqual(
            pull_10s.normalize_device_url("http://192.168.1.20"),
            "http://192.168.1.20/onvif/device_service",
        )
        with self.assertRaises(ValueError):
            pull_10s.normalize_device_url("http://camera@192.168.1.20")

    def test_rtsp_credentials_are_url_encoded(self) -> None:
        uri = pull_10s.add_rtsp_credentials("rtsp://192.168.1.20/live", "cam user", "p@ss:word")
        self.assertEqual(uri, "rtsp://cam%20user:p%40ss%3Aword@192.168.1.20/live")

    def test_resolves_rtsp_uri_through_onvif_media_service(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                root = ET.fromstring(self.rfile.read(int(self.headers["Content-Length"])))
                operation = next(node for node in root.iter() if pull_10s.local_name(node) in (
                    "GetCapabilities", "GetProfiles", "GetStreamUri"
                ))
                operation_name = pull_10s.local_name(operation)
                requests.append((self.path, operation_name, root))
                envelope = ET.Element(ET.QName(pull_10s.SOAP_NS, "Envelope"))
                body = ET.SubElement(envelope, ET.QName(pull_10s.SOAP_NS, "Body"))
                response = ET.SubElement(body, ET.QName(pull_10s.MEDIA_NS, operation_name + "Response"))
                if operation_name == "GetCapabilities":
                    caps = ET.SubElement(response, ET.QName(pull_10s.DEVICE_NS, "Capabilities"))
                    media = ET.SubElement(caps, ET.QName(pull_10s.SCHEMA_NS, "Media"))
                    ET.SubElement(media, ET.QName(pull_10s.SCHEMA_NS, "XAddr")).text = (
                        f"http://127.0.0.1:{self.server.server_port}/onvif/media_service"
                    )
                elif operation_name == "GetProfiles":
                    ET.SubElement(response, ET.QName(pull_10s.MEDIA_NS, "Profiles"), {"token": "main"})
                else:
                    uri = ET.SubElement(response, ET.QName(pull_10s.SCHEMA_NS, "MediaUri"))
                    ET.SubElement(uri, ET.QName(pull_10s.SCHEMA_NS, "Uri")).text = "rtsp://192.168.1.20/live"
                payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _fmt, *_args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            device_url = f"http://127.0.0.1:{server.server_port}/onvif/device_service"
            uri = pull_10s.get_stream_uri(device_url, "tester", "secret")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(uri, "rtsp://192.168.1.20/live")
        self.assertEqual([request[1] for request in requests], ["GetCapabilities", "GetProfiles", "GetStreamUri"])
        for _path, _operation, root in requests:
            token = next(node for node in root.iter() if pull_10s.local_name(node) == "UsernameToken")
            password = next(node for node in token if pull_10s.local_name(node) == "Password")
            self.assertEqual(password.attrib["Type"], pull_10s.PASSWORD_DIGEST_TYPE)

    def test_capture_is_limited_to_ten_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.mkv"
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"video")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(pull_10s.subprocess, "run", side_effect=fake_run):
                pull_10s.capture_10_seconds("/usr/bin/ffmpeg", "rtsp://192.168.1.20/live", output)
            self.assertEqual(commands[0][commands[0].index("-t") + 1], "10")
            self.assertEqual(commands[0][-2], "-y")
            self.assertEqual(output.read_bytes(), b"video")

    def test_capture_does_not_replace_existing_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.mkv"
            output.write_bytes(b"keep")
            with self.assertRaisesRegex(RuntimeError, "--overwrite"):
                pull_10s.capture_10_seconds("/usr/bin/ffmpeg", "rtsp://192.168.1.20/live", output)
            self.assertEqual(output.read_bytes(), b"keep")

    def test_failed_capture_removes_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.mkv"

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"partial")
                return type("Result", (), {"returncode": 1, "stderr": "unable to connect"})()

            with patch.object(pull_10s.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "FFmpeg 拉流失败"):
                    pull_10s.capture_10_seconds("/usr/bin/ffmpeg", "rtsp://192.168.1.20/live", output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
