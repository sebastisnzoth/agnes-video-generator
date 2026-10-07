"""Explicit Vercel ASGI entrypoint for Agnes Video Generator.

Vercel imports this module and serves the FastAPI application as an ASGI function.
Runtime state is initialized lazily here because the original application lifespan
is intentionally disabled for Vercel's ASGI adapter.
"""
from server import app
from web.app_state import init_runtime_state

# Vercel's filesystem is ephemeral, but /tmp is writable for the lifetime of a
# function instance. Initialize only the directories needed by API handlers.
try:
    init_runtime_state()
except Exception:
    # Do not prevent the ASGI app from booting; individual endpoints will return
    # their normal application errors if initialization cannot be completed.
    pass

__all__ = ["app"]
