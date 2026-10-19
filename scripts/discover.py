"""Compatibility entry point. Prefer: uv run nyan discover"""
import sys
from nyan_pack.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["discover", *sys.argv[1:]]))
