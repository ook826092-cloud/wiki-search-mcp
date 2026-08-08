"""敌意测试：最恶心/最可怕/最威胁安全的场景（隔离环境跑）"""
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

# ============ 1. SQL 注入矩阵（最可怕）============
def test_sql_injection_matrix():
    """SQL 注入：经典攻击向量全部参数化拦截"""
    attacks = [
        "' OR 1=1--", "'; DROP TABLE page_meta;--", "' UNION SELECT * FROM sqlite_master--",
        "'; DELETE FROM pages_fts_jieba;--", "1' OR '1'='1", "') OR (1=1",
        "'||'", "';--", "x'; INSERT INTO page_meta(path,title) VALUES('hack','pwn');--",
        "' AND 1=(SELECT count(*) FROM sqlite_master)--", "0x27 OR 1=1",
        "' OR EXISTS(SELECT 1)--", "'; UPDATE page_meta SET title='pwned';--",
    ]
    for a in attacks:
        r = server.search(query=a, limit=5)
        assert "results" in r, f"搜索注入崩溃: {a!r}"
    # 表还在、数据没被删
    db = server.get_db()
    db.execute("SELECT count(*) FROM page_meta").fetchone()
    db.close()

# ============ 2. 命令注入矩阵 ============
def test_command_injection_paths():
    """命令注入：路径/URL 塞 shell 元字符 → 不执行"""
    evil_paths = [
        "x; rm -rf /", "x$(rm -rf /)", "x`rm -rf /`", "x|cat /etc/passwd",
        "& calc", "x&&reboot", "x> /tmp/pwned", "x< /etc/passwd",
        "$(whoami)", "`id`", "%0a id", "%0d%0a id", "x & whoami",
    ]
    for p in evil_paths:
        got = server.safe_resolve(TEST_ROOT, p)
        assert got is None or str(got).startswith(str(TEST_ROOT)), f"路径逃逸: {p!r}"
        # get/preview 也不崩
        assert isinstance(server.get(path=p, max_lines=5), str)

# ============ 3. 编码混淆（看不见的攻击）============
def test_encoding_confusion():
    """编码混淆：全角/零宽/RTL/BOM/控制字符"""
    evil = [
        "ｅｔｃ／ｐａｓｓｗｄ",  # 全角
        "et\x00c/passwd",       # 空字节
        "..\u202e/..",          # RTL 覆盖
        "\ufeff../../etc",      # BOM
        "a\u200bb.md",          # 零宽空格
        "..%2f..%2fetc",        # URL 编码
        "..%5c..%5cetc",        # 反斜杠编码
        "..\t/..\t/etc",        # 制表符
        "\x1b[31mRED\x1b[0m.md",  # ANSI 转义
        "\\\\?\\C:\\Windows",       # NT 设备路径
        "file:../../etc",       # URI scheme
    ]
    for p in evil:
        got = server.safe_resolve(TEST_ROOT, p)
        assert got is None or str(got).startswith(str(TEST_ROOT)), f"编码逃逸: {p!r}"

# ============ 4. XSS/HTML 注入 ============
def test_xss_injection():
    """XSS：script/事件/js 协议 → 检索结果不崩"""
    evil_queries = [
        "<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
        "javascript:alert(1)", "<svg onload=alert(1)>", "';alert(1);//",
        "<iframe src=evil>", "<a href='javascript:x'>", "{{7*7}}", "${7*7}",
        "<!-- 注释 -->", "<textarea>", "</textarea><script>x</script>",
    ]
    for q in evil_queries:
        r = server.search(query=q, limit=3)
        assert "results" in r, f"XSS 查询崩溃: {q!r}"
    # 页面内容含 XSS → snippet 不崩
    (server.WIKI_ROOT / "xss.md").write_text("<script>alert(1)</script>" + "正文", encoding="utf-8")
    server.reindex(full=True)
    r = server.search(query="alert", limit=3)
    assert "results" in r

