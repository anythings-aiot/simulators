# IoT 协议设备模拟器

用于本地模拟 IoT 设备及其协议数据，方便网关开发、联调和测试。通过配置文件启用多个协议插件，周期性生成测点数据。

## 功能

- Modbus TCP 从站：网关作为主站轮询保持寄存器。
- TCP / UDP 服务端：接收网关请求并返回 JSON 数据。
- MQTT 发布者：向指定 topic 发布模拟数据。
- OPC UA 服务器：提供模拟测点节点。
- Prometheus exporter：在 `/metrics` 暴露模拟指标。
- 可配置设备 ID、采样周期、测点范围、精度和随机种子。
- 摄像头协议模拟器：GB28181 设备与信令平台、ONVIF 摄像头、RTMP 推流的 IP 摄像头。

## 环境要求

- Python 3.10 或更高版本
- 对应协议服务的可用端口；使用 MQTT 插件时还需 MQTT Broker
- 运行摄像头模拟器时需要 FFmpeg（`ffmpeg -version` 可检查，也可用 `VZ_FFMPEG_PATH` 指定路径）和 Pillow；ONVIF / GB28181 平台还依赖可用的 RTSP 媒体服务（默认 ZLMediaKit），IP 摄像头依赖 RTMP 服务（默认 SRS）

## 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

默认读取项目根目录的 `config.yaml`。也可以指定配置文件：

```bash
python main.py --config /path/to/config.yaml
```

运行时按 `Ctrl+C` 停止所有模拟器。

## 配置

编辑 `config.yaml` 中的 `plugins` 配置。将插件的 `enabled` 设为 `true` 启用，`false` 则跳过。默认启用 `modbus-tcp`、`tcp`、`udp` 和 `prometheus`；`mqtt` 与 `opc-ua` 默认关闭。

各插件的常用默认监听地址：

| 插件 | 地址 | 说明 |
| --- | --- | --- |
| `modbus-tcp` | `127.0.0.1:1502` | Modbus TCP 从站 |
| `tcp` | `127.0.0.1:9000` | TCP 请求-应答服务 |
| `udp` | `127.0.0.1:9001` | UDP 请求-应答服务 |
| `mqtt` | `127.0.0.1:1883` | 发布到配置的 topic，需 Broker |
| `opc-ua` | `0.0.0.0:4840` | OPC UA 服务器 |
| `prometheus` | `127.0.0.1:9100/metrics` | Prometheus metrics 接口 |

`values` 定义测点，例如：

```yaml
values:
  - name: temperature
    min: 18
    max: 32
    decimals: 1
```

`interval_ms` 设置采样周期（毫秒），`device_id` 设置设备标识，`seed`（可选）设置随机种子以复现生成结果。Modbus 插件还可用 `registers` 配置测点到保持寄存器地址的映射；`scale` 用于将测点值转换为寄存器整数值。

如需连接网关，请确保网关的协议参数、地址、端口、topic 或 metrics URL 与此处配置一致。监听地址 `127.0.0.1` 仅允许本机访问；跨主机联调时按网络环境调整 `host`，并确认防火墙放行对应端口。

## 摄像头协议模拟器

`cameras/` 目录按协议提供独立的单文件脚本，不依赖 `config.yaml`，直接用命令行参数配置，可通过 `--help` 查看全部选项。所有脚本均按 `Ctrl+C` 停止（同时会结束其拉起的 FFmpeg 子进程）。

默认协议与端口：

| 模拟器 | 协议 / 默认地址 | 说明 |
| --- | --- | --- |
| `gb28181/gb28181_sim.py` | SIP `5062/UDP`，RTP `30000` | GB28181 设备，向平台注册并推流 |
| `gb28181/gb_platform.py` | SIP `5060/UDP`，HTTP `8025` | 本地联调用的 GB28181 信令平台 |
| `onvif/onvif_sim.py` | WS-Discovery `3702/UDP`，HTTP `8080`，RTSP `8554` | ONVIF 摄像头，发布 RTSP 测试流 |
| `ipcam/ipcam_sim.py` | RTMP `127.0.0.1:1935` | IP 摄像头，向 SRS 推送 RTMP 流 |

### GB28181 设备模拟器

`cameras/gb28181/gb28181_sim.py` 模拟一台国标摄像头：

- 向平台发起 REGISTER（Digest MD5 鉴权，支持 `qop=auth`），按 `--expires` 周期续注册，并按 `--keepalive` 发送 Keepalive；心跳无响应或被拒绝时自动重新注册。
- 响应平台的 Catalog 目录查询、INVITE / BYE，支持 UDP 与 TCP 被动（`a=setup:passive`）两种 RTP 收流方式，并按 SDP 的 `y=` 复用 SSRC。
- 接收 DeviceControl 中的 `PTZCmd`（`A5 0F 01` 格式），解析方向、速度与变倍，实时反映到画面位置和缩放。
- 用 Pillow 绘制“Vizbox + 毫秒时间戳”画面，经 FFmpeg 编码 H.264 后封装为 PS/RTP 发送，关键帧重复发送系统头与 PSM，便于 SRS / ZLMediaKit 等平台识别。

