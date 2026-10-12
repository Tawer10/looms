# Loom recording tools

These scripts record website and LinkedIn walkthroughs, compose recordings, create GIFs, and inspect Google Drive permissions.

## Privacy

The public repository intentionally contains source code only. Real recordings, contact exports, source links, proxy lists, OAuth credentials, refresh tokens, and generated media stay in the private working directory and are excluded by `.gitignore`.

## Local setup

Use Python 3.11 or newer and install the dependencies required by the script you plan to run. Supply private paths through command line arguments. Keep OAuth files outside version control.

Examples:

```text
python record_website.py --help
python record_linkedin.py --help
python compose_video.py --help
python create_gifs.py --help
python change_drive_permission.py --dry-run --help
```

The scripts expect local input data when performing real work. No real personal data is included in this repository.

## Verification

```text
python -m compileall -q .
git status --short
git ls-files
```
