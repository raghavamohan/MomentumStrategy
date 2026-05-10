"""Compatibility CLI entrypoint.

Prefer ``python -m app.cli_client`` — HTTP client to the local server.
"""

from __future__ import annotations

from app.cli_client import main

if __name__ == "__main__":
    main()
