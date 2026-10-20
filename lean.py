#!/usr/bin/env python3
"""Compatibility entry point. New installations provide the barline command."""
import sys

sys.dont_write_bytecode = True
from barline.core import *  # Preserve source-checkout imports for existing users.

if __name__ == "__main__":
    raise SystemExit(main())
