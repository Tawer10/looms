#!/usr/bin/env python3
"""Record a LinkedIn source link in a native Microsoft Edge window.

The script opens a new Edge window with the user's normal Edge profile, so an
already connected account can be used without copying or handling credentials.
It records the visible native window, including Edge tabs and the address bar.
It does not automate login, messaging, connection requests, or other LinkedIn
actions.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from record_website import (
    RecorderError,
    StatusCsvError,
    error_status,
    find_native_window,
    maximize_window,
    project_relative_path,
    read_websites,
    safe_filename,
    start_window_capture,
    stop_window_capture,
    update_fields,
    visible_windows,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record LinkedIn links through a native Microsoft Edge window."
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        nargs="?",
        default=Path("data/data.csv"),
        help="CSV containing source links, default: data/data.csv",
    )
    parser.add_argument("--row", type=int, help="Record only this one based CSV data row")
    parser.add_argument(
        "--source-column",
        default="source_link",
        help="CSV column containing LinkedIn URLs, default: source_link",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/linkedin-videos"),
        help="Directory for recorded WebM files",
    )
    parser.add_argument("--duration", type=float, default=3, help="Seconds to record, default: 3")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=5,
        help="Minimum seconds to wait for the profile to render, default: 5",
    )
    parser.add_argument(
        "--page-wait-max",
        type=float,
        default=15,
        help="Maximum seconds to wait for the profile to render, default: 15",
    )
    parser.add_argument(
        "--crop-sides",
        type=int,
        default=8,
        help="Pixels to crop from each side, default: 8",
    )
    parser.add_argument(
        "--next-wait-min",
        type=float,
        default=10,
        help="Minimum pause before the next profile, default: 10 seconds",
    )
    parser.add_argument(
        "--next-wait-max",
        type=float,
        default=60,
        help="Maximum pause before the next profile, default: 60 seconds",
    )
    parser.add_argument(
        "--batch-size-min",
        type=int,
        default=30,
        help="Minimum profiles per batch, default: 30",
    )
    parser.add_argument(
        "--batch-size-max",
        type=int,
        default=50,
        help="Maximum profiles per batch, default: 50",
    )
    parser.add_argument(
        "--batch-wait-min",
        type=float,
        default=30 * 60,
        help="Minimum batch pause in seconds, default: 30 minutes",
    )
    parser.add_argument(
        "--batch-wait-max",
        type=float,
        default=2 * 60 * 60,
        help="Maximum batch pause in seconds, default: 2 hours",
    )
    parser.add_argument(
        "--short-batch-size-min",
        type=int,
        default=5,
        help="Minimum profiles per short batch, default: 5",
    )
    parser.add_argument(
        "--short-batch-size-max",
        type=int,
        default=10,
        help="Maximum profiles per short batch, default: 10",
    )
    parser.add_argument(
        "--short-batch-wait-min",
        type=float,
        default=2 * 60,
        help="Minimum short batch pause in seconds, default: 2 minutes",
    )
    parser.add_argument(
        "--short-batch-wait-max",
        type=float,
        default=10 * 60,
        help="Maximum short batch pause in seconds, default: 10 minutes",
    )
    parser.add_argument("--edge-executable", type=Path, help="Path to msedge.exe")
    parser.add_argument(
        "--profile-directory",
        help="Optional Edge profile directory, for example Default or Profile 1",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    return parser.parse_args(argv)


def find_edge_executable(explicit: Path | None) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise RecorderError(f"Edge executable does not exist: {explicit}")
    candidates = []
    on_path = shutil.which("msedge.exe") or shutil.which("msedge")
    if on_path:
        candidates.append(Path(on_path))
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RecorderError("Could not find Microsoft Edge. Pass --edge-executable with the path to msedge.exe")


def validate_link(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "linkedin.com",
        "www.linkedin.com",
    } and not parsed.netloc.lower().endswith(".linkedin.com"):
        raise RecorderError(f"Source link is not a LinkedIn URL: {url}")
    return url


def is_linkedin_candidate(url: str) -> bool:
    """Select LinkedIn-looking links, including malformed ones for tracking."""
    return "linkedin" in url.lower()


def open_edge(edge_executable: Path, url: str, profile_directory: str | None) -> None:
    command = [str(edge_executable), "--new-window", "--start-maximized", url]
    if profile_directory:
        command.append(f"--profile-directory={profile_directory}")
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RecorderError(f"Could not open Microsoft Edge: {exc}") from exc


def close_native_window(hwnd: int) -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    if not user32.PostMessageW(hwnd, 0x0010, 0, 0):
        raise RecorderError("Could not close the LinkedIn Edge window")
    time.sleep(1)


def crop_recording(ffmpeg: str, source: Path, output: Path, crop_sides: int) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"crop=iw-{crop_sides * 2}:ih:{crop_sides}:0",
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "32",
        "-b:v",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(output),
    ]
    try:
        subprocess.run(command, check=True, text=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RecorderError(f"Could not crop the LinkedIn recording: {detail.strip()}") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RecorderError("FFmpeg produced an empty cropped LinkedIn recording")


def existing_linkedin_window() -> tuple[int, str] | None:
    for hwnd, title, process_name in visible_windows():
        if process_name == "msedge.exe" and "linkedin" in title.lower():
            return hwnd, title
    return None


INVALID_PROFILE_TITLE_RE = re.compile(
    r"(?:page not found|profile not found|profile unavailable|\b404\b|\b403\b|"
    r"access denied|this page isn't available|this page is unavailable|"
    r"something went wrong|we couldn't find|couldn't find this page|"
    r"member not found|user not found)",
    re.IGNORECASE,
)


def current_window_title(hwnd: int, fallback: str) -> str:
    for candidate_hwnd, title, process_name in visible_windows():
        if candidate_hwnd == hwnd and process_name == "msedge.exe":
            return title or fallback
    return fallback


def validate_profile_window(hwnd: int, initial_title: str) -> None:
    title = current_window_title(hwnd, initial_title)
    if INVALID_PROFILE_TITLE_RE.search(title):
        raise RecorderError(f"Invalid LinkedIn profile is unavailable: {title}")


def record_one(
    url: str,
    output_path: Path,
    args: argparse.Namespace,
    edge_executable: Path,
) -> None:
    if os.name != "nt":
        raise RecorderError("Native Edge capture requires Windows")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_handles = {hwnd for hwnd, _title, _process in visible_windows()}
    open_edge(edge_executable, url, args.profile_directory)
    hwnd, title = find_native_window(
        "",
        existing_handles,
        {"msedge.exe"},
        "Microsoft Edge",
        timeout=20,
    )
    selection = "Opened native Edge window for the requested profile"
    safe_title = title.encode("ascii", "replace").decode("ascii")
    print(f"{selection}: {safe_title}")
    maximize_window(hwnd)
    page_wait = random.SystemRandom().uniform(args.wait_seconds, args.page_wait_max)
    print(f"Waiting {page_wait:.1f} seconds for the LinkedIn profile to render")
    time.sleep(page_wait)
    capture = None
    capture_path = output_path.with_name(f".{output_path.stem}.capture.webm")
    cropped_path = output_path.with_name(f".{output_path.stem}.cropped.webm")
    try:
        validate_profile_window(hwnd, title)
        capture = start_window_capture(args.ffmpeg, hwnd, capture_path)
        time.sleep(args.duration)
    finally:
        if capture is not None:
            stop_window_capture(capture)
        close_native_window(hwnd)
    try:
        if args.crop_sides > 0:
            crop_recording(args.ffmpeg, capture_path, cropped_path, args.crop_sides)
            cropped_path.replace(output_path)
        else:
            capture_path.replace(output_path)
    finally:
        capture_path.unlink(missing_ok=True)
        cropped_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.duration <= 0:
            raise RecorderError("--duration must be greater than zero")
        if args.wait_seconds < 0:
            raise RecorderError("--wait-seconds cannot be negative")
        if args.page_wait_max < args.wait_seconds:
            raise RecorderError("--page-wait-max cannot be less than --wait-seconds")
        if args.crop_sides < 0:
            raise RecorderError("--crop-sides cannot be negative")
        if args.next_wait_min < 0 or args.next_wait_max < args.next_wait_min:
            raise RecorderError("LinkedIn wait range is invalid")
        if args.batch_size_min < 1 or args.batch_size_max < args.batch_size_min:
            raise RecorderError("LinkedIn batch size range is invalid")
        if args.batch_wait_min < 0 or args.batch_wait_max < args.batch_wait_min:
            raise RecorderError("LinkedIn batch wait range is invalid")
        if args.short_batch_size_min < 1 or args.short_batch_size_max < args.short_batch_size_min:
            raise RecorderError("LinkedIn short batch size range is invalid")
        if args.short_batch_wait_min < 0 or args.short_batch_wait_max < args.short_batch_wait_min:
            raise RecorderError("LinkedIn short batch wait range is invalid")
        edge_executable = find_edge_executable(args.edge_executable)
        rows = read_websites(args.csv_path, args.source_column, args.row)
        rows = [
            (row_number, url)
            for row_number, url in rows
            if is_linkedin_candidate(url)
        ]
        if not rows:
            raise RecorderError("No pending LinkedIn source links were selected")
        wait_rng = random.SystemRandom()
        batch_target = wait_rng.randint(args.batch_size_min, args.batch_size_max)
        batch_count = 0
        short_batch_target = wait_rng.randint(
            args.short_batch_size_min, args.short_batch_size_max
        )
        short_batch_count = 0
        for index, (row_number, raw_url) in enumerate(rows):
            try:
                url = validate_link(raw_url)
                output_path = args.output_dir / safe_filename(url, row_number)
                print(f"Recording row {row_number}: {url}")
                record_one(url, output_path, args, edge_executable)
                if not output_path.is_file() or output_path.stat().st_size == 0:
                    raise RecorderError(f"LinkedIn recording was not created: {output_path}")
                update_fields(
                    args.csv_path,
                    row_number,
                    {
                        "source_video": project_relative_path(args.csv_path, output_path),
                        "status": "recorded_linkedin",
                    },
                )
                print(f"Wrote {output_path}")
            except RecorderError as exc:
                status = error_status("linkedin", exc)
                update_fields(
                    args.csv_path,
                    row_number,
                    {"source_video": "", "status": status},
                )
                print(f"Row {row_number} failed with {status}: {exc}")
            batch_count += 1
            short_batch_count += 1
            if index < len(rows) - 1:
                pause = wait_rng.uniform(args.next_wait_min, args.next_wait_max)
                print(f"Waiting {pause:.1f} seconds before the next LinkedIn profile")
                time.sleep(pause)
                if batch_count >= batch_target:
                    batch_pause = wait_rng.uniform(args.batch_wait_min, args.batch_wait_max)
                    print(
                        f"Completed {batch_count} LinkedIn profiles; waiting "
                        f"{batch_pause / 60:.1f} minutes before the next batch"
                    )
                    time.sleep(batch_pause)
                    batch_count = 0
                    batch_target = wait_rng.randint(args.batch_size_min, args.batch_size_max)
                    short_batch_count = 0
                    short_batch_target = wait_rng.randint(
                        args.short_batch_size_min, args.short_batch_size_max
                    )
                elif short_batch_count >= short_batch_target:
                    short_batch_pause = wait_rng.uniform(
                        args.short_batch_wait_min, args.short_batch_wait_max
                    )
                    print(
                        f"Completed {short_batch_count} LinkedIn profiles; waiting "
                        f"{short_batch_pause / 60:.1f} minutes before the next short batch"
                    )
                    time.sleep(short_batch_pause)
                    short_batch_count = 0
                    short_batch_target = wait_rng.randint(
                        args.short_batch_size_min, args.short_batch_size_max
                    )
        return 0
    except (RecorderError, StatusCsvError) as exc:
        print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
