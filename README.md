# IoT 协议设备模拟器

用于本地模拟 IoT 设备及其协议数据，方便网关开发、联调和测试。通过配置文件启用多个协议插件，周期性生成测点数据。

## 功能

- Modbus TCP 从站：网关作为主站轮询保持寄存器。
- TCP / UDP 服务端：接收网关请求并返回 JSON 数据。
- MQTT 发布者：向指定 topic 发布模拟数据。
- OPC UA 服务器：提供模拟测点节点。
- Prometheus exporter：在 `/metrics` 暴露模拟指标。
- 可配置设备 ID、采样周期、测点范围、精度和随机种子。

## 环境要求

- Python 3.10 或更高版本
- 对应协议服务的可用端口；使用 MQTT 插件时还需 MQTT Broker

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
demo/                      其他协议设备演示程序
tests/                     自动化测试
```
