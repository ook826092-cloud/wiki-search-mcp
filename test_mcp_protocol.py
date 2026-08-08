"""MCP 协议级测试：真起 server + StreamableHttp 全链路"""
import subprocess, sys, time, os, tempfile, httpx, pytest
from pathlib import Path

PORT = 8123
URL = f"http://127.0.0.1:{PORT}/mcp"
SRV_DIR = str(Path(__file__).parent)

@pytest.fixture(scope="module")
def mcp_server():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "wiki").mkdir()
    (tmp / "wiki" / "测试页.md").write_text(
        "---\ntitle: 测试页\ntype: concept\ntags: [AI, 收藏夹]\n---\n这是测试内容，收藏夹管理方法。",
        encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "WIKI_ROOT": str(tmp), "VAULT_ROOT": str(tmp),
        "WIKI_DB": str(tmp / "test.db"), "VEC0_PATH": "",
        "EMBED_BASE_URL": "", "RERANK_BASE_URL": "", "LOG_LEVEL": "WARNING",
    })
    p = subprocess.Popen(
        [sys.executable, "-m", "fastmcp", "run", "server.py",
         "--transport", "http", "--port", str(PORT)],
        env=env, cwd=SRV_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(120):  # 最长 60s（jieba 首次加载 + fastmcp 启动可能慢）
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        out, _ = p.communicate(timeout=5)
        p.terminate()
        raise RuntimeError("MCP server 启动失败: " + out.decode(errors="ignore")[-2000:])
    yield
    p.terminate()
    p.wait(timeout=10)

def _session(client):
    r = client.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}})
    sid = r.headers.get("mcp-session-id")
    assert sid, "无 session id"
    client.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={"mcp-session-id": sid})
    return sid

def _call(client, sid, mid, name, args):
    r = client.post(URL, json={"jsonrpc": "2.0", "id": mid, "method": "tools/call",
        "params": {"name": name, "arguments": args}}, headers={"mcp-session-id": sid}, timeout=20)
    return r.json()

def test_tools_list_15(mcp_server):
    with httpx.Client(timeout=10) as c:
        sid = _session(c)
        r = c.post(URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                   headers={"mcp-session-id": sid})
        tools = r.json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert len(tools) >= 15
        assert {"search", "get", "status", "lint", "reindex", "similar"} <= names

def test_search_http(mcp_server):
    with httpx.Client(timeout=20) as c:
        sid = _session(c)
        d = _call(c, sid, 3, "search", {"query": "收藏夹", "limit": 3})
        assert "result" in d, d
        assert "content" in d["result"]

def test_status_lint_reindex_http(mcp_server):
    with httpx.Client(timeout=30) as c:
        sid = _session(c)
        assert "result" in _call(c, sid, 4, "status", {})
        assert "result" in _call(c, sid, 5, "lint", {"path": "wiki", "limit": 3})
        assert "result" in _call(c, sid, 6, "reindex", {"full": True})

def test_bad_tool_http(mcp_server):
    """未知工具 → 错误返回（不崩）"""
    with httpx.Client(timeout=10) as c:
        sid = _session(c)
        d = _call(c, sid, 7, "tools/call_unknown", {})
        assert "error" in d or "result" in d
