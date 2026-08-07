"""故障注入：异常输入/降级逻辑"""
import os, tempfile
from pathlib import Path
TEST_ROOT = Path(tempfile.mkdtemp())
os.environ["WIKI_ROOT"] = str(TEST_ROOT)
os.environ["VAULT_ROOT"] = str(TEST_ROOT)
os.environ["WIKI_DB"] = str(TEST_ROOT / "test.db")
os.environ["VEC0_PATH"] = ""
os.environ["EMBED_BASE_URL"] = ""   # 禁用嵌入（不耗额度）
os.environ["RERANK_BASE_URL"] = ""
import server  # noqa: E402

def test_empty_query():
    r = server.search(query="", limit=5)
    assert r["total"] == 0

def test_fts_special_chars():
    """FTS5 特殊字符（引号/括号/星号）不崩"""
    for q in ['"', '("', 'a"b', '()', '*', 'AND', 'OR', 'NEAR', "'"]:
        r = server.search(query=q, limit=3)
        assert "results" in r

def test_super_long_query():
    r = server.search(query="测" * 5000, limit=3)
    assert "results" in r

def test_bad_frontmatter():
    """乱 frontmatter 不崩"""
    (TEST_ROOT / "bad.md").write_text("---\nno closing\n正文", encoding="utf-8")
    server.reindex(full=True)
    db = server.get_db()
    assert db.execute("SELECT 1").fetchone()

def test_embed_disabled_degrade():
    """嵌入禁用时 search 正常（关键词路径）"""
    (TEST_ROOT / "ok.md").write_text("---\ntitle: 收藏夹\ntype: concept\n---\n收藏夹管理方法", encoding="utf-8")
    server.reindex(full=True)  # 先索引再搜（测试独立，不依赖前序副作用）
    r = server.search(query="收藏夹", mode="hybrid", limit=5)
    assert r["total"] >= 1

def test_limit_clamp():
    r = server.search(query="测试", limit=-1)
    assert "results" in r
    r2 = server.search(query="测试", limit=99999)
    assert "results" in r2

def test_get_nonexistent():
    assert "不存在" in server.get(path="不存在的文件.md", max_lines=5)

def test_lint_no_crash_on_empty():
    r = server.lint(path="wiki", limit=5)
    assert "broken" in r
