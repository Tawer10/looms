#!/usr/bin/env python3
"""Retry forbidden website rows through distinct random proxies.

This script uses the existing website recorder and only selects rows whose
status is ``error_website_forbidden``. A successful retry records the video and
updates the row as a normal website recording. If all three proxy attempts fail,
the row is marked as unreachable even with a proxy.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

from record_website import (
    RecorderError,
    StatusCsvError,
    error_status,
    project_relative_path,
    primary_screen_size,
    read_websites,
    record_one,
    safe_filename,
    playwright_proxy_options,
    update_fields,
)
from record_linkedin import (
    close_native_window,
    find_edge_executable,
    find_native_window,
    maximize_window,
    start_window_capture,
    stop_window_capture,
    visible_windows,
)


FORBIDDEN_STATUS = "error_website_forbidden"
UNREACHABLE_WITH_PROXY_STATUS = "website_cannot_be_reached_even_with_proxy"
HEADED_NO_CONTENT_STATUS = "error_website_headed_no_content"
PROXY_ATTEMPTS = 3
GODADDY_TEXT_RE = re.compile(r"go\s*daddy", re.IGNORECASE)
INVALID_EDGE_TITLE_RE = re.compile(
    r"(?:robot challenge|captcha|verify you are human|access denied|forbidden|"
    r"just a moment|security check|error 403)",
    re.IGNORECASE,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retry forbidden website rows with three random proxies."
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        nargs="?",
        default=Path("data/data.csv"),
        help="CSV containing source links, default: data/data.csv",
    )
    parser.add_argument(
        "--proxy-file",
        type=Path,
        default=Path("data/proxies.txt"),
        help="Text file containing one proxy per line, default: data/proxies.txt",
    )
    parser.add_argument(
        "--edit",
        action="store_true",
        help="Scan every source link for GoDaddy text and update matching CSV rows",
    )
    parser.add_argument("--row", type=int, help="Retry only this CSV data row")
    parser.add_argument(
        "--source-column",
        "--website-column",
        dest="source_column",
        default="source_link",
        help="CSV column containing source URLs, default: source_link",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/website-videos"),
        help="Directory for recorded WebM files",
    )
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument(
        "--keep-last-seconds",
        type=float,
        default=5,
        help="Save only this many seconds from the end of each recording, default: 5",
    )
    parser.add_argument("--trim-seconds", type=float, default=2)
    parser.add_argument("--page-wait-max", type=float, default=15)
    parser.add_argument(
        "--http-error-wait",
        type=float,
        default=10,
        help="Seconds to wait after HTTP 403 before failing a proxy attempt",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.set_defaults(maximize_window=False)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument(
        "--capture-mode",
        choices=("browser", "viewport"),
        default="browser",
        help="Capture the separate Chromium window by default, or only its viewport",
    )
    parser.add_argument("--max-clicks", type=int, default=3)
    parser.add_argument("--actions", action="store_true")
    parser.add_argument("--timeout", type=int, default=20_000)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--seed", type=int)
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headed", dest="headless", action="store_false", default=False)
    headless_group.add_argument("--headless", dest="headless", action="store_true")
    return parser.parse_args(argv)


def load_proxies(proxy_file: Path) -> list[str]:
    if not proxy_file.is_file():
        raise RecorderError(f"Proxy file does not exist: {proxy_file}")
    proxies = []
    for raw_line in proxy_file.read_text(encoding="utf-8-sig").splitlines():
        proxy = raw_line.split("#", 1)[0].strip()
        if proxy:
            proxies.append(proxy)
    unique_proxies = list(dict.fromkeys(proxies))
    if len(unique_proxies) < PROXY_ATTEMPTS:
        raise RecorderError(
            f"Proxy file must contain at least {PROXY_ATTEMPTS} distinct proxies: {proxy_file}"
        )
    return unique_proxies


def check_godaddy_text(
    url: str, proxies: list[str], *, headed: bool = False
) -> tuple[bool, str | None]:
    normalized_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
    selected_proxies = random.SystemRandom().sample(
        proxies, min(PROXY_ATTEMPTS, len(proxies))
    )
    last_error: str | None = None
    for proxy in selected_proxies:
        try:
            if headed:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    launch_args = [
                        "--kiosk",
                        "--start-fullscreen",
                        "--force-device-scale-factor=1",
                        "--window-position=0,0",
                    ]
                    screen_size = primary_screen_size()
                    if screen_size is not None:
                        launch_args.append(f"--window-size={screen_size[0]},{screen_size[1]}")
                    browser = playwright.chromium.launch(
                        headless=False,
                        args=launch_args,
                        proxy=playwright_proxy_options(proxy),
                    )
                    try:
                        page = browser.new_page(ignore_https_errors=True)
                        page.goto(normalized_url, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(2_000)
                        body = page.locator("body").inner_text(timeout=3_000)
                    finally:
                        browser.close()
                return bool(GODADDY_TEXT_RE.search(body)), None
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            request = urllib.request.Request(
                normalized_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"},
            )
            with opener.open(request, timeout=15) as response:
                body = response.read(500_000).decode("utf-8", "ignore")
            return bool(GODADDY_TEXT_RE.search(body)), None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return False, last_error


def edit_godaddy_statuses(
    csv_path: Path,
    source_column: str,
    proxies: list[str],
    workers: int,
    headed: bool,
) -> None:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or source_column not in rows[0]:
        raise RecorderError(f"CSV has no {source_column!r} column")
    websites = [
        (row_number, (row.get(source_column) or "").strip())
        for row_number, row in enumerate(rows, start=1)
        if (row.get(source_column) or "").strip()
    ]

    def check(item: tuple[int, str]) -> tuple[int, str, bool, str | None]:
        row_number, url = item
        hit, error = check_godaddy_text(url, proxies, headed=headed)
        return row_number, url, hit, error

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(check, websites))
    godaddy_rows = [result for result in results if result[2]]
    failed_rows = [result for result in results if result[3] and not result[2]]
    for row_number, url, _hit, _error in godaddy_rows:
        update_fields(csv_path, row_number, {"status": FORBIDDEN_STATUS})
        print(f"Edited row {row_number}: GoDaddy text found in {url}")
    for row_number, url, _hit, error in failed_rows:
        update_fields(
            csv_path,
            row_number,
            {"status": UNREACHABLE_WITH_PROXY_STATUS},
        )
        print(f"Edited row {row_number}: unreachable after three proxies for {url} ({error})")
    print(
        f"Checked {len(results)} websites, found GoDaddy text on {len(godaddy_rows)}, "
        f"and could not reach {len(failed_rows)}"
    )


def read_forbidden_websites(
    csv_path: Path, column: str, row_number: int | None
) -> list[tuple[int, str]]:
    websites = read_websites(csv_path, column, row_number, pending_only=False)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected = []
    for actual_row, url in websites:
        status = (rows[actual_row - 1].get("status") or "").strip()
        if status == FORBIDDEN_STATUS:
            selected.append((actual_row, url))
    if not selected:
        raise RecorderError(f"CSV contains no rows with status {FORBIDDEN_STATUS!r}")
    return selected


def record_native_edge_one(
    url: str,
    output_path: Path,
    args: argparse.Namespace,
    proxy: str,
) -> None:
    if os.name != "nt":
        raise RecorderError("Native Edge capture requires Windows")
    edge_executable = find_edge_executable(None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_handles = {hwnd for hwnd, _title, _process in visible_windows()}
    command = [
        str(edge_executable),
        "--new-window",
        "--start-maximized",
        f"--proxy-server={proxy}",
        url,
    ]
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RecorderError(f"Could not open Microsoft Edge: {exc}") from exc
    hwnd, title = find_native_window(
        "",
        existing_handles,
        {"msedge.exe"},
        "Microsoft Edge",
        timeout=20,
    )
    print(f"Opened native Edge window: {title}")
    maximize_window(hwnd)
    wait_seconds = min(args.page_wait_max, 15)
    print(f"Waiting up to {wait_seconds:g} seconds for the website to render")
    time.sleep(wait_seconds)
    current_title = next(
        (candidate_title for candidate_hwnd, candidate_title, _process in visible_windows() if candidate_hwnd == hwnd),
        title,
    )
    if (
        not current_title.strip()
        or current_title.lower() in {"new tab", "microsoft edge"}
        or INVALID_EDGE_TITLE_RE.search(current_title)
    ):
        close_native_window(hwnd)
        raise RecorderError("Website did not display valid headed content")
    capture_path = output_path.with_name(f".{output_path.stem}.edge.capture.webm")
    capture = None
    try:
        capture = start_window_capture(args.ffmpeg, hwnd, capture_path)
        time.sleep(args.duration)
    finally:
        if capture is not None:
            stop_window_capture(capture)
        close_native_window(hwnd)
    if not capture_path.is_file() or capture_path.stat().st_size == 0:
        raise RecorderError("Native Edge capture produced an empty recording")
    trimmed_path = output_path.with_name(f".{output_path.stem}.edge.last.webm")
    command = [
        args.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-sseof",
        f"-{args.keep_last_seconds:g}",
        "-i",
        str(capture_path),
        "-t",
        f"{args.keep_last_seconds:g}",
        "-vf",
        "setpts=PTS-STARTPTS",
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "32",
        "-b:v",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(trimmed_path),
    ]
    try:
        subprocess.run(command, check=True, text=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        capture_path.unlink(missing_ok=True)
        trimmed_path.unlink(missing_ok=True)
        raise RecorderError(f"Could not keep the last seconds of the recording: {detail.strip()}") from exc
    if not trimmed_path.is_file() or trimmed_path.stat().st_size == 0:
        capture_path.unlink(missing_ok=True)
        raise RecorderError("FFmpeg produced an empty last five seconds recording")
    capture_path.unlink(missing_ok=True)
    trimmed_path.replace(output_path)


def process_row(
    csv_path: Path,
    output_dir: Path,
    row_number: int,
    url: str,
    args: argparse.Namespace,
    proxies: list[str],
) -> None:
    output_path = output_dir / safe_filename(url, row_number)
    row_rng = random.Random(args.seed + row_number if args.seed is not None else None)
    selected_proxies = row_rng.sample(proxies, PROXY_ATTEMPTS)
    failures: list[str] = []
    print(f"Retrying forbidden row {row_number}: {url}")
    for attempt, proxy in enumerate(selected_proxies, start=1):
        try:
            print(f"Row {row_number}, proxy attempt {attempt}/{PROXY_ATTEMPTS}")
            record_native_edge_one(url, output_path, args, proxy)
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise RecorderError(f"Website recording was not created: {output_path}")
            update_fields(
                csv_path,
                row_number,
                {
                    "source_video": project_relative_path(csv_path, output_path),
                    "status": "recorded_website",
                },
            )
            print(f"Wrote {output_path}")
            return
        except RecorderError as exc:
            if "did not display valid" in str(exc).lower():
                update_fields(
                    csv_path,
                    row_number,
                    {"source_video": "", "status": HEADED_NO_CONTENT_STATUS},
                )
                print(f"Row {row_number} failed with {HEADED_NO_CONTENT_STATUS}: {exc}")
                return
            failures.append(error_status("website_proxy", exc))
            print(f"Row {row_number}, proxy attempt {attempt} failed: {exc}")

    update_fields(
        csv_path,
        row_number,
        {
            "source_video": "",
            "status": UNREACHABLE_WITH_PROXY_STATUS,
        },
    )
    print(
        f"Row {row_number} failed with {UNREACHABLE_WITH_PROXY_STATUS} "
        f"after {len(failures)} proxy attempts"
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.duration <= 0:
            raise RecorderError("--duration must be greater than zero")
        if args.keep_last_seconds <= 0 or args.keep_last_seconds > args.duration:
            raise RecorderError("--keep-last-seconds must be greater than zero and no greater than --duration")
        if args.trim_seconds < 0:
            raise RecorderError("--trim-seconds cannot be negative")
        if args.width <= 0 or args.height <= 0:
            raise RecorderError("--width and --height must be greater than zero")
        if args.max_clicks < 0:
            raise RecorderError("--max-clicks cannot be negative")
        if args.http_error_wait < 0:
            raise RecorderError("--http-error-wait cannot be negative")
        if args.workers <= 0:
            raise RecorderError("--workers must be greater than zero")
        proxies = load_proxies(args.proxy_file)
        if args.edit:
            edit_godaddy_statuses(
                args.csv_path,
                args.source_column,
                proxies,
                args.workers,
                headed=not args.headless,
            )
            return 0
        websites = read_forbidden_websites(args.csv_path, args.source_column, args.row)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(
                    process_row,
                    args.csv_path,
                    args.output_dir,
                    row_number,
                    url,
                    args,
                    proxies,
                )
                for row_number, url in websites
            ]
            for future in as_completed(futures):
                future.result()
        return 0
    except (RecorderError, StatusCsvError) as exc:
        print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
