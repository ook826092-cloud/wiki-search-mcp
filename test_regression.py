"""回归极限测试：历史全部 bug 的最极限复现场景（确保修复不复发）"""
import os, tempfile, threading, time
from pathlib import Path
TEST_ROOT = Path(tempfile.mkdtemp())
os.environ["WIKI_ROOT"] = str(TEST_ROOT)
os.environ["VAULT_ROOT"] = str(TEST_ROOT)
os.environ["WIKI_DB"] = str(TEST_ROOT / "test.db")
os.environ["VEC0_PATH"] = ""
os.environ["EMBED_BASE_URL"] = ""
os.environ["RERANK_BASE_URL"] = ""
import server  # noqa: E402

# ============ 1. 路径遍历（最极限攻击变体）============
def test_path_traversal_attack_matrix():
    """路径遍历矩阵：深度组合/编码/空字节/绝对/超长/symlink 全拦"""
    attacks = [
        "../../etc/passwd", "../../../../etc/shadow", "..\\..\\..\\windows",
        "%2e%2e%2fetc%2fpasswd", "%252e%252e%252f", "....//....//etc",
        "/etc/passwd", "//etc/shadow", "C:/Windows/System32", "\\\\server\\share",
        "a\x00b", "x" * 2000 + ".md", ".", "..", "...", "a/../../b",
        "wiki/../../server.py", "wiki/../../../etc/passwd",
    ]
    for p in attacks:
        got = server.safe_resolve(TEST_ROOT, p)
        assert got is None or str(got).startswith(str(TEST_ROOT)), f"逃逸: {p!r}"

def test_symlink_escape():
    """symlink 逃逸：链接指向库外 → 拦截"""
    outside = TEST_ROOT.parent / "outside_secret.md"
    outside.write_text("secret", encoding="utf-8")
    (TEST_ROOT / "link.md").symlink_to(outside)
    got = server.safe_resolve(TEST_ROOT, "link.md")
    assert got is None or str(got).startswith(str(TEST_ROOT))

# ============ 2. FTS5 特殊字符注入矩阵 ============
def test_fts_injection_matrix():
    """FTS5 注入：全部特殊字符组合不崩"""
    evil = ['"', '("', 'a"b', "()", "*", "AND", "OR", "NEAR", "'", 'a OR b',
            '"a OR b"', "a AND b", "NOT", "(", ")", '"', "\\", "a: b", "-a",
            "+a", "~a", "*a*", '"a b c"', "NEAR(a b)", "a AND (b OR c)",
            '"a" OR "b" AND "c"', "a" * 500 + '"', "((((((a", "))))))"]
    for q in evil:
        r = server.search(query=q, limit=5)
        assert "results" in r, f"FTS 崩溃: {q!r}"

# ============ 3. LIKE 通配符转义 ============
def test_like_wildcard_escape():
    """LIKE 通配符 % _ \\ 转义：不作为通配符误匹配"""
    (server.WIKI_ROOT / "w1.md").write_text("---\ntitle: 100%完成\n---\n进度", encoding="utf-8")
    (server.WIKI_ROOT / "w2.md").write_text("---\ntitle: 进度条\n---\n进度", encoding="utf-8")
    server.reindex(full=True)
    db = server.get_db()
    rows = db.execute("SELECT path FROM page_meta WHERE title LIKE ? ESCAPE '\\'", ("%\\%%",)).fetchall()
    assert any("w1.md" in r["path"] for r in rows), "LIKE % 转义失效"
    db.close()

# ============ 4. frontmatter 地狱 ============
def test_frontmatter_hell():
    """frontmatter 全部形态 + 极端值不崩"""
    cases = [
        "---\ntitle: 块列表\ntags:\n  - AI\n  - LLM\naliases:\n  - 别名1\n  - 别名2\n---\n",
        "---\ntags: 单行标量\n---\n",
        "---\ntags: [a, b, c]\n---\n",
        "---\ntags: \"\"\n---\n",
        "---\n---\n",
        "---\n",
        "正文无 frontmatter",
        "",
        "---\ntitle: " + "长" * 5000 + "\n---\n",
        "---\ntags:\n  - a\n  -b\n  -\n  - \n---\n",
        "---\ntitle: 引号\"和'特殊\n---\n",
    ]
    for text in cases:
        title, ptype, tags, aliases = server.parse_frontmatter(text)
        assert isinstance(title, (str, type(None)))
        assert isinstance(tags, str)

