#!/usr/bin/env python3
"""Entry point: python3 engine/craft_sync.py <validate|plan|sync-app|finalize> ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from craft_suite.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
