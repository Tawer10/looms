# Repository instructions

Keep this repository limited to reusable source code and documentation. Never commit credentials, tokens, proxy lists, contact data, source URL exports, browser profiles, recordings, generated media, or local machine configuration.

Before publishing, inspect the staged file list and scan staged text for secrets, personal data, private URLs, and local paths. Use synthetic values for tests and examples.

Use Python 3.11 or newer. Preserve the existing command line interfaces. After edits, run `python -m compileall -q .` and the narrowest available validation or dry run.
