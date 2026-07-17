"""Shortlist engine — a pure library.

The engine never imports from ``shortlist.server``. Every entry point takes plain config
dataclasses plus client instances and returns report objects; the FastAPI service is
the thin adapter over this one engine.
"""
