#!/usr/bin/env python3
"""Compose a browser recording with a Loom face recording as picture in picture.

The script deliberately shells out to FFmpeg instead of importing a video library.
That keeps installation small and leaves the actual encoding work to FFmpeg.
"""

from __future__ import annotations

import argparse
import csv
import re
import json
import math
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from record_website import StatusCsvError, project_relative_path, update_fields


class ComposeError(RuntimeError):
    """An expected, user actionable composition error."""


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float | None


POSITIONS = {
    "top-left": ("margin", "margin"),
    "top-right": ("W-w-margin", "margin"),
    "bottom-left": ("margin", "H-h-margin"),
    "bottom-right": ("W-w-margin", "H-h-margin"),
    "center-left": ("margin", "(H-h)/2"),
    "left-center": ("margin", "(H-h)/2"),
    "center": ("(W-w)/2", "(H-h)/2"),
}

COMPOSABLE_STATUSES = frozenset(
    {"recorded_website_headed", "recorded_website", "recorded_linkedin"}
)
MISSING_BACKGROUND_STATUS = "error_combined_video_background_missing"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Overlay a Loom face recording on a browser recording."
    )
    parser.add_argument("background", type=Path, nargs="?", help="Full screen browser recording")
    parser.add_argument("overlay", type=Path, nargs="?", help="Loom face recording")
    parser.add_argument("output", type=Path, nargs="?", help="New output video path")
    parser.add_argument(
        "--csv-path",
        type=Path,
        help="Compose every pending row with source_video and loom_video values",
    )
    parser.add_argument(
        "--workers", type=int, default=6, help="Concurrent compositions, default: 3"
    )
    parser.add_argument(
        "--position",
        default="bottom-left",
        help=(
            "Overlay position: top-left, top-right, bottom-left, bottom-right, center-left, "
            "center, or x,y. Keyframes override this setting."
        ),
    )
    parser.add_argument("--margin", type=int, default=32, help="Margin for named positions")
    parser.add_argument(
        "--overlay-width",
        type=int,
        help="Overlay width in pixels. Defaults to 30%% of the output width.",
    )
    parser.add_argument(
        "--overlay-height",
        type=int,
        help="Overlay height in pixels. If omitted, the source aspect ratio is kept.",
    )
    parser.add_argument(
        "--face-shape",
        choices=("circle", "rectangle"),
        default="circle",
        help="Loom face shape, default: circle",
    )
    parser.add_argument(
        "--circle-scale",
        type=float,
        default=0.5,
        help="Rendered circle size relative to the centered Loom crop, default: 0.5",
    )
    parser.add_argument("--output-width", type=int, help="Output width in pixels")
    parser.add_argument("--output-height", type=int, help="Output height in pixels")
    parser.add_argument("--fps", type=float, help="Output frame rate")
    parser.add_argument(
        "--audio-source",
        choices=("overlay", "background", "none"),
        default="overlay",
        help="Audio track to keep, default: overlay Loom audio",
    )
    parser.add_argument(
        "--no-loop-background",
        action="store_true",
        help="Do not repeat a short source video to match a longer Loom video",
    )
    parser.add_argument(
        "--keyframe",
        action="append",
        metavar="SECONDS,X,Y",
        help=(
            "Overlay location keyframe. Repeat for movement, for example "
            "--keyframe 0,40,40 --keyframe 3,800,500."
        ),
    )
    parser.add_argument(
        "--video-codec", default="libx264", help="FFmpeg video encoder, default: libx264"
    )
    parser.add_argument("--crf", type=int, default=20, help="H.264 quality value, default: 20")
    parser.add_argument("--preset", default="medium", help="FFmpeg encoder preset")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable")
    parser.add_argument(
        "--status-csv",
        type=Path,
        help="CSV to update after a successful composition; inferred from the browser video when omitted",
    )
    parser.add_argument(
        "--status-row",
        type=int,
        help="CSV data row to mark; inferred from a row_### browser filename when omitted",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the FFmpeg command without encoding"
    )
    return parser.parse_args(argv)


def infer_status_target(args: argparse.Namespace) -> tuple[Path | None, int | None]:
    csv_path = args.status_csv
    row_number = args.status_row
    if csv_path is None:
        candidate = args.background.parent.parent / "data.csv"
        if candidate.is_file():
            csv_path = candidate
    if row_number is None:
        match = re.match(r"row_(\d+)_", args.background.stem, re.IGNORECASE)
        if match:
            row_number = int(match.group(1))
    return csv_path, row_number


def project_root(csv_path: Path) -> Path:
    return csv_path.parent.parent if csv_path.parent.name.lower() == "data" else csv_path.parent


