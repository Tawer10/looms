#!/usr/bin/env python3
"""Record website walkthrough videos from the website column of a CSV.

The recorder uses Playwright's browser video capture. Actions are deliberately
slow and conservative: it scrolls in moderate increments and only clicks
informational or consent controls, never forms or purchase actions.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urljoin, urlparse, urlsplit
from ctypes import wintypes



class StatusCsvError(RuntimeError):
    """An expected CSV status update error."""


_CSV_UPDATE_LOCK = threading.Lock()


def error_status(prefix: str, error: Exception) -> str:
    """Return a short stable status that prevents repeated failed attempts."""
    message = str(error).lower()
    if "source link is not a linkedin url" in message:
        detail = "invalid_link"
    elif "invalid linkedin profile" in message or "linkedin profile is unavailable" in message:
        detail = "invalid_profile"
    elif (
        "could not find the visible linkedin window" in message
        or "could not find the visible microsoft edge window" in message
        or "linkedin profile was not reached" in message
    ):
        detail = "unreached_profile"
    elif "timed out opening" in message or "timeout" in message:
        detail = "timeout"
    elif "403" in message or "forbidden" in message:
        detail = "forbidden"
    elif "401" in message or "unauthorized" in message:
        detail = "unauthorized"
    elif "429" in message or "too many requests" in message:
        detail = "rate_limited"
    elif "did not display valid visible text" in message or "did not display a valid website title" in message:
        detail = "no_content"
    elif "invalid linkedin profile" in message or "linkedin profile is unavailable" in message:
        detail = "invalid_profile"
    elif "could not launch" in message:
        detail = "browser_launch"
    elif "capture" in message:
        detail = "capture"
    elif "playwright" in message:
        detail = "playwright"
    elif "page.goto" in message or "navigation" in message or "err_cert" in message:
        detail = "navigation"
    else:
        detail = "recording"
    return f"error_{prefix}_{detail}"


def _update_fields(csv_path: Path, row_number: int, fields: dict[str, str]) -> None:
    if not csv_path.is_file():
        raise StatusCsvError(f"CSV file does not exist: {csv_path}")
    if row_number < 1:
        raise StatusCsvError("CSV data row must be one or greater")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames:
        raise StatusCsvError(f"CSV has no header row: {csv_path}")
    if row_number > len(rows):
        raise StatusCsvError(f"CSV has no data row {row_number}")
    for field in ("status", *fields):
        if field not in fieldnames:
            fieldnames.append(field)
    rows[row_number - 1].update(fields)
    temporary_path = csv_path.with_name(f".{csv_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary_path.replace(csv_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def update_fields(csv_path: Path, row_number: int, fields: dict[str, str]) -> None:
    with _CSV_UPDATE_LOCK:
        _update_fields(csv_path, row_number, fields)


def project_relative_path(csv_path: Path, target_path: Path) -> str:
    project_root = csv_path.parent.parent if csv_path.parent.name.lower() == "data" else csv_path.parent
    try:
        return target_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return target_path.resolve().as_posix()


class RecorderError(RuntimeError):
    """An expected, user actionable recording error."""


SAFE_CLICK_RE = re.compile(
    r"(?:accept(?: all)?|agree|got it|dismiss|close|learn more|read more|"
    r"view more|see more|explore|discover|about|services|products|features|"
    r"how it works|menu)$",
    re.IGNORECASE,
)
BLOCKED_CLICK_RE = re.compile(
    r"(?:log ?in|sign ?in|sign ?up|register|buy|purchase|checkout|cart|"
    r"subscribe|delete|remove|submit|send|contact|book|schedule|apply|"
    r"download|share|payment|pricing|free trial)",
    re.IGNORECASE,
)
FORBIDDEN_PAGE_RE = re.compile(
    r"(?:\b403\s+forbidden\b|request forbidden|forbidden by administrative rules|"
    r"access denied|this domain is parked|domain name is parked|"
    r"domain name is for sale|this domain is not connected|future home of something quite cool|"
    r"powered by godaddy)",
    re.IGNORECASE,
)
VISIBLE_TEXT_RE = re.compile(r"[A-Za-z]{3,}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record slow, conservative website walkthroughs from a CSV."
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        nargs="?",
        default=Path("data/data.csv"),
        help="CSV containing source links, default: data/data.csv",
    )
    parser.add_argument(
        "--row",
        type=int,
        help="Record only this one based CSV data row. By default, record every row.",
    )
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
    parser.add_argument("--duration", type=float, default=10, help="Final seconds per recording, default: 10")
    parser.add_argument(
        "--trim-seconds",
        type=float,
        default=2,
        help="Seconds to remove from the beginning, default: 2",
    )
    parser.add_argument(
        "--page-wait-max",
        type=float,
        default=15,
        help="Maximum seconds to wait for visible website text, default: 15",
    )
    parser.add_argument(
        "--workers", type=int, default=6, help="Concurrent recordings, default: 6"
    )
    parser.add_argument("--width", type=int, default=1440, help="Browser viewport width")
    parser.add_argument("--height", type=int, default=900, help="Browser viewport height")
    parser.add_argument(
        "--capture-mode",
        choices=("browser", "viewport"),
        default="viewport",
        help="Capture the native browser window or only its viewport, default: viewport",
    )
    parser.add_argument(
        "--max-clicks",
        type=int,
        default=3,
        help="Maximum safe informational clicks per recording",
    )
    parser.add_argument(
        "--actions",
        action="store_true",
        help="Opt in to spaced scrolling and safe informational clicks",
    )
    parser.add_argument("--timeout", type=int, default=20_000, help="Navigation timeout in ms")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable for browser capture")
    parser.add_argument("--seed", type=int, help="Optional seed for repeatable action timing")
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
        help="Run without showing the browser window, default",
    )
    headless_group.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Show the browser window during viewport recording",
    )
    return parser.parse_args(argv)


def read_websites(
    csv_path: Path, column: str, row_number: int | None, *, pending_only: bool = True
) -> list[tuple[int, str]]:
    if not csv_path.is_file():
        raise RecorderError(f"CSV file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if column not in fieldnames:
            available = ", ".join(fieldnames) or "none"
            raise RecorderError(f"CSV has no {column!r} column. Available columns: {available}")
        rows = list(reader)

    if row_number is not None and row_number < 1:
        raise RecorderError("--row must be one or greater")
    selected = rows if row_number is None else rows[row_number - 1 : row_number]
    if row_number is not None and len(selected) != 1:
        raise RecorderError(f"CSV has no data row {row_number}")

    websites: list[tuple[int, str]] = []
    for offset, row in enumerate(selected):
        actual_row = row_number if row_number is not None else offset + 1
        raw_url = (row.get(column) or "").strip()
        if not raw_url:
            continue
        if pending_only and (row.get("status") or "").strip():
            continue
        url = raw_url if urlparse(raw_url).scheme else f"https://{raw_url}"
        parsed = urlparse(url)
        websites.append((actual_row, url))
    if not websites:
        qualifier = " pending" if pending_only else ""
        raise RecorderError(f"CSV contains no{qualifier} source links to record")
    return websites


def safe_filename(url: str, row_number: int) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    host = re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "website"
    return f"row_{row_number:03d}_{host}.webm"


def process_name_for_window(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(process)


def visible_windows() -> list[tuple[int, str, str]]:
    """Return visible Windows handles, titles, and process names."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    windows: list[tuple[int, str, str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        title = title_buffer.value.strip()
        if title:
            windows.append((hwnd, title, process_name_for_window(hwnd)))
        return True

    callback_reference = callback_type(callback)
    user32.EnumWindows(callback_reference, 0)
    return windows


