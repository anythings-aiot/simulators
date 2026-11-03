"""入口：加载 config.yaml，启动所有 enabled 的模拟器插件，Ctrl+C 退出。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from simulator import registry
from simulator.config import load_config
from simulator.types import Reporter


def main() -> int:
    parser = argparse.ArgumentParser(description="anythings 协议数据模拟器")
    parser.add_argument(
        "-c",
        "--config",
        default=str(Path(__file__).resolve().parent / "config.yaml"),
        help="配置文件路径，默认执行目录下的 config.yaml",
    )
    args = parser.parse_args()

    data = load_config(args.config)
    reporter = Reporter()
    simulators = []
    for name, cfg in data.get("plugins", {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        try:
            simulators.append(registry.create(name, cfg, reporter))
        except Exception as exc:  # noqa: BLE001 - 单插件失败不影响其它插件
            print(f"[main] 插件 {name} 创建失败: {exc}", file=sys.stderr)

    if not simulators:
        print("[main] 没有已启用的插件，请检查 config.yaml", file=sys.stderr)
        return 1

    for sim in simulators:
        try:
            sim.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[main] 插件 {sim.name} 启动失败: {exc}", file=sys.stderr)
    print(f"[main] 已启动 {len(simulators)} 个模拟器，Ctrl+C 退出")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[main] 正在停止…")
    finally:
        for sim in simulators:
            sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
