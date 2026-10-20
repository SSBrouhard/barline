#!/usr/bin/env python3
"""Compatibility entry point for barline-recover."""
import sys

sys.dont_write_bytecode = True
from barline.recovery import *

if __name__ == "__main__":
    raise SystemExit(main())
