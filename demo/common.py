"""控制中心 API 客户端、日志与设备/告警装配工具。"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import urlparse

import config as config


def log(message: str, level: str = "INFO", step: str | None = None) -> None:
    stamp = time.strftime("%H:%M:%S")
    prefix = f"[{stamp}] [{level}]"
    if step:
        prefix = f"{prefix} [STEP {step}]"
    stream = sys.stderr if level in {"ERROR", "WARN"} else sys.stdout
    print(f"{prefix} {message}", file=stream, flush=True)


def section(title: str) -> None:
    log("------------------------------------------------------------")
    log(title)


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, code: str, message: str):
        super().__init__(f"{method} {url} 失败 HTTP {status} [{code}]: {message}")
        self.method = method
        self.url = url
        self.status = status
        self.code = code
        self.message = message


def _decode_token_spaces(token: str) -> list[str]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        spaces = claims.get("space_ids") or []
        return [str(item) for item in spaces if str(item).strip()]
    except (IndexError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return []


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


class CCApi:
    def __init__(
        self,
        base_url: str = config.API_DEFAULT,
        token: str | None = None,
        space: str | None = None,
        username: str = config.DEFAULT_USERNAME,
        password: str = config.DEFAULT_PASSWORD,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("ANYTHINGS_CC_TOKEN") or ""
        self.space = space or os.environ.get("ANYTHINGS_CC_SPACE") or ""
        self.username = username
        self.password = password
        self.timeout = timeout
        self.user: dict[str, Any] = {}

    def login(self) -> None:
        data = self._request(
            "POST", "/api/v1/auth/login",
            body={"username": self.username, "password": self.password},
            auth=False,
        )
        token = str(data.get("token") or "")
        if not token:
            raise ApiError("POST", f"{self.base_url}/api/v1/auth/login", 200, "CC_SCHEMA_INVALID", "登录响应缺少 token")
        self.token = token
        self.user = data.get("user") or {}
        log(f"使用账号 {self.username} 登录成功")

    def ensure_token(self) -> None:
        if not self.token:
            self.login()
        elif not self.user:
            try:
                self.user = self._request("GET", "/api/v1/auth/me", space=False) or {}
            except ApiError as exc:
                if exc.status in {401, 403}:
                    log(f"提供的 token 校验失败：{exc.message}", "WARN")
                    self.login()
                else:
                    raise
        if self.user:
            log(f"当前用户 {self.user.get('username', self.username)}，token 长度 {len(self.token)}")
        else:
            log(f"使用提供的 token，长度 {len(self.token)}")

    def resolve_space(self) -> str:
        if self.space:
            log(f"使用指定空间 {self.space}")
            return self.space
        for source, spaces in (
            ("token", _decode_token_spaces(self.token)),
            ("用户", [str(item) for item in (self.user.get("space_ids") or [])]),
        ):
            if spaces:
                self.space = spaces[0]
                log(f"从{source}解析空间 {self.space}")
                return self.space
        self.space = config.SPACE_DEFAULT
        log(f"未解析到空间，使用默认空间 {self.space}")
        return self.space

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        auth: bool = True,
        space: bool | None = None,
    ) -> Any:
        url = path if path.startswith("http") else _join_url(self.base_url, path)
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        use_space = bool(self.space) if space is None else space
        if use_space and self.space:
            headers["X-Space-Id"] = self.space
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            code, message = _error_detail(raw)
            raise ApiError(method, url, status, code, message) from exc
        except urllib.error.URLError as exc:
            raise ApiError(method, url, 0, "CC_UNREACHABLE", f"无法连接控制中心: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(method, url, 0, "CC_TIMEOUT", "控制中心请求超时") from exc

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ApiError(method, url, status, "CC_BAD_RESPONSE", f"响应不是合法 JSON: {raw[:200]}") from exc
        if isinstance(payload, dict) and payload.get("code") not in (None, "0"):
            raise ApiError(method, url, status, str(payload.get("code")), str(payload.get("message", "")))
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, body or {})

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def get_or_none(self, path: str) -> Any | None:
        try:
            return self.get(path)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def list_devices(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get(f"/api/v1/devices?page={page}&page_size=200") or {}
            batch = data.get("items") or []
            items.extend(batch)
            total = int(data.get("total") or 0)
            if not batch or len(items) >= total:
                return items
            page += 1

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        return self.get_or_none(f"/api/v1/devices/{urllib.parse.quote(device_id)}")

    def list_org_nodes(self) -> list[dict[str, Any]]:
        return self.get("/api/v1/org-nodes") or []

    def list_alarm_rules(self, device_id: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"device_id": device_id})
        return self.get(f"/api/v1/alarm-rules?{query}") or []


def _error_detail(raw: str) -> tuple[str, str]:
    try:
        payload = json.loads(raw)
        return str(payload.get("code", "CC_ERROR")), str(payload.get("message", raw))
    except json.JSONDecodeError:
        return "CC_ERROR", raw[:300] or "无响应内容"


def ensure_device_tree(api: CCApi) -> str:
    section("检查设备树")
    nodes = api.list_org_nodes()
    if not nodes:
        log("未发现设备树，创建默认测试设备树", "WARN")
        for node in config.ORG_TREE:
            api.post("/api/v1/org-nodes", dict(node))
            log(f"已创建设备树节点 {node['node_id']}（{node['node_type']}）")
        return config.ORG_TREE[-1]["node_id"]

    log(f"发现 {len(nodes)} 个设备树节点")
    line = next((node for node in nodes if node.get("node_type") == "line"), None)
    if line:
        log(f"复用产线节点 {line.get('node_id')}（{line.get('name')}）")
        return str(line["node_id"])

    parent_id = ""
    parent = next(
        (node for node in nodes if node.get("node_type") in {"factory", "workshop"}),
        None,
    )
    if parent:
        parent_id = str(parent.get("node_id") or "")
    line_node = {
        "node_id": "TEST-LINE",
        "node_type": "line",
        "parent_id": parent_id,
        "name": "测试产线",
    }
    api.post("/api/v1/org-nodes", line_node)
    log(f"设备树缺少产线节点，已创建 {line_node['node_id']}")
    return line_node["node_id"]


def add_or_update_device(
    api: CCApi,
    spec: config.DeviceSpec,
    org_id: str,
) -> dict[str, Any]:
    section(f"添加设备 {spec.device_id}（{config.protocol_label(spec.protocol)}）")
    payload = config.device_payload(spec, org_id)
    existing = api.get_device(spec.device_id)
    if existing and not config.is_test_device(existing):
        raise ApiError(
            "POST", f"{api.base_url}/api/v1/devices", 409, "CC_CONFLICT",
            f"设备 ID {spec.device_id} 已被非测试设备占用，拒绝覆盖",
        )
    if existing:
        log(f"设备已存在，更新配置（端点 {len(payload['endpoints'])} 个）")
        device = api.put(f"/api/v1/devices/{urllib.parse.quote(spec.device_id)}", payload)
    else:
        log(f"设备不存在，创建设备（端点 {len(payload['endpoints'])} 个）")
        device = api.post("/api/v1/devices", payload)
    log(f"设备配置已写入：{device.get('name', spec.name)}，组织节点 {device.get('org_id', org_id)}")
    for endpoint in payload["endpoints"]:
        log(f"  端点 {endpoint['attribute_key']} -> {endpoint['endpoint_id']} ({endpoint['unit']})")
    return device


def sync_alarms(api: CCApi, spec: config.DeviceSpec) -> int:
    section(f"同步告警规则 {spec.device_id}")
    existing = api.list_alarm_rules(spec.device_id)
    for rule in existing:
        api.delete(f"/api/v1/alarm-rules/{urllib.parse.quote(str(rule['rule_id']))}")
    if existing:
        log(f"已清理旧告警规则 {len(existing)} 条")
    for alarm in spec.alarms:
        api.post("/api/v1/alarm-rules", config.alarm_payload(spec, alarm))
        log(
            f"已添加告警 {alarm.name}：{alarm.attribute_key} {alarm.operator} "
            f"{alarm.threshold:g}（{alarm.level}）"
        )
    log(f"告警规则同步完成，共 {len(spec.alarms)} 条")
    return len(spec.alarms)


def publish_station_config(api: CCApi, station_id: str = config.STATION_ID) -> dict[str, Any]:
    """发布站配置；控制中心会按端点台账自动生成各网关配置。"""
    section(f"发布站配置 {station_id}")
    data = api.post(f"/api/v1/stations/{urllib.parse.quote(station_id)}/config:publish", {}) or {}
    version = data.get("version")
    snapshot = data.get("snapshot") or {}
    gateways = []
    try:
        payload = base64.b64decode(snapshot.get("payload_b64") or "").decode("utf-8")
        gateways = json.loads(payload).get("gateways") or []
    except (ValueError, json.JSONDecodeError):
        pass
    log(f"站配置已发布：version={version}，网关 {len(gateways)} 个")
    for gateway in gateways:
        log(f"  网关 {gateway.get('gateway_id')} -> version {gateway.get('version')}")
    return data


def delete_device(api: CCApi, device: dict[str, Any]) -> None:
    device_id = str(device.get("device_id") or "")
    if not device_id:
        return
    rules = api.list_alarm_rules(device_id)
    for rule in rules:
        api.delete(f"/api/v1/alarm-rules/{urllib.parse.quote(str(rule['rule_id']))}")
    if rules:
        log(f"已删除告警规则 {len(rules)} 条")
    api.delete(f"/api/v1/devices/{urllib.parse.quote(device_id)}")
    log(f"已删除设备 {device_id}（端点随设备一并删除）")


def delete_dashboard_project(api: CCApi, project_id: str) -> bool:
    existing = api.get_or_none(f"/api/v1/dashboard-projects/{urllib.parse.quote(project_id)}")
    if not existing:
        log("测试看板项目不存在，跳过")
        return False
    api.delete(f"/api/v1/dashboard-projects/{urllib.parse.quote(project_id)}")
    log(f"已删除测试看板项目 {project_id}")
    return True


def ensure_api_reachable(api: CCApi) -> None:
    parsed = urlparse(api.base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ApiError("GET", api.base_url, 0, "CC_INVALID_URL", "API 地址必须包含 http(s)://host:port")
    log(f"控制中心 API: {api.base_url}")
    try:
        with urllib.request.urlopen(_join_url(api.base_url, "/healthz"), timeout=3) as response:
            status = response.status
        if status == 200:
            log("控制中心健康检查通过")
        else:
            log(f"控制中心健康检查返回 HTTP {status}，继续尝试业务接口", "WARN")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        log(f"健康检查不可用（{exc}），继续尝试业务接口", "WARN")
