"""Smoke-test the stdio MCP contract the way another agent's client would."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py"), "--transport", "stdio"],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            print("server:", init.serverInfo.name, init.serverInfo.version)
            print("tools:", ", ".join(names))
            expected = {"list_voice_models", "synthesize_speech", "clone_voice", "check_balance"}
            missing = expected - set(names)
            if missing:
                print("MISSING:", sorted(missing))
                return 1
            print("STDIO CONTRACT OK")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
