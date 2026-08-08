"""极限测试：一次性拉满（10万页/256线程/1万查询/50MB/5000模糊）"""
import os, tempfile, threading, time, statistics, random, string
from pathlib import Path
TEST_ROOT = Path(tempfile.mkdtemp())
os.environ["WIKI_ROOT"] = str(TEST_ROOT)
os.environ["VAULT_ROOT"] = str(TEST_ROOT)
os.environ["WIKI_DB"] = str(TEST_ROOT / "test.db")
os.environ["VEC0_PATH"] = ""
os.environ["EMBED_BASE_URL"] = ""   # 强制禁用嵌入（零额度消耗）
os.environ["RERANK_BASE_URL"] = ""
import server  # noqa: E402

def test_bulk_reindex_100000_pages():
    """🏆 10 万页全量索引：正确性 + 性能报告"""
    t0 = time.time()
    for i in range(100000):
        (TEST_ROOT / f"p{i:05}.md").write_text(
            f"---\ntitle: 页面{i}\ntype: concept\ntags: [压力]\n---\n压力测试内容 {i} 号收藏夹管理方法",
            encoding="utf-8")
    tw = time.time() - t0
    t0 = time.time()
    server.reindex(full=True)
    dt = time.time() - t0
    db = server.get_db()
    n = db.execute("SELECT count(*) c FROM page_meta").fetchone()["c"]
    size = os.path.getsize(os.environ["WIKI_DB"]) / 1e6
    db.close()
    print(f"\n🏆 索引 100000 页: 写入 {tw:.0f}s + 索引 {dt:.0f}s（{100000/dt:.0f} 页/s），DB {size:.1f}MB")
    assert n >= 100000, f"只索引到 {n} 页"
    r = server.search(query="收藏夹", limit=50)
    assert r["total"] >= 100000, f"只搜到 {r['total']}"

def test_benchmark_query_10000():
    """🏆 1 万次查询压测：p50/p95/p99 + 最大 QPS"""
    lat = []
    for _ in range(10000):
        t0 = time.time()
        server.search(query="压力测试", limit=10)
        lat.append((time.time() - t0) * 1000)
    lat.sort()
    p50, p95, p99 = lat[5000], lat[9500], lat[9900]
    qps = 1000 / (sum(lat) / len(lat))
    print(f"\n🏆 10000 次查询: p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms 吞吐={qps:.0f} QPS")
    assert p99 < 5000, f"p99 太慢: {p99:.0f}ms"

def test_concurrency_256():
    """🏆 256 线程并发搜索轰炸"""
    errs = []
    def worker(i):
        try:
            for _ in range(5):
                r = server.search(query=f"页面{i % 100000}", limit=5)
                assert "results" in r
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(256)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, f"并发异常: {errs[:3]}"

def test_huge_page_50mb():
    """🏆 50MB 巨页：reindex 不崩"""
    (TEST_ROOT / "huge.md").write_text("---\ntitle: 巨页\n---\n" + "内容" * 17500000, encoding="utf-8")
    server.reindex(full=True)
    r = server.search(query="巨页", limit=3)
    assert r["total"] >= 1

def test_fuzz_5000():
    """🏆 5000 个随机模糊查询不崩"""
    random.seed(42)
    chars = string.printable + "收藏夹管理B站视频人工智能测试极限"
    for _ in range(5000):
        q = "".join(random.choice(chars) for _ in range(random.randint(0, 50)))
        r = server.search(query=q, limit=3)
        assert "results" in r

def test_extreme_path_1000():
    """🏆 1000 字符路径/攻击变体全拦"""
    bad = ["../../etc/passwd", "//etc/shadow", "a" * 1000 + ".md", "..\\..\\win",
           "x\x00y.md", "/abs/path.md", "%2e%2e%2fetc", "....//....//etc/passwd"]
    for p in bad:
        got = server.safe_resolve(TEST_ROOT, p)
        assert got is None or str(got).startswith(str(TEST_ROOT)), p

def test_limit_billion():
    """🏆 limit=10^9 边界钳制不崩"""
    r = server.search(query="测试", limit=10**9)
    assert "results" in r

def test_wikilink_10000():
    """🏆 单页 1 万 wikilink：lint 不崩"""
    (TEST_ROOT / "wiki").mkdir(exist_ok=True)
    links = " ".join(f"[[链接{i}]]" for i in range(10000))
    (TEST_ROOT / "wiki" / "att.md").write_text(links, encoding="utf-8")
    r = server.lint(path="wiki", limit=100)
    assert "broken" in r

def test_reindex_idempotent():
    """🏆 幂等：reindex 两次无重复"""
    server.reindex(full=True)
    server.reindex(full=True)
    db = server.get_db()
    dup = db.execute("SELECT path, count(*) c FROM page_meta GROUP BY path HAVING c>1").fetchall()
    db.close()
    assert not dup, f"重复页: {dup[:3]}"
