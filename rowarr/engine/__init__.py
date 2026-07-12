"""Rowarr engine — a pure library.

The engine never imports from ``rowarr.server``. Every entry point takes plain config
dataclasses plus client instances and returns report objects; the CLI and the FastAPI
service are two thin adapters over this one engine.
"""
