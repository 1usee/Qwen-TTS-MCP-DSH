"""``python -m qwen_voice_mcp`` — delegate to the CLI entry point."""

from __future__ import annotations

import sys

from qwen_voice_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
