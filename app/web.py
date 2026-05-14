"""Compatibility shim for the ASGI application.

The canonical module is :mod:`app.server`. Run::

    python -m app.server
    uvicorn app.server:app --host 127.0.0.1 --port 5000
"""

from __future__ import annotations

from app.server import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
