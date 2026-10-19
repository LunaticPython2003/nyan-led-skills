"""Compatibility entry point. Prefer: uv run nyan scan"""
import sys
from nyan_pack.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["scan", *sys.argv[1:]]))
