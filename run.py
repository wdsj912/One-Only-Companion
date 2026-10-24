#!/usr/bin/env python
"""Run the AI Friend server.

Prefer ``python -m app.main``. This entrypoint passes the app object directly
with ``reload=False``: import strings + reload spawn a child process, which
reintroduces the MissingGreenlet failure on Windows.

Usage: python run.py
"""
from __future__ import annotations

import uvicorn

from app.config import settings
from app.main import app


if __name__ == "__main__":
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, reload=False)
