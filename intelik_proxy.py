#!/usr/bin/env python3
"""Compatibility wrapper for the integrated IntelikRoute proxy command."""

from __future__ import annotations

import sys

from intelikroute import main


if __name__ == "__main__":
    raise SystemExit(main(["proxy", *sys.argv[1:]]))
