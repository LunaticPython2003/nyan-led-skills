"""Compatibility entry point. Prefer: uv run nyan show"""
import sys
from nyan_pack.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["show", *sys.argv[1:]]))