def find_native_window(
    page_title: str,
    excluded_handles: set[int] | None = None,
    process_names: set[str] | None = None,
    browser_label: str = "browser",
    timeout: float = 10.0,
) -> tuple[int, str]:
    if os.name != "nt":
        raise RecorderError("Native browser window capture currently requires Windows")
    wanted = page_title.strip().lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fallback = None
        fallback = None
        for hwnd, title, process_name in visible_windows():
            if excluded_handles and hwnd in excluded_handles:
                continue
            if process_names is not None and process_name not in process_names:
                continue
            lowered = title.lower()
            if wanted and wanted in lowered:
                return hwnd, title
            if not wanted or browser_label.lower() in lowered:
                fallback = (hwnd, title)
        if fallback is not None:
            return fallback
        time.sleep(0.2)
    raise RecorderError(
        f"Could not find the visible {browser_label} window."
    )


def find_browser_window(
    page_title: str,
    excluded_handles: set[int] | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    return find_native_window(
        page_title,
        excluded_handles,
        {"chrome.exe", "chromium.exe"},
        "Chromium",
        timeout,
    )


def maximize_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow(hwnd, 3)
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    user32.SetWindowPos(hwnd, -1, 0, 0, width, height, 0x0040)
    user32.BringWindowToTop(hwnd)
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    current_thread = kernel32.GetCurrentThreadId()
    if foreground_thread and foreground_thread != current_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.SetActiveWindow(hwnd)
    user32.SwitchToThisWindow(hwnd, True)
    if foreground_thread and foreground_thread != current_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, False)
    time.sleep(0.5)


