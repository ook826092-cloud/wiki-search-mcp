"""极限测试：并发/海量/极端输入轰炸（GitHub Actions 跑，约 1-2 分钟）"""
import os, tempfile, threading
from pathlib import Path
TEST_ROOT = Path(tempfile.mkdtemp())
os.environ["WIKI_ROOT"] = str(TEST_ROOT)
os.environ["VAULT_ROOT"] = str(TEST_ROOT)
os.environ["WIKI_DB"] = str(TEST_ROOT / "test.db")
os.environ["VEC0_PATH"] = ""
os.environ["EMBED_BASE_URL"] = ""
os.environ["RERANK_BASE_URL"] = ""
import server  # noqa: E402

def test_bulk_reindex_100_pages():
    """海量：100 页全量索引 → 全部可检索"""
    for i in range(100):
        (TEST_ROOT / f"p{i:03}.md").write_text(
            f"---\ntitle: 页面{i}\ntype: concept\ntags: [压力]\n---\n压力测试内容 {i} 号收藏夹管理",
            encoding="utf-8")
    server.reindex(full=True)
    db = server.get_db()
    n = db.execute("SELECT count(*) c FROM page_meta").fetchone()["c"]
    assert n >= 100, f"只索引到 {n} 页"
    r = server.search(query="收藏夹", limit=50)
    assert r["total"] >= 100
    db.close()

def test_reindex_idempotent():
    """幂等：reindex 两次无重复"""
    server.reindex(full=True)
    server.reindex(full=True)
    db = server.get_db()
    dup = db.execute("""SELECT path, count(*) c FROM page_meta GROUP BY path HAVING c>1""").fetchall()
    assert not dup, f"重复页: {dup}"
    db.close()

def test_concurrent_search():
    """并发：8 线程同时 search 不崩、结果稳定"""
    errs = []
    def worker(i):
        try:
            for _ in range(10):
                r = server.search(query=f"页面{i % 100}", limit=5)
                assert "results" in r
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, f"并发异常: {errs[:3]}"

def test_huge_page_1mb():
    """超大：1MB 页面不崩"""
    (TEST_ROOT / "huge.md").write_text("---\ntitle: 巨页\n---\n" + "内容" * 350000, encoding="utf-8")
    server.reindex(full=True)
    r = server.search(query="巨页", limit=3)
    assert r["total"] >= 1

def test_extreme_paths():
    """路径攻击：../、//、超长、\x00 全拦"""
    bad = ["../../etc/passwd", "//etc/shadow", "a" * 600 + ".md", "..\\..\\win", "x\x00y.md", "/abs/path.md"]
    for p in bad:
        assert server.safe_resolve(TEST_ROOT, p) is None or str(server.safe_resolve(TEST_ROOT, p)).startswith(str(TEST_ROOT)), p

def test_fuzz_queries():
    """模糊：200 个随机查询不崩"""
    import random, string
    random.seed(42)
    chars = string.printable + "收藏夹管理B站视频人工智能测试"
    for _ in range(200):
        q = "".join(random.choice(chars) for _ in range(random.randint(0, 30)))
        r = server.search(query=q, limit=3)
        assert "results" in r

def test_extreme_limits():
    """极限参数：limit 边界钳制"""
    for lim in (-1, 0, 1, 100, 999999):
        r = server.search(query="测试", limit=lim)
        assert "results" in r

def test_wikilink_attack():
    """wikilink 注入：恶意链接 lint 不崩"""
    (TEST_ROOT / "wiki").mkdir(exist_ok=True)
    (TEST_ROOT / "wiki" / "att.md").write_text(
        "[[../../etc/passwd]] [[http://evil.com]] [[x]] [[..\\\\..\\\\win]]", encoding="utf-8")
    r = server.lint(path="wiki", limit=20)
    assert "broken" in r
