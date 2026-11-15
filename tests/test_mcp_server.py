"""MCP server end to end: start mcp_server.py over stdio and call each tool like a client would.

    pytest tests/test_mcp_server.py        (skipped when the mcp package is not installed)
"""
import asyncio
import json
import pathlib
import sys

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_hiddenprompt_scan import AT, BODY, PROMPT, pdf  # noqa: E402

SERVER = pathlib.Path(__file__).resolve().parents[1] / "mcp_server.py"


async def _session(fn):
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(SERVER.parent))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return await fn(s)


async def _call(s, tool, **args):
    res = await s.call_tool(tool, args)
    assert not res.is_error, res
    return res.structured_content or json.loads(res.content[0].text)


def test_mcp_tools(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(pdf(lambda d, p: p.insert_text(AT, PROMPT, fontsize=10, color=(1, 1, 1))))
    out = tmp_path / "clean.pdf"

    async def run(s):
        names = {t.name for t in (await s.list_tools()).tools}
        assert names == {"scan_pdf", "read_pdf_safely", "strip_pdf"}
        rep = await _call(s, "scan_pdf", path=str(bad))
        assert rep["verdict"] == "prompt"
        safe = await _call(s, "read_pdf_safely", path=str(bad))
        assert not safe["blocked"] and BODY in safe["text"] and "IGNORE" not in safe["text"]
        blocked = await _call(s, "read_pdf_safely", path=str(bad), block_prompts=True)
        assert blocked["blocked"] and blocked["text"] is None
        stripped = await _call(s, "strip_pdf", path=str(bad), out_path=str(out))
        assert stripped["before"] == "prompt" and stripped["after"] == "clean"
        missing = await s.call_tool("scan_pdf", {"path": str(tmp_path / "nope.pdf")})
        assert missing.is_error

    asyncio.run(_session(run))
    assert out.is_file()
