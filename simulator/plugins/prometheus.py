"""Prometheus exporter 模拟器：暴露 /metrics，供网关 prometheus 驱动拉取。

网关端点配置示例：
`connection: {"url": "http://127.0.0.1:9100/metrics"}`，
`protocol_identifier: {"metric": "line_temperature", "labels": {"line": "A"}}`。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .base import BaseSimulator


def render_metrics(
    values: dict[str, float], labels: dict[str, str], metric_types: dict[str, str] | None = None
) -> str:
    """按 Prometheus 文本格式渲染指标，标签值做必要转义。"""
    metric_types = metric_types or {}
    label_text = ""
    if labels:
        parts = [
            f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())
        ]
        label_text = "{" + ",".join(parts) + "}"
    lines: list[str] = []
    for name, value in values.items():
        metric_type = metric_types.get(name, "gauge")
        lines.append(f"# TYPE {name} {metric_type}")
        lines.append(f"# HELP {name} anythings simulator metric")
        lines.append(f"{name}{label_text} {value}")
    return "\n".join(lines) + "\n"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        simulator: PrometheusSimulator = self.server.simulator  # type: ignore[attr-defined]
        if self.path.split("?", 1)[0] != simulator.metrics_path:
            self.send_response(404)
            self.end_headers()
            return
        body = simulator.render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """静默访问日志，避免刷屏。"""


class PrometheusSimulator(BaseSimulator):
    def __init__(self, name: str, config: dict[str, Any], reporter: Any | None = None):
        super().__init__(name, config, reporter)
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}

    @property
    def metrics_path(self) -> str:
        path = str(self.config.get("path", "/metrics"))
        return path if path.startswith("/") else f"/{path}"

    @property
    def labels(self) -> dict[str, str]:
        raw = self.config.get("labels") or {}
        return {str(key): str(value) for key, value in raw.items()}

    @property
    def metric_types(self) -> dict[str, str]:
        return {
            str(spec["name"]): str(spec.get("type", "gauge"))
            for spec in self.value_specs()
        }

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
        if not values:
            values = {spec["name"]: 0.0 for spec in self.value_specs()}
        return render_metrics(values, self.labels, self.metric_types)

    def step(self) -> None:
        values = self.generate_values()
        with self._lock:
            self._values = values
        payload = self.payload()
        payload["metrics"] = values
        self.record(payload)

    def _run_guarded_setup(self) -> None:
        host = str(self.config.get("host", "127.0.0.1"))
        port = int(self.config.get("port", 9100))
        server = ThreadingHTTPServer((host, port), _MetricsHandler)
        server.simulator = self  # type: ignore[attr-defined]
        self._server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.threads.append(thread)
        print(f"[prometheus] exporter 监听 {host}:{port}{self.metrics_path}")

    def _teardown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
