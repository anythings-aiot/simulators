#!/usr/bin/env python3
"""运行指定协议测试设备，持续输出线性变化且相互关联的模拟数据。"""
from __future__ import annotations

import argparse
import sys
import time

import config as config
from simulators import create_simulator
from common import log, section


def parse_protocol(value: str) -> str:
    if value.strip().lower() == "all":
        return "all"
    return config.normalize_protocol(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 MQTT 温控风扇 / CoAP 喷淋系统 / Prometheus 工控机模拟器",
    )
    parser.add_argument(
        "--protocol",
        required=True,
        nargs="+",
        type=parse_protocol,
        metavar="PROTOCOL",
        help="协议类型：mqtt、coap、prometheus（兼容 promethus），或 all",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听或连接主机，默认 127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=config.MQTT_PORT, help="MQTT Broker 端口")
    parser.add_argument("--coap-port", type=int, default=config.COAP_PORT, help="CoAP 服务端口")
    parser.add_argument(
        "--prometheus-port", type=int, default=config.PROMETHEUS_PORT,
        help="Prometheus exporter 端口",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="采样周期（秒），默认 1.0")
    parser.add_argument("--seed", type=int, default=None, help="随机种子，便于复现")
    parser.add_argument("--duration", type=float, default=0.0, help="运行时长（秒），0 表示常驻")
    return parser


def expand_protocols(values: list[str]) -> list[str]:
    if "all" in values:
        return ["mqtt", "coap", "prometheus"]
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def run(args: argparse.Namespace) -> int:
    protocols = expand_protocols(args.protocol)
    section("AnyThings 测试设备模拟器")
    log(f"协议: {', '.join(config.protocol_label(item) for item in protocols)}")
    log(f"采样周期: {args.interval:g}s" + (f"，运行 {args.duration:g}s" if args.duration > 0 else "，Ctrl+C 退出"))

    simulators = []
    for index, protocol in enumerate(protocols):
        seed = None if args.seed is None else args.seed + index
        simulators.append(create_simulator(
            protocol,
            host=args.host,
            mqtt_port=args.mqtt_port,
            coap_port=args.coap_port,
            prometheus_port=args.prometheus_port,
            interval=args.interval,
            seed=seed,
        ))

    started = []
    for simulator in simulators:
        try:
            simulator.start()
            started.append(simulator)
        except Exception as exc:  # noqa: BLE001 - 多协议时单个失败不影响其它
            log(f"{simulator.label} {simulator.name} 启动失败: {exc}", "ERROR")
    if not started:
        log("没有模拟器成功启动", "ERROR")
        return 1
    log(f"已启动 {len(started)} 个模拟器，开始输出数据")

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    next_tick = time.monotonic()
    errors = {id(simulator): 0 for simulator in started}
    try:
        while deadline is None or time.monotonic() < deadline:
            for simulator in started:
                try:
                    simulator.step()
                    errors[id(simulator)] = 0
                except Exception as exc:  # noqa: BLE001 - 单次采集失败不终止
                    errors[id(simulator)] += 1
                    log(f"{simulator.label} 单步执行失败: {exc}", "ERROR")
                    if errors[id(simulator)] >= 5:
                        raise RuntimeError("连续失败 5 次，停止模拟器")
            next_tick += max(0.1, args.interval)
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        log("收到停止信号", "WARN")
    except RuntimeError as exc:
        log(str(exc), "ERROR")
        return 1
    finally:
        for simulator in started:
            try:
                simulator.stop()
            except Exception as exc:  # noqa: BLE001
                log(f"{simulator.label} 停止失败: {exc}", "WARN")
        log("模拟器已停止")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except (ValueError, RuntimeError) as exc:
        log(str(exc), "ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