def primary_screen_size() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    if width <= 0 or height <= 0:
        return None
    return width, height


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RecorderError("Could not read the Chromium window bounds")
    user32 = ctypes.windll.user32
    virtual_left = user32.GetSystemMetrics(76)
    virtual_top = user32.GetSystemMetrics(77)
    virtual_right = virtual_left + user32.GetSystemMetrics(78)
    virtual_bottom = virtual_top + user32.GetSystemMetrics(79)
    left = max(rect.left, virtual_left)
    top = max(rect.top, virtual_top)
    right = min(rect.right, virtual_right)
    bottom = min(rect.bottom, virtual_bottom)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise RecorderError("Chromium reported invalid window bounds")
    return left, top, width, height


def start_window_capture(ffmpeg: str, hwnd: int, output_path: Path) -> subprocess.Popen[str]:
    left, top, width, height = window_rect(hwnd)
    print(f"Capturing native browser region {width}x{height} at {left},{top}")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "gdigrab",
        "-framerate",
        "25",
        "-draw_mouse",
        "1",
        "-offset_x",
        str(left),
        "-offset_y",
        str(top),
        "-video_size",
        f"{width}x{height}",
        "-i",
        "desktop",
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "32",
        "-b:v",
        "0",
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RecorderError(
            f"Could not find {ffmpeg!r}. Install FFmpeg or pass --ffmpeg."
        ) from exc
    time.sleep(0.7)
    if process.poll() is not None:
        detail = (process.stderr.read() if process.stderr else "").strip()
        raise RecorderError(f"Native browser capture failed to start: {detail}")
    return process


def stop_window_capture(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        process.communicate()
        return
    try:
        _, stderr = process.communicate(input="q\n", timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate()
    if process.returncode != 0:
        detail = (stderr or "").strip()
        raise RecorderError(f"Native browser capture failed: {detail}")


def visible_safe_candidate(page):
    """Return a visible same site informational control, if one exists."""
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=SAFE_CLICK_RE)
        for index in range(min(locator.count(), 20)):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                label = candidate.inner_text(timeout=500).strip()
                if not label or BLOCKED_CLICK_RE.search(label):
                    continue
                if role == "link":
                    href = candidate.get_attribute("href")
                    if href:
                        target = urlparse(urljoin(page.url, href))
                        current = urlparse(page.url)
                        if target.netloc and target.netloc != current.netloc:
                            continue
                return candidate
            except Exception:
                continue
    return None


def make_scroll(page, rng: random.Random, viewport_height: int) -> None:
    direction = -1 if rng.random() < 0.15 else 1
    distance = round(viewport_height * rng.uniform(0.45, 0.80))
    page.mouse.wheel(0, direction * distance)


def make_safe_click(page, rng: random.Random) -> bool:
    candidate = visible_safe_candidate(page)
    if candidate is None:
        return False
    try:
        candidate.scroll_into_view_if_needed(timeout=1_500)
        page.wait_for_timeout(rng.randint(700, 1_400))
        candidate.click(timeout=2_000)
        return True
    except Exception:
        return False


def perform_walkthrough(
    page, duration: float, viewport_height: int, max_clicks: int, rng: random.Random
) -> None:
    deadline = time.monotonic() + duration
    clicks = 0
    action_number = 0
    while time.monotonic() < deadline:
        pause = min(rng.uniform(2.5, 5.0), max(0.25, deadline - time.monotonic()))
        page.wait_for_timeout(round(pause * 1_000))
        if time.monotonic() >= deadline:
            break
        action_number += 1
        should_click = clicks < max_clicks and (action_number > 1 and rng.random() < 0.32)
        if should_click and make_safe_click(page, rng):
            clicks += 1
        else:
            make_scroll(page, rng, viewport_height)


def wait_for_valid_content(page, max_wait: float, url: str) -> None:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=1_000).strip()
        except Exception:
            body_text = ""
        if FORBIDDEN_PAGE_RE.search(body_text[:10_000]):
            raise RecorderError(f"Website displayed a forbidden page for {url}")
        normalized = re.sub(r"\s+", " ", body_text)
        if len(normalized) >= 30 and VISIBLE_TEXT_RE.search(normalized):
            return
        page.wait_for_timeout(250)
    raise RecorderError(
        f"Website did not display valid visible text within {max_wait:g} seconds: {url}"
    )


