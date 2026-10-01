"""Vercel entrypoint: Vercel's Python runtime serves the ASGI `app` found here."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter.main import app  # noqa: E402,F401
