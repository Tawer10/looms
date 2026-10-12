#!/usr/bin/env python3
"""Convert videos in a folder to optimized looping GIF files with FFmpeg."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence


VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data" / "combined-videos" / "Camp2-videos"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "combined-videos" / "Camp2-gifs"


class GifError(RuntimeError):
    """An expected, user actionable GIF conversion error."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert every video in an input folder to a looping GIF."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Folder containing videos, default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Folder for GIFs, default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--fps", type=int, default=10, help="GIF frame rate, default: 10"
    )
    parser.add_argument(
        "--width", type=int, default=640, help="GIF width in pixels, default: 640"
    )
    parser.add_argument(
        "--start", type=float, default=0, help="Start time in seconds, default: 0"
    )
    parser.add_argument(
        "--duration", type=float, help="Optional maximum duration in seconds"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of videos to convert concurrently, default: 6",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace GIFs that already exist; otherwise existing files are skipped",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without converting"
    )
    return parser.parse_args(argv)


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise GifError(
            f"Could not find {name!r}. Install FFmpeg and make sure ffmpeg is on PATH, "
            "or pass --ffmpeg with the executable path."
        )
    return executable


def run_checked(command: Sequence[str], *, dry_run: bool = False) -> None:
    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    if dry_run:
        return
    try:
        result = subprocess.run(
            list(command), check=True, text=True, capture_output=True
        )
    except FileNotFoundError as exc:
        raise GifError(f"Executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if len(detail) > 2_000:
            detail = detail[-2_000:]
        raise GifError(f"FFmpeg failed for {command[-1]}\n{detail}") from exc


def validate_args(args: argparse.Namespace) -> None:
    if args.fps <= 0:
        raise GifError("--fps must be greater than zero")
    if args.width <= 0:
        raise GifError("--width must be greater than zero")
    if args.start < 0:
        raise GifError("--start cannot be negative")
    if args.duration is not None and args.duration <= 0:
        raise GifError("--duration must be greater than zero")
    if args.workers <= 0:
        raise GifError("--workers must be greater than zero")
    if not args.input_dir.is_dir():
        raise GifError(f"Input folder does not exist: {args.input_dir}")


def conversion_command(
    ffmpeg: str,
    source: Path,
    output: Path,
    *,
    fps: int,
    width: int,
    start: float,
    duration: float | None,
    overwrite: bool,
) -> list[str]:
    filters = (
        f"fps={fps},scale={width}:-1:flags=lanczos,"
        "split[s0][s1];[s0]palettegen=max_colors=256:stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=sierra2_4a"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    command.append("-y" if overwrite else "-n")
    if start:
        command.extend(["-ss", str(start)])
    command.extend(["-i", str(source)])
    if duration is not None:
        command.extend(["-t", str(duration)])
    command.extend(["-vf", filters, "-loop", "0", "-an", str(output)])
    return command


def convert_one(
    source: Path,
    args: argparse.Namespace,
    ffmpeg: str,
) -> str:
    output = args.output_dir / f"{source.stem}.gif"
    if output.exists() and not args.overwrite:
        print(f"Skipping existing GIF: {output}")
        return "skipped"

    command = conversion_command(
        ffmpeg,
        source,
        output,
        fps=args.fps,
        width=args.width,
        start=args.start,
        duration=args.duration,
        overwrite=args.overwrite,
    )
    run_checked(command, dry_run=args.dry_run)
    return "converted"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_args(args)
    ffmpeg = require_executable(args.ffmpeg)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        path
        for path in args.input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not sources:
        print(f"No supported videos found in {args.input_dir}")
        return 0

    converted = 0
    skipped = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(convert_one, source, args, ffmpeg): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except GifError as exc:
                failures.append(f"{source.name}: {exc}")
            else:
                if result == "converted":
                    converted += 1
                else:
                    skipped += 1

    if failures:
        raise GifError("GIF conversion failed:\n" + "\n".join(failures))

    action = "would convert" if args.dry_run else "converted"
    print(
        f"{action.capitalize()} {converted} video(s) with {args.workers} worker(s); "
        f"skipped {skipped} existing GIF(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GifError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
