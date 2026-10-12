# Repository instructions

This repository contains Windows Python tools for recording website and LinkedIn walkthroughs, composing videos, creating GIFs, and inspecting Google Drive permissions. Keep it limited to reusable source code and documentation.

## Scripts

`record_website.py` records website URLs from a CSV with Playwright Chromium, conservative scrolling, optional safe informational clicks, and FFmpeg trimming. It writes WebM files and updates `source_video` and `status`.

`record_linkedin.py` records LinkedIn URLs in Microsoft Edge through native window capture. It crops the result with FFmpeg and updates `source_video` and `status`. It does not automate login, messaging, or connection requests.

`record_website_forbidden.py` retries blocked websites through distinct proxies from a local proxy file, checks page content, records usable pages through Edge, and updates CSV statuses.

`compose_video.py` combines browser and Loom recordings through FFmpeg. It supports one pair or pending CSV rows, circular or rectangular overlays, keyframes, audio selection, sizing, and dry runs. Composition CSVs need `source_video` and either `loom_video` or `loom-video`.

`create_gifs.py` converts local videos to looping GIF files with FFmpeg. It supports frame rate, width, trimming, worker count, overwrite behavior, and dry runs.

`change_drive_permission.py` authenticates to Google Drive, exports file metadata, and can change link access to Viewer. Always start with `--dry-run`. Running without it changes external Drive permissions.

## Requirements and setup

Use Windows with Python 3.11 or newer. From this directory:

```text
python -m venv .venv
.venv\\Scripts\\activate
python -m pip install --upgrade pip
python -m pip install playwright google-api-python-client google-auth-httplib2 google-auth-oauthlib
python -m playwright install chromium
```

Install FFmpeg and FFprobe and make sure `ffmpeg -version` and `ffprobe -version` work. Install Microsoft Edge for LinkedIn and native website capture, or pass `--edge-executable` with the full path to `msedge.exe`.

The Python dependencies beyond the standard library are Playwright and the Google Drive packages. The recording scripts share local modules, so run them from this directory.

## Local data

Private input belongs outside this repository. The usual local layout is:

```text
data/data.csv                         input rows with source_link
data/proxies.txt                      private proxy endpoints
data/credentials.json                 Google OAuth client secret
data/token.json                       Google OAuth tokens
data/website-videos/                   website recordings
data/linkedin-videos/                 LinkedIn recordings
data/loom-videos/                     Loom recordings
data/combined-videos/                 composed videos
```

Recording input normally needs `source_link`. Composition input also needs `source_video`, `loom_video` or `loom-video`, and optionally `combined_video`, `status`, `first_name`, and `email`. Never add real contact data or recordings to this repository.

For Google Drive, keep OAuth files in a private local `data` directory, authenticate interactively, and pass `--folder-id` for the target folder. Never commit either OAuth file.

## Run and verify

```text
python record_website.py --help
python record_linkedin.py --help
python record_website_forbidden.py --help
python compose_video.py --help
python create_gifs.py --help
python change_drive_permission.py --help
python -m compileall -q .
```

Start with one test row and dry runs:

```text
python record_website.py data/data.csv --row 2
python record_linkedin.py data/data.csv --row 2
python compose_video.py --csv-path data/data.csv --dry-run
python create_gifs.py --dry-run
python change_drive_permission.py --dry-run --folder-id YOUR_FOLDER_ID
```

Never commit credentials, tokens, proxy lists, contact data, source URL exports, browser profiles, recordings, generated media, or local machine configuration. Inspect `git status --short`, `git diff --cached --name-only`, and staged text before publishing.
