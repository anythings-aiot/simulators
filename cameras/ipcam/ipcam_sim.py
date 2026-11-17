#!/usr/bin/env python3
"""IP 摄像头模拟器：绘制毫秒时间与编号，经 ffmpeg 推送到 SRS。"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STOP = False


def _handle_stop(_signum, _frame):
    global STOP
    STOP = True


def find_ffmpeg(explicit: str) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise SystemExit(f"指定的 ffmpeg 不可执行：{explicit}")

    for name in ("ffmpeg",):
        found = shutil.which(name)
        if found:
            return found

    env_bin = os.environ.get("VZ_FFMPEG_PATH", "")
    if env_bin and Path(env_bin).is_file():
        return env_bin

    try:
        from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise as get

        return get()[0]
    except Exception:
        pass
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise SystemExit("未找到 ffmpeg，请安装 ffmpeg 或执行 pip install static-ffmpeg")


def load_font(size: int):
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_frame(size: tuple[int, int], number: int, font) -> bytes:
    image = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(image)
    text = f"{int(time.time() * 1000)} #{number}"
    box = draw.textbbox((0, 0), text, font=font)
    x = (size[0] - (box[2] - box[0])) // 2 - box[0]
    y = (size[1] - (box[3] - box[1])) // 2 - box[1]
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    return image.tobytes()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IP 摄像头模拟器")
    parser.add_argument("--number", type=int, default=1, help="摄像头编号（默认 1）")
    parser.add_argument("--fps", type=float, default=25.0, help="帧率（默认 25）")
    parser.add_argument("--width", type=int, default=1280, help="画面宽度（默认 1280）")
    parser.add_argument("--height", type=int, default=720, help="画面高度（默认 720）")
    parser.add_argument(
        "--rtmp-url",
        default="",
        help="推流地址，默认 rtmp://127.0.0.1:1935/live/cam<编号>",
    )
    parser.add_argument("--duration", type=float, default=0.0, help="运行秒数，0 表示持续运行")
    parser.add_argument("--video", default="", help="视频文件路径；设置后按文件循环推流")
    parser.add_argument("--loop", dest="loop", action="store_true", default=True, help="视频文件循环播放（默认开启）")
    parser.add_argument("--no-loop", dest="loop", action="store_false", help="视频文件播放结束后退出")
    parser.add_argument("--ffmpeg", default="", help="ffmpeg 可执行文件路径")
    parser.add_argument("--font-size", type=int, default=0, help="文字字号，默认自动计算")
    return parser.parse_args()


def run_video(args, ffmpeg: str, url: str) -> int:
    source = Path(args.video).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"视频文件不存在：{source}")
    width, height = args.width, args.height
    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    if args.loop:
        command += ["-stream_loop", "-1"]
    command += [
        "-re", "-i", str(source),
        "-vf", scale, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", str(max(2, int(args.fps * 2))),
        "-r", str(args.fps),
    ]
    if args.duration > 0:
        command += ["-t", str(args.duration)]
    command += ["-f", "flv", url]

    print(f"camera {args.number}: video {source} -> {url}", file=sys.stderr, flush=True)
    process = subprocess.Popen(command)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    try:
        while not STOP and process.poll() is None:
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    print(f"camera {args.number} stopped (code {process.returncode})", file=sys.stderr)
    return 0 if STOP or args.duration > 0 else (process.returncode or 0)


def main() -> int:
    args = parse_args()
    if args.fps <= 0:
        raise SystemExit("--fps 必须大于 0")
    if args.width <= 0 or args.height <= 0:
        raise SystemExit("分辨率必须大于 0")
    if args.width % 2 or args.height % 2:
        raise SystemExit("H.264 要求宽高为偶数")

    url = args.rtmp_url or f"rtmp://127.0.0.1:1935/live/cam{args.number}"
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if args.video:
        return run_video(args, ffmpeg, url)

    font_size = args.font_size or max(24, min(args.width, args.height) // 8)
    font = load_font(font_size)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{args.width}x{args.height}",
        "-r",
        str(args.fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(max(2, int(args.fps * 2))),
        "-f",
        "flv",
        url,
    ]

    print(
        f"camera {args.number}: {args.width}x{args.height}@{args.fps:g}fps -> {url}",
        file=sys.stderr,
        flush=True,
    )
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    interval = 1.0 / args.fps
    started = time.monotonic()
    deadline = started
    frames = 0
    try:
        while not STOP and process.poll() is None:
            now = time.monotonic()
            if args.duration > 0 and now - started >= args.duration:
                break
            if now < deadline:
                time.sleep(min(deadline - now, 0.05))
                continue
            frame = render_frame((args.width, args.height), args.number, font)
            try:
                process.stdin.write(frame)
            except (BrokenPipeError, ValueError):
                break
            frames += 1
            deadline += interval
            if now - deadline > interval:
                deadline = now
    finally:
        if process.stdin:
            try:
                process.stdin.close()
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    if process.returncode not in (0, None) and not STOP:
        detail = b""
        if process.stderr:
            detail = process.stderr.read()[-500:]
        print(f"ffmpeg 退出码 {process.returncode}：{detail.decode('utf-8', 'ignore').strip()}", file=sys.stderr)
        return 1
    if process.stderr:
        process.stderr.close()
    print(f"camera {args.number} stopped after {frames} frames", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