def resolve_csv_video(csv_path: Path, value: str, *, loom: bool = False) -> Path:
    raw = Path(value.strip())
    if raw.is_absolute():
        return raw
    root = project_root(csv_path)
    candidates = [root / raw, csv_path.parent / raw]
    if loom:
        candidates.append(csv_path.parent / "loom-videos" / raw)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def combined_filename(first_name: str, email: str) -> str:
    clean_first_name = re.sub(r"[^A-Za-z0-9]+", "_", first_name.strip()).strip("_")
    email_domain = email.strip().rsplit("@", 1)[-1].lower()
    domain_label = email_domain.split(".", 1)[0]
    domain_label = re.sub(r"[^a-z0-9-]+", "", domain_label).strip("-")
    if not clean_first_name or not domain_label:
        raise ComposeError("Cannot name combined video without first_name and email domain")
    return f"{clean_first_name} {domain_label}.mp4"


def default_combined_path(
    csv_path: Path,
    row_number: int,
    source_path: Path,
    row: dict[str, str] | None = None,
) -> Path:
    if row is not None:
        return csv_path.parent / "combined-videos" / combined_filename(
            row.get("first_name", ""), row.get("email", "")
        )
    stem = source_path.stem
    return csv_path.parent / "combined-videos" / f"{stem}.mp4"


def compose_csv(args: argparse.Namespace) -> int:
    csv_path = args.csv_path
    if not csv_path or not csv_path.is_file():
        raise ComposeError(f"CSV file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        required = {"source_video"}
        missing = sorted(required - set(fieldnames))
        if missing:
            raise ComposeError(f"CSV is missing required columns: {', '.join(missing)}")
        loom_column = next(
            (candidate for candidate in ("loom_video", "loom-video") if candidate in fieldnames),
            None,
        )
        if loom_column is None:
            raise ComposeError("CSV is missing required column: loom_video or loom-video")
        rows = list(reader)

    if args.workers <= 0:
        raise ComposeError("--workers must be greater than zero")

    jobs: list[tuple[int, dict[str, str]]] = []
    for row_number, row in enumerate(rows, start=1):
        status = (row.get("status") or "").strip()
        if status not in COMPOSABLE_STATUSES:
            continue
        source_value = (row.get("source_video") or "").strip()
        loom_value = (row.get(loom_column) or "").strip()
        if not source_value or not loom_value:
            continue
        jobs.append((row_number, row))

    total_jobs = len(jobs)

    def compose_row(
        job_number: int, row_number: int, row: dict[str, str]
    ) -> str | None:
        source_value = (row.get("source_video") or "").strip()
        loom_value = (row.get(loom_column) or "").strip()
        source_path = resolve_csv_video(csv_path, source_value)
        loom_path = resolve_csv_video(csv_path, loom_value, loom=True)
        combined_value = (row.get("combined_video") or "").strip()
        output_path = (
            resolve_csv_video(csv_path, combined_value)
            if combined_value
            else default_combined_path(csv_path, row_number, source_path, row)
        )
        row_args = argparse.Namespace(**vars(args))
        row_args.background = source_path
        row_args.overlay = loom_path
        row_args.output = output_path
        row_args.status_csv = csv_path
        row_args.status_row = row_number
        row_args.dry_run = False
        print(f"Composing {job_number}/{total_jobs} row {row_number}: {output_path.name}")
        try:
            compose(row_args)
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise ComposeError(f"Combined video was not created: {output_path}")
            update_fields(
                csv_path,
                row_number,
                {
                    "combined_video": project_relative_path(csv_path, output_path),
                    "status": "combined_video",
                },
            )
            print(f"Completed {job_number}/{total_jobs} row {row_number}: {output_path.name}")
            return None
        except (ComposeError, StatusCsvError) as exc:
            if isinstance(exc, ComposeError) and "Background file does not exist:" in str(exc):
                update_fields(
                    csv_path,
                    row_number,
                    {
                        "combined_video": "",
                        "status": MISSING_BACKGROUND_STATUS,
                    },
                )
                print(
                    f"Failed {job_number}/{total_jobs} row {row_number}, marked "
                    f"{MISSING_BACKGROUND_STATUS}: "
                    f"{source_path}"
                )
            print(f"Failed {job_number}/{total_jobs} row {row_number}: {exc}", file=sys.stderr)
            return f"row {row_number}: {exc}"

    processed = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(compose_row, job_number, row_number, row)
            for job_number, (row_number, row) in enumerate(jobs, start=1)
        ]
        for future in as_completed(futures):
            failure = future.result()
            if failure is None:
                processed += 1
            else:
                failures.append(failure)

    if processed == 0 and not failures:
        print("No pending rows have both source_video and loom_video values.")
    if failures:
        raise ComposeError("Batch composition failed:\n" + "\n".join(failures))
    print(f"Composed {processed} row(s).")
    return 0


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ComposeError(
            f"Could not find {name!r}. Install FFmpeg and make sure both ffmpeg and "
            "ffprobe are on PATH, or pass --ffmpeg and --ffprobe."
        )
    return executable