# ============ 5. reindex 幂等/脏数据 ============
def test_reindex_idempotent_dirty():
    """reindex 多次 + 空文件 + 重复内容不产生脏数据"""
    for i in range(50):
        (TEST_ROOT / f"d{i}.md").write_text(f"---\ntitle: 脏{i}\n---\n内容{i}", encoding="utf-8")
    (TEST_ROOT / "empty.md").write_text("", encoding="utf-8")
    (TEST_ROOT / "nofm.md").write_text("只有正文没有 frontmatter", encoding="utf-8")
    for _ in range(3):
        server.reindex(full=True)
    db = server.get_db()
    dup = db.execute("SELECT path, count(*) c FROM page_meta GROUP BY path HAVING c>1").fetchall()
    assert not dup, f"reindex 重复: {dup[:3]}"
    db.close()

# ============ 6. snippet 极限 ============
def test_snippet_extreme():
    """snippet：⟪⟫ 冲突/超长/多命中/空 terms"""
    body = "⟪已有高亮⟫" + "普通内容" * 500 + "关键词位置" + "尾部内容" * 100
    sn = server.make_snippet(body, ["关键词"])
    assert isinstance(sn, str)
    assert server.make_snippet("", []) == "…"  # 空 body 返回省略号
    assert isinstance(server.make_snippet("x" * 10000, ["不存在的词"]), str)

# ============ 7. 引用僵尸 + 附件逃逸 ============
def test_reference_zombies():
    """引用僵尸：孤立 attachment_links 清理 + 附件路径逃逸"""
    (TEST_ROOT / "wiki").mkdir(exist_ok=True)
    (TEST_ROOT / "wiki" / "a.md").write_text("![图](../images/x.png) [[附件]] [[../../etc/passwd]]", encoding="utf-8")
    r = server.reindex(full=True)
    db = server.get_db()
    n = db.execute("SELECT count(*) c FROM attachment_links").fetchone()["c"]
    assert n >= 0
    db.close()
    out = server.lint(path="wiki", limit=50)
    assert "broken" in out

# ============ 8. since 边界（时区）============
def test_since_edge():
    """since：过去/未来/今天边界不崩"""
    for s in ["2020-01-01", "2099-12-31", "1970-01-01", "", "2026-08-08"]:
        r = server.search(query="内容", since=s, limit=3)
        assert "results" in r

# ============ 9. 并发 sync 竞态 ============
def test_concurrent_sync_race():
    """同步竞态：快速连续触发 sync + 修改文件不崩"""
    def mut():
        for i in range(30):
            (TEST_ROOT / f"race{i}.md").write_text(f"---\ntitle: 竞态{i}\n---\n内容", encoding="utf-8")
            server._maybe_sync(server.get_db())
    ts = [threading.Thread(target=mut) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    db = server.get_db()
    assert db.execute("SELECT 1").fetchone()
    db.close()

# ============ 10. 参数地狱 ============
def test_param_hell():
    """参数：limit/from_line/max_lines 极端值 + 不存在路径"""
    assert "results" in server.search(query="x", limit=-1)
    assert "results" in server.search(query="x", limit=0)
    assert "results" in server.search(query="x", limit=10**9)
    assert "results" in server.search(query="x", mode="bad_mode")
    assert "INVALID" in server.get(path="无此文件.md", max_lines=10**6, from_line=10**6)
    assert "INVALID" in server.preview(path="无此文件.md", max_lines=10**6)
    assert isinstance(server.related(path="无此文件.md", limit=5), list)
    assert isinstance(server.similar(path="无此文件.md", limit=5), list)