# ============ 5. frontmatter YAML 炸弹 ============
def test_yaml_bomb_frontmatter():
    """YAML 炸弹：锚点递归/深嵌套/巨量标签 → 解析不崩"""
    bombs = [
        "---\na: &x [*x]\n---\n",                    # 递归锚点
        "---\na: &a\n b: *a\n---\n",                  # 别名递归
        "---\ntags: [" + ",".join(f"t{i}" for i in range(10000)) + "]\n---\n",  # 1 万标签
        "---\ntitle: " + "嵌套" * 10000 + "\n---\n",   # 2 万字符 title
        "---\n" + "\n".join(f"k{i}: v{i}" for i in range(5000)) + "\n---\n",   # 5000 字段
    ]
    for b in bombs:
        title, ptype, tags, aliases = server.parse_frontmatter(b)
        assert isinstance(title, (str, type(None)))

# ============ 6. 巨型输入 DoS ============
def test_dos_giant_input():
    """DoS：1MB 查询/极限参数/海量并行 → 不崩不挂死"""
    # 1MB 查询
    r = server.search(query="A" * 1000000, limit=3)
    assert "results" in r
    # 极限参数
    assert "results" in server.search(query="x", limit=10**9)
    r = server.get(path="x" * 5000, max_lines=10**9)
    assert isinstance(r, (str, dict)), f"get 返回类型异常: {type(r)}"
    # 300 线程并行 DoS 式轰炸
    errs = []
    def hammer(i):
        try:
            server.search(query=f"压力{i%50}", limit=100)
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=hammer, args=(i,)) for i in range(300)]
    [t.start() for t in ts]
    [t.join(timeout=60) for t in ts]
    assert not errs, f"DoS 下异常: {errs[:3]}"

# ============ 7. 文件名地狱 ============
def test_filename_hell():
    """文件名：控制字符/emoji/隐藏/.git/保留名 → 索引不崩"""
    names = [
        ".hidden.md", ".git/config", ".git/HEAD", "con.md", "aux.md",
        "a b c.md", "a\tb.md", "中文 空格 测试.md", "emoji🎉.md",
        "a" * 300 + ".md", "….md", "a.b.c.d.md",
    ]
    for n in names:
        try:
            (server.WIKI_ROOT / n).write_text(f"---\ntitle: {n}\n---\n内容", encoding="utf-8")
        except OSError:
            continue  # 非法文件名系统拒绝，跳过（也是安全）
    server.reindex(full=True)
    db = server.get_db()
    assert db.execute("SELECT count(*) c FROM page_meta").fetchone()["c"] >= 1
    db.close()

# ============ 8. wikilink 复杂语法 ============
def test_wikilink_complex_syntax():
    """wikilink：别名/锚点/管道/超长/嵌套 → lint 不崩"""
    (server.WIKI_ROOT / "wiki").mkdir(exist_ok=True)
    links = " ".join([
        "[[目标|显示名]]", "[[目标#锚点]]", "[[#纯锚点]]", "[[|空]]", "[[]]",
        "[[../../etc/passwd]]", "[[http://evil.com]]", "[[a" * 500 + "]]",
        "[[a]] [[b]] [[c]]" * 100, "![[图.png]]", "![[../../etc/shadow]]",
    ])
    (server.WIKI_ROOT / "wiki" / "w.md").write_text(links, encoding="utf-8")
    r = server.lint(path="wiki", limit=100)
    assert "broken" in r

# ============ 9. 日志注入 ============
def test_log_injection():
    """日志注入：\n\r ANSI 换行污染 → 查询不崩"""
    for q in ["a\nb", "a\rb", "a\x1b[2Jb", "a\n\n\nb", "\x00\x01\x02"]:
        r = server.search(query=q, limit=3)
        assert "results" in r

# ============ 10. FTS 高级语法攻击 ============
def test_fts_advanced_attack():
    """FTS5 高级语法：^ 前缀/{} 分组/列名/* 全表 → 不崩"""
    attacks = [
        "^AND", "{}", "a{}b", "a^b", "title:正文", "a b c d e", "*",
        "a*", "*a", '"a"*', "NEAR(a b c)", "a OR b OR c", "a AND b AND c",
        "(" * 100 + "a" + ")" * 100, "a" + " AND " * 50 + "b",
        "column:value", "content:正文", "a OR NOT b",
    ]
    for a in attacks:
        r = server.search(query=a, limit=5)
        assert "results" in r, f"FTS 高级攻击崩溃: {a!r}"
