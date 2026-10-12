"""Export Drive files from a folder and optionally set link access to Viewer.

Examples:
    Run this file directly from PyCharm using the defaults below.
    python change_drive_permission.py --dry-run

The default PyCharm run scans the Camp2-videos folder, writes drive_files.csv,
and applies link Viewer access. Use --dry-run to make no sharing changes.
OAuth credentials are read from credentials.json beside this script;
the refresh token is stored in token.json beside it.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import socket
from pathlib import Path
from typing import Any, Iterable

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import Resource, build
    from googleapiclient.errors import HttpError
except ModuleNotFoundError as error:  # pragma: no cover
    Request = Credentials = InstalledAppFlow = Resource = build = None  # type: ignore[assignment]
    HttpError = Exception  # type: ignore[misc,assignment]
    GOOGLE_API_IMPORT_ERROR = error
else:
    GOOGLE_API_IMPORT_ERROR = None


SCOPES = ["https://www.googleapis.com/auth/drive"]
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_CREDENTIALS = DATA_DIR / "credentials.json"
DEFAULT_TOKEN = DATA_DIR / "token.json"
DEFAULT_OUTPUT = DATA_DIR / "drive_files.csv"
# Current Drive input folder: Camp2-videos.
# DEFAULT_FOLDER_ID = "1HwXgtAkNwuO3gAY8V4HQrcjVdR6aSXpd"
DEFAULT_FOLDER_ID = "10g6l9IK_qqrPCcjfMZfUUovErxlUUpGp"
LOGGER = logging.getLogger("change_drive_permission")
NETWORK_ERRORS = (OSError, socket.timeout, TimeoutError)


def authenticate(credentials_path: Path, token_path: Path) -> Credentials:
    """Load or create OAuth credentials for the Drive API."""
    if GOOGLE_API_IMPORT_ERROR is not None:
        raise RuntimeError(
            "Google API dependencies are missing. Install them with "
            "`python -m pip install -r requirements-drive.txt`."
        ) from GOOGLE_API_IMPORT_ERROR

    credentials: Credentials | None = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials is None or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Missing {credentials_path}. Download an OAuth desktop app "
                    "client secret from Google Cloud Console and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")

    return credentials


def extract_folder_id(value: str) -> str:
    """Accept a raw folder ID or a standard Google Drive folder URL."""
    value = value.strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    raise ValueError("folder-id must be a Drive folder ID or a Drive folder URL")


def iter_children(
    drive: Resource,
    folder_id: str,
    recursive: bool,
) -> Iterable[dict[str, Any]]:
    """Yield files in a folder, optionally descending into subfolders."""
    folders = [folder_id]
    seen_folders: set[str] = set()

    while folders:
        current_folder = folders.pop(0)
        if current_folder in seen_folders:
            continue
        seen_folders.add(current_folder)

        page_token: str | None = None
        while True:
            response = (
                drive.files()
                .list(
                    q=f"'{current_folder}' in parents and trashed = false",
                    spaces="drive",
                    fields=(
                        "nextPageToken, files(id, name, mimeType, webViewLink, "
                        "parents, permissions(id, type, role, allowFileDiscovery))"
                    ),
                    pageSize=1000,
                    pageToken=page_token,
                    orderBy="name",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=5)
            )
            for file in response.get("files", []):
                if file.get("mimeType") == "application/vnd.google-apps.folder":
                    if recursive:
                        folders.append(file["id"])
                    continue
                yield file
            page_token = response.get("nextPageToken")
            if not page_token:
                break


def get_anyone_permission(file: dict[str, Any]) -> dict[str, Any] | None:
    """Return the existing anyone permission already included in file metadata."""
    return next(
        (
            permission
            for permission in file.get("permissions", [])
            if permission.get("type") == "anyone"
        ),
        None,
    )


def set_link_viewer(
    drive: Resource,
    file_id: str,
    permission: dict[str, Any] | None,
) -> str:
    """Create or reduce an anyone permission to link Viewer access."""
    body = {"type": "anyone", "role": "reader", "allowFileDiscovery": False}
    if permission is None:
        drive.permissions().create(
            fileId=file_id,
            body=body,
            fields="id",
            supportsAllDrives=True,
        ).execute(num_retries=5)
        return "created"

    if permission.get("role") == "reader" and permission.get("allowFileDiscovery") is False:
        return "unchanged"

    drive.permissions().update(
        fileId=file_id,
        permissionId=permission["id"],
        body=body,
        fields="id",
        supportsAllDrives=True,
    ).execute(num_retries=5)
    return "updated"


def inspect_file(
    drive: Resource,
    file: dict[str, Any],
    apply_changes: bool,
) -> dict[str, str]:
    """Inspect one file, optionally update sharing, and return CSV data."""
    file_id = file["id"]
    permission = get_anyone_permission(file)
    already_viewer = permission and permission.get("role") == "reader" and permission.get(
        "allowFileDiscovery"
    ) is False
    action = "unchanged" if already_viewer else "update" if permission else "create"

    if apply_changes:
        action = set_link_viewer(drive, file_id, permission)

    LOGGER.info("%-9s %s", action, file.get("name", file_id))
    return {
        "name": file.get("name", ""),
        "id": file_id,
        "mime_type": file.get("mimeType", ""),
        "web_url": file.get("webViewLink", ""),
        "permission": action,
        "error": "",
    }


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["name", "id", "mime_type", "web_url", "permission", "error"]
    with output_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder-id",
        default=DEFAULT_FOLDER_ID,
        help="Google Drive folder ID or folder URL to scan",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="include files inside nested subfolders",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and export only; do not change permissions",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows: list[dict[str, str]] = []

    try:
        folder_id = extract_folder_id(args.folder_id)
        credentials = authenticate(args.credentials, args.token)
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

        for file in iter_children(drive, folder_id, recursive=args.recursive):
            try:
                rows.append(inspect_file(drive, file, apply_changes=not args.dry_run))
            except (HttpError, *NETWORK_ERRORS) as error:
                LOGGER.error("failed    %s: %s", file.get("name", file["id"]), error)
                rows.append(
                    {
                        "name": file.get("name", ""),
                        "id": file["id"],
                        "mime_type": file.get("mimeType", ""),
                        "web_url": file.get("webViewLink", ""),
                        "permission": "failed",
                        "error": str(error),
                    }
                )

    except (FileNotFoundError, HttpError, RuntimeError, ValueError, *NETWORK_ERRORS) as error:
        LOGGER.error("%s", error)
        write_csv(args.output, rows)
        return 1

    write_csv(args.output, rows)

    LOGGER.info("Wrote %d file rows to %s", len(rows), args.output)
    if args.dry_run:
        LOGGER.info("Dry run complete. Run without --dry-run to change permissions.")
    return 0 if not any(row["permission"] == "failed" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
