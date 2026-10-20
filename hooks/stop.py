#!/usr/bin/env python3
"""Compatibility entry point for barline-hook."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from barline.hook import *

if __name__ == "__main__":
    raise SystemExit(main())
