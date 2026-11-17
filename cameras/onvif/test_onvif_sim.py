import argparse
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from xml.etree import ElementTree as ET

import onvif_sim


def soap_request(operation: str) -> bytes:
    return (
        f'<s:Envelope xmlns:s="{onvif_sim.SOAP_NS}"><s:Body>'
        f'<trt:{operation} xmlns:trt="{onvif_sim.MEDIA_NS}"/>'
        "</s:Body></s:Envelope>"
    ).encode()


class OnvifSimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        args = argparse.Namespace(
            advertise_host="192.0.2.10",
            http_port=8080,
            rtsp_port=8554,
            rtsp_publish_url="",
            stream_uri="",
            name="Test Camera",
            model="Test Model",
            uuid="",
            width=1920,
            height=1080,
            fps=30.0,
        )
        self.camera = onvif_sim.Camera(args)

    def test_profile_exposes_configured_resolution_and_frame_rate(self) -> None:
        response = ET.fromstring(self.camera.handle_soap(soap_request("GetProfiles")))
        values = {onvif_sim.local_name(element): element.text for element in response.iter() if element.text}
        self.assertEqual(values["Width"], "1920")
        self.assertEqual(values["Height"], "1080")
        self.assertEqual(values["FrameRateLimit"], "30")

    def test_stream_uri_can_be_discovered_from_media_service(self) -> None:
        response = ET.fromstring(self.camera.handle_soap(soap_request("GetStreamUri")))
        self.assertEqual(onvif_sim.child_text(response, "Uri"), self.camera.stream_uri)

    def test_video_publisher_uses_configured_resolution_and_frame_rate(self) -> None:
        command = onvif_sim.build_video_command("/usr/bin/ffmpeg", self.camera)
        source = command[command.index("-i") + 1]
        self.assertEqual(source, "testsrc2=size=1920x1080:rate=30")
        self.assertEqual(command[-1], self.camera.publish_uri)

    def test_discovery_probe_response_has_endpoint_and_device_type(self) -> None:
        request = ET.fromstring(
            f'<s:Envelope xmlns:s="{onvif_sim.SOAP_NS}" xmlns:a="{onvif_sim.ADDRESSING_NS}">'
            f"<s:Header><a:MessageID>urn:uuid:request-1</a:MessageID></s:Header>"
            f'<s:Body><d:Probe xmlns:d="{onvif_sim.DISCOVERY_NS}"/></s:Body></s:Envelope>'
        )
        response = ET.fromstring(self.camera.discovery_response(request, "Probe"))
        self.assertEqual(onvif_sim.child_text(response, "Address"), self.camera.endpoint)
        self.assertEqual(onvif_sim.child_text(response, "XAddrs"), self.camera.device_url)
        self.assertEqual(onvif_sim.child_text(response, "RelatesTo"), "urn:uuid:request-1")
        self.assertEqual(onvif_sim.child_text(response, "To"), f"{onvif_sim.ADDRESSING_NS}/role/anonymous")

    def test_http_media_endpoint_serves_soap(self) -> None:
        handler = type("TestHandler", (onvif_sim.SOAPHandler,), {"camera": self.camera})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/onvif/media_service",
                data=soap_request("GetProfiles"),
                headers={"Content-Type": "application/soap+xml"},
            )
            with urllib.request.urlopen(request) as response:
                body = ET.fromstring(response.read())
            self.assertIn("Profiles", [onvif_sim.local_name(element) for element in body.iter()])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
