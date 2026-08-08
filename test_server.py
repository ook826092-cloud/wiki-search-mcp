"""wiki-search 动态测试（单元 + 集成，不依赖外部 API）"""
import os, sqlite3, tempfile, shutil
from pathlib import Path

# 测试环境隔离（临时 vault/db）
TEST_ROOT = Path(tempfile.mkdtemp())
os.environ["WIKI_ROOT"] = str(TEST_ROOT)
os.environ["VAULT_ROOT"] = str(TEST_ROOT)
os.environ["WIKI_DB"] = str(TEST_ROOT / "test.db")
os.environ["VEC0_PATH"] = ""
os.environ["EMBED_BASE_URL"] = ""  # 强制禁用嵌入（零额度消耗）
os.environ["RERANK_BASE_URL"] = ""  # 不加载 vec0

import server  # noqa: E402

def test_parse_frontmatter():
    t = "---\ntitle: 测试页\ntype: concept\ntags: [知识, 管理]\naliases: [测试, 试页]\n---\n正文"
    title, ptype, tags, aliases = server.parse_frontmatter(t)
    assert title == "测试页"
    assert ptype == "concept"
    assert "知识" in tags and "管理" in tags
    assert "测试" in aliases

def test_parse_frontmatter_yaml_list():
    t = "---\ntitle: 列表页\ntags:\n  - AI\n  - LLM\naliases:\n  - 人工智能\n  - 大模型\n---\n"
    title, ptype, tags, aliases = server.parse_frontmatter(t)
    assert "AI" in tags and "LLM" in tags
    assert "人工智能" in aliases

def test_query_nature():
    assert server._query_nature("B站") == "short"
    assert server._query_nature("LLM") == "short"
    assert server._query_nature("怎么把视频整理起来") == "desc"
    assert server._query_nature("收藏夹管理") == "balanced"

def test_safe_resolve_normal():
    f = server.safe_resolve(TEST_ROOT, "a/b.md")
    assert f is not None and f.exists() is False  # 路径安全（文件不存在但校验通过）

def test_safe_resolve_escape():
    assert server.safe_resolve(TEST_ROOT, "../../etc/passwd") is None

def test_make_snippet_highlight():
    body = "这是正文，包含收藏夹管理的内容测试。"
    sn = server.make_snippet(body, ["收藏夹"])
    assert "⟪收藏夹⟫" in sn

def test_tokenize_query():
    fts, like = server.tokenize_query("B站收藏夹")
    assert fts and like

def test_integration_search():
    """集成：建临时库 → 索引 → 检索"""
    (TEST_ROOT / "p1.md").write_text("---\ntitle: 页面一\ntype: concept\n---\n收藏夹管理方法", encoding="utf-8")
    server.reindex(full=True)  # 临时库全量
    db = server.get_db()
    n = db.execute("SELECT count(*) c FROM page_meta").fetchone()["c"]
    assert n >= 1
    # FTS 检索（用 jieba 保证的单词，短语 "收藏夹" 可能被切词不匹配）
    rows = db.execute("SELECT path FROM pages_fts_jieba WHERE pages_fts_jieba MATCH ?", ('"管理"',)).fetchall()
    assert any("p1.md" in r["path"] for r in rows)
    db.close()

def test_integration_lint():
    """集成：断链检测（模板豁免 + 模糊匹配）"""
    (TEST_ROOT / "wiki").mkdir(exist_ok=True)
    (TEST_ROOT / "wiki" / "a.md").write_text("[[存在的页面]] [[不存在的]]", encoding="utf-8")
    (TEST_ROOT / "存在的页面.md").write_text("x", encoding="utf-8")
    r = server.lint(path="wiki", limit=10)
    links = {b["link"] for b in r["broken"]}
    assert "存在的页面" not in links  # 模糊匹配：文件存在不算断链
    assert "不存在的" in links

if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