def playwright_proxy_options(proxy: str) -> dict[str, str]:
    parsed = urlsplit(proxy)
    if not parsed.scheme or not parsed.hostname or parsed.port is None:
        raise RecorderError(f"Invalid proxy format: {proxy}")
    options = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    }
    if parsed.username:
        options["username"] = unquote(parsed.username)
    if parsed.password:
        options["password"] = unquote(parsed.password)
    return options


def record_one(
    url: str,
    output_path: Path,
    args: argparse.Namespace,
    rng: random.Random,
    proxy: str | None = None,
) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RecorderError(
            "Playwright is required. Install dependencies with "
            "python -m pip install -r requirements.txt and then run "
            "python -m playwright install chromium."
        ) from exc

    if args.capture_mode == "browser" and args.headless:
        raise RecorderError("Native browser capture needs a visible browser; use --capture-mode viewport with --headless")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="website-recording-") as temp_dir:
        with sync_playwright() as playwright:
            launch_args = []
            if args.capture_mode == "browser" or not args.headless:
                launch_args = [
                    "--kiosk",
                    "--start-fullscreen",
                    "--start-maximized",
                    "--force-device-scale-factor=1",
                    "--window-position=0,0",
                ]
                screen_size = primary_screen_size()
                if screen_size is not None:
                    launch_args.append(f"--window-size={screen_size[0]},{screen_size[1]}")
            existing_window_handles = (
                {hwnd for hwnd, _title, _process in visible_windows()}
                if args.capture_mode == "browser"
                else set()
            )
            try:
                launch_options = {"headless": args.headless, "args": launch_args}
                if proxy:
                    launch_options["proxy"] = playwright_proxy_options(proxy)
                browser = playwright.chromium.launch(**launch_options)
            except Exception as exc:
                raise RecorderError(
                    "Could not launch Chromium. Run python -m playwright install chromium."
                ) from exc
            if args.capture_mode == "browser":
                context = browser.new_context(viewport=None, ignore_https_errors=True)
                staged_path = Path(temp_dir) / "browser-window.webm"
                video = None
            else:
                context = browser.new_context(
                    viewport={"width": args.width, "height": args.height},
                    record_video_dir=temp_dir,
                    record_video_size={"width": args.width, "height": args.height},
                    ignore_https_errors=True,
                )
                staged_path = None
                video = None
            page = context.new_page()
            native_capture = None
            capture_error: RecorderError | None = None
            try:
                video = page.video
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
                    if response is not None and response.status >= 400:
                        if response.status == 403:
                            error_wait = getattr(args, "http_error_wait", 0)
                            if error_wait > 0:
                                page.wait_for_timeout(round(error_wait * 1_000))
                        raise RecorderError(
                            f"Website returned HTTP {response.status} for {url}"
                        )
                except PlaywrightTimeoutError:
                    if page.url == "about:blank":
                        raise RecorderError(f"Timed out opening {url}")
                except PlaywrightError as exc:
                    raise RecorderError(f"Navigation failed for {url}: {exc}") from exc
                if args.page_wait_max <= 0:
                    raise RecorderError("--page-wait-max must be greater than zero")
                wait_for_valid_content(page, args.page_wait_max, url)
                page.wait_for_timeout(rng.randint(500, 1_000))
                if args.capture_mode == "browser":
                    native_hwnd, _window_title = find_browser_window(
                        page.title(), existing_window_handles
                    )
                    print(f"Selected native browser window: {_window_title}")
                    if getattr(args, "maximize_window", True):
                        maximize_window(native_hwnd)
                    native_capture = start_window_capture(args.ffmpeg, native_hwnd, staged_path)
                recording_duration = args.duration + args.trim_seconds
                if args.actions:
                    perform_walkthrough(page, recording_duration, args.height, args.max_clicks, rng)
                else:
                    page.wait_for_timeout(round(recording_duration * 1_000))
            finally:
                if native_capture is not None:
                    try:
                        stop_window_capture(native_capture)
                    except RecorderError as exc:
                        capture_error = exc
                try:
                    context.close()
                finally:
                    browser.close()
            if capture_error is not None:
                raise capture_error
            if args.capture_mode == "browser":
                source_path = staged_path
            else:
                if video is None:
                    raise RecorderError("Playwright did not create a video for the page")
                source_path = Path(video.path())
        if args.trim_seconds <= 0:
            shutil.move(str(source_path), str(output_path))
            return
        trimmed_path = Path(temp_dir) / "trimmed-recording.webm"
        command = [
            args.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            f"trim=start={args.trim_seconds},setpts=PTS-STARTPTS",
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
            result = subprocess.run(command, check=True, text=True, capture_output=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise RecorderError(f"Could not trim the recording: {detail.strip()}") from exc
        if not trimmed_path.is_file() or trimmed_path.stat().st_size == 0:
            raise RecorderError("FFmpeg produced an empty trimmed recording")
        shutil.move(str(trimmed_path), str(output_path))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.duration <= 0:
            raise RecorderError("--duration must be greater than zero")
        if args.trim_seconds < 0:
            raise RecorderError("--trim-seconds cannot be negative")
        if args.width <= 0 or args.height <= 0:
            raise RecorderError("--width and --height must be greater than zero")
        if args.max_clicks < 0:
            raise RecorderError("--max-clicks cannot be negative")
        websites = read_websites(args.csv_path, args.source_column, args.row)
        websites = [
            (row_number, url)
            for row_number, url in websites
            if not urlparse(url).netloc.lower().removeprefix("www.").endswith("linkedin.com")
        ]
        if not websites:
            raise RecorderError(
                "No website source links were selected. Use record_linkedin.py for LinkedIn rows."
            )
        if args.workers <= 0:
            raise RecorderError("--workers must be greater than zero")

        def process_row(row: tuple[int, str]) -> None:
            row_number, url = row
            output_path = args.output_dir / safe_filename(url, row_number)
            print(f"Recording row {row_number}: {url}")
            try:
                row_seed = args.seed + row_number if args.seed is not None else None
                rng = random.Random(row_seed)
                record_one(url, output_path, args, rng)
                if not output_path.is_file() or output_path.stat().st_size == 0:
                    raise RecorderError(f"Website recording was not created: {output_path}")
                update_fields(
                    args.csv_path,
                    row_number,
                    {
                        "source_video": project_relative_path(args.csv_path, output_path),
                        "status": "recorded_website",
                    },
                )
                print(f"Wrote {output_path}")
            except RecorderError as exc:
                status = error_status("website", exc)
                update_fields(args.csv_path, row_number, {"status": status})
                print(f"Row {row_number} failed with {status}: {exc}")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(process_row, row) for row in websites]
            for future in as_completed(futures):
                future.result()
        return 0
    except (RecorderError, StatusCsvError) as exc:
        print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