```bash
python cameras/gb28181/gb28181_sim.py \
  --platform-host 127.0.0.1 --platform-port 5060 \
  --device-id 34020000001320000001 --channel-id 34020000001320000001 \
  --password 12345678
```

常用参数：`--local-ip` 指定设备对外通告的 IP（默认自动探测，跨主机联调时应显式设置）、`--fps` / `--width` / `--height` 设置视频规格（宽高必须为偶数）、`--password` 需与平台一致。

### GB28181 信令平台模拟器

`cameras/gb28181/gb_platform.py` 是与上面设备配套的开发态平台，不参与生产部署：

- SIP 侧负责设备注册鉴权、目录查询与同步、心跳保活、PTZ 下发、INVITE/ACK/BYE 拉流。
- 媒体侧通过 ZLMediaKit 的 `openRtpServer` 申请收流端口，把 SDP 回给设备，并对外返回 `rtsp://<zlm>/rtp/<stream_id>`。
- HTTP 侧供网关调用：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /healthz` | 健康检查 |
| `GET /api/v1/devices` | 已注册设备及其通道状态 |
| `POST /api/v1/ptz` | 下发云台命令，字段 `device_id`、`channel_id`、`command`（`stop`/`left`/`right`/`up`/`down`/`up_left`/`up_right`/`down_left`/`down_right`/`zoom_in`/`zoom_out`）、`speed`、`zoom_speed` |
| `POST /api/v1/streams` | 请求拉流，字段 `device_id`、`channel_id`，返回 `stream_id`、`stream_uri`、`port` |
| `DELETE /api/v1/streams/{stream_id}` | 释放媒体流（向设备发 BYE） |

```bash
python cameras/gb28181/gb_platform.py \
  --host-ip 127.0.0.1 --media-ip 127.0.0.1 \
  --zlm-api http://127.0.0.1:8090 --zlm-secret vs-dev-zlm-secret
```

平台默认以 TCP 被动方式收流（`--media-transport tcp`），可绕开 UDP 端口映射问题；需保证 ZLMediaKit API 可达且 `--zlm-secret` 正确。`--host-ip` / `--media-ip` 是 SDP 中通告给设备的 IP，跨主机时应改为设备可访问的地址。

### ONVIF 摄像头模拟器

`cameras/onvif/onvif_sim.py` 提供 WS-Discovery 与 Device / Media / PTZ SOAP 服务（HTTP 8080），`GetStreamUri` 返回 RTSP 地址；默认用 FFmpeg 的 `testsrc2` 生成 H.264 测试画面并推送到本机 ZLMediaKit 的 `rtsp://127.0.0.1:8554/live/onvif/<UUID>`。

```bash
python cameras/onvif/onvif_sim.py --resolution 1920x1080 --fps 30
```

- `--advertise-host`：Discovery 与 SOAP 响应中公布的本机 IPv4，跨主机或容器场景必须显式指定。
- `--no-video`：只启动发现与控制面，不生成视频。
- `--stream-uri`：`GetStreamUri` 直接返回既有外部流地址，不启动内置 FFmpeg 发布器。
- 本模拟器不实现 ONVIF 用户认证，客户端无需填写用户名密码。

ONVIF 逻辑的单元测试位于 `cameras/onvif/test_onvif_sim.py`：

```bash
python -m pytest cameras/onvif
```

### IP 摄像头模拟器

`cameras/ipcam/ipcam_sim.py` 绘制“毫秒时间戳 + 摄像头编号”，编码为 H.264 后以 RTMP 推送到 SRS，默认地址为 `rtmp://127.0.0.1:1935/live/cam<编号>`，用于多路摄像头接入测试。

```bash
python cameras/ipcam/ipcam_sim.py --number 1 --fps 25 --rtmp-url rtmp://127.0.0.1:1935/live/cam1
```

使用 `--video /path/to/video.mp4` 可改为循环推送本地视频文件（`--no-loop` 则播放一次后退出）；`--duration` 设置运行秒数，`0` 表示持续运行。

## 测试

```bash
pytest
```

## 项目结构

```text
main.py                    主程序入口
config.yaml                插件与测点配置
simulator/                 插件注册、配置及公共类型
simulator/plugins/         Modbus、MQTT、TCP/UDP、OPC UA、Prometheus 插件
cameras/gb28181/           GB28181 设备与信令平台模拟器
cameras/onvif/             ONVIF 摄像头模拟器及其测试
cameras/ipcam/             RTMP 推流的 IP 摄像头模拟器
demo/                      其他协议设备演示程序
tests/                     自动化测试
```