def run_checked(command: Sequence[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except FileNotFoundError as exc:
        raise ComposeError(f"Executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if len(detail) > 2_000:
            detail = detail[-2_000:]
        raise ComposeError(f"Command failed: {' '.join(command)}\n{detail}") from exc


def parse_fps(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", 1)
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError) as exc:
        raise ComposeError(f"Invalid frame rate reported by ffprobe: {value!r}") from exc
    return result if math.isfinite(result) and result > 0 else None


def probe_video(ffprobe: str, path: Path) -> VideoInfo:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    result = run_checked(command, capture_output=True)
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ComposeError(f"Could not read video dimensions from {path}") from exc
    if width <= 0 or height <= 0:
        raise ComposeError(f"Video has invalid dimensions: {path}")
    return VideoInfo(width=width, height=height, fps=parse_fps(stream.get("avg_frame_rate")))


def positive_int(value: int | None, label: str) -> int | None:
    if value is not None and value <= 0:
        raise ComposeError(f"{label} must be greater than zero")
    return value


def resolve_dimensions(
    args: argparse.Namespace,
    background: VideoInfo,
    overlay: VideoInfo,
) -> tuple[int, int, int, int]:
    output_width = args.output_width or background.width
    output_height = args.output_height or background.height
    overlay_width = args.overlay_width
    overlay_height = args.overlay_height
    for value, label in (
        (output_width, "Output width"),
        (output_height, "Output height"),
        (overlay_width, "Overlay width"),
        (overlay_height, "Overlay height"),
    ):
        positive_int(value, label)

    if args.face_shape == "circle":
        if overlay_width is not None or overlay_height is not None:
            raise ComposeError(
                "Circle faces preserve the Loom source size; omit overlay dimensions "
                "or use --face-shape rectangle"
            )
        diameter = max(1, round(min(overlay.width, overlay.height) * args.circle_scale))
        overlay_width = diameter
        overlay_height = diameter
    elif overlay_width is None and overlay_height is None:
        overlay_width = max(1, round(output_width * 0.30))
        overlay_height = max(1, round(overlay_width * overlay.height / overlay.width))
    elif overlay_width is None:
        overlay_width = max(1, round(overlay_height * overlay.width / overlay.height))
    elif overlay_height is None:
        overlay_height = max(1, round(overlay_width * overlay.height / overlay.width))

    if overlay_width > output_width or overlay_height > output_height:
        raise ComposeError("Overlay dimensions must fit inside the output dimensions")
    return output_width, output_height, overlay_width, overlay_height


def parse_position(position: str, margin: int) -> tuple[str, str]:
    if margin < 0:
        raise ComposeError("Margin cannot be negative")
    if position in POSITIONS:
        x, y = POSITIONS[position]
        return x.replace("margin", str(margin)), y.replace("margin", str(margin))
    try:
        x_text, y_text = position.split(",", 1)
        x, y = int(x_text.strip()), int(y_text.strip())
    except ValueError as exc:
        raise ComposeError(
            "Position must be a named position or explicit x,y coordinates, for example 40,40"
        ) from exc
    return str(x), str(y)


def parse_keyframes(values: Iterable[str] | None) -> list[tuple[float, float, float]]:
    if not values:
        return []
    keyframes: list[tuple[float, float, float]] = []
    for value in values:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 3:
            raise ComposeError(f"Invalid keyframe {value!r}; expected seconds,x,y")
        try:
            time, x, y = (float(part) for part in parts)
        except ValueError as exc:
            raise ComposeError(f"Invalid keyframe {value!r}; values must be numeric") from exc
        if time < 0:
            raise ComposeError("Keyframe time cannot be negative")
        keyframes.append((time, x, y))
    keyframes.sort(key=lambda item: item[0])
    if any(first[0] == second[0] for first, second in zip(keyframes, keyframes[1:])):
        raise ComposeError("Keyframe times must be unique")
    return keyframes


def keyframe_expression(keyframes: Sequence[tuple[float, float, float]], index: int) -> str:
    """Create a piecewise linear FFmpeg expression for x or y."""
    expression = str(keyframes[-1][index])
    for current, next_frame in reversed(list(zip(keyframes, keyframes[1:]))):
        time_a, value_a = current[0], current[index]
        time_b, value_b = next_frame[0], next_frame[index]
        if time_b == time_a:
            raise ComposeError("Keyframe times must be unique")
        interpolated = f"{value_a}+({value_b}-{value_a})*(t-{time_a})/({time_b}-{time_a})"
        expression = f"if(lt(t,{time_a}),{value_a},if(lt(t,{time_b}),{interpolated},{expression}))"
    return expression


def build_filter(
    output_width: int,
    output_height: int,
    overlay_width: int,
    overlay_height: int,
    position: tuple[str, str],
    keyframes: Sequence[tuple[float, float, float]],
    face_shape: str,
    source_face_size: int | None,
) -> str:
    x, y = position
    if keyframes:
        x = keyframe_expression(keyframes, 1)
        y = keyframe_expression(keyframes, 2)
    face_filter = f"scale={overlay_width}:{overlay_height}"
    if face_shape == "circle":
        face_filter = (
            f"crop={source_face_size}:{source_face_size}:(in_w-out_w)/2:(in_h-out_h)/2,"
            f"scale={overlay_width}:{overlay_height},"
            "format=rgba,"
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            "a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),"
            "(W/2)*(W/2)),255,0)'"
        )
    return (
        f"[0:v]scale={output_width}:{output_height}:force_original_aspect_ratio=increase,"
        f"crop={output_width}:{output_height}:(in_w-out_w)/2:(in_h-out_h)/2[bg];"
        f"[1:v]{face_filter}[face];"
        # FFmpeg treats commas as filter separators, so quote expressions that
        # contain if() calls. The quotes are part of the filter graph syntax.
        f"[bg][face]overlay=x='{x}':y='{y}':eof_action=repeat:shortest=1[v]"
    )


def compose(args: argparse.Namespace) -> list[str]:
    for path, label in ((args.background, "Background"), (args.overlay, "Overlay")):
        if not path.is_file():
            raise ComposeError(f"{label} file does not exist: {path}")
    if args.background.resolve() == args.overlay.resolve():
        raise ComposeError("Background and overlay must be different files")
    if args.output.resolve() in {args.background.resolve(), args.overlay.resolve()}:
        raise ComposeError("Output must be a new path and cannot overwrite an input video")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = require_executable(args.ffmpeg)
    ffprobe = require_executable(args.ffprobe)
    background = probe_video(ffprobe, args.background)
    overlay = probe_video(ffprobe, args.overlay)
    output_width, output_height, overlay_width, overlay_height = resolve_dimensions(
        args, background, overlay
    )
    keyframes = parse_keyframes(args.keyframe)
    position = parse_position(args.position, args.margin)
    if args.circle_scale <= 0:
        raise ComposeError("Circle scale must be greater than zero")
    source_face_size = min(overlay.width, overlay.height) if args.face_shape == "circle" else None
    video_filter = build_filter(
        output_width,
        output_height,
        overlay_width,
        overlay_height,
        position,
        keyframes,
        args.face_shape,
        source_face_size,
    )
    fps = args.fps or background.fps
    if fps is not None and fps <= 0:
        raise ComposeError("Frame rate must be greater than zero")
    if not 0 <= args.crf <= 51:
        raise ComposeError("CRF must be between 0 and 51")

    command = [ffmpeg, "-hide_banner", "-y"]
    if not args.no_loop_background:
        command.extend(["-stream_loop", "-1"])
    command.extend([
        "-i",
        str(args.background),
        "-i",
        str(args.overlay),
        "-filter_complex",
        video_filter,
        "-map",
        "[v]",
        "-c:v",
        args.video_codec,
        "-preset",
        args.preset,
        "-crf",
        str(args.crf),
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        "-movflags",
        "+faststart",
    ])
    if fps is not None:
        command.extend(["-r", f"{fps:.6f}"])
    if args.audio_source != "none":
        audio_input = "1:a?" if args.audio_source == "overlay" else "0:a?"
        command.extend(["-map", audio_input, "-c:a", "aac", "-b:a", "192k"])
    command.append(str(args.output))
    if not args.dry_run:
        run_checked(command, capture_output=True)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.csv_path is not None:
            if any(value is not None for value in (args.background, args.overlay, args.output)):
                raise ComposeError("Use either --csv-path or the three positional video paths, not both")
            return compose_csv(args)
        if args.background is None and args.overlay is None and args.output is None:
            args.csv_path = Path("data/data.csv")
            return compose_csv(args)
        if any(value is None for value in (args.background, args.overlay, args.output)):
            raise ComposeError(
                "Provide background, overlay, and output paths, or use --csv-path for CSV batch mode"
            )
        command = compose(args)
        print("FFmpeg command:")
        print(" ".join(command))
        if args.dry_run:
            print("Dry run complete; no video was written.")
        else:
            if not args.output.is_file() or args.output.stat().st_size == 0:
                raise ComposeError(f"Combined video was not created: {args.output}")
            csv_path, row_number = infer_status_target(args)
            if csv_path is not None and row_number is not None:
                update_fields(
                    csv_path,
                    row_number,
                    {
                        "combined_video": project_relative_path(csv_path, args.output),
                        "status": "combined_video",
                    },
                )
                print(f"Updated {csv_path} row {row_number}: combined_video")
            print(f"Wrote {args.output}")
        return 0
    except (ComposeError, StatusCsvError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
