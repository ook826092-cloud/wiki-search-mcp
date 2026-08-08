#!/usr/bin/env python3
"""wiki-search MCP Server — SQLite FTS5 关键词 + 向量语义 混合检索
嵌入/重排模型全部可配置（环境变量），支持任意 OpenAI 兼容接口。
零硬编码模型绑定：EMBED_* / RERANK_* 环境变量控制。
"""
import os, re, sqlite3, time, json, logging, threading, urllib.request, sys
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Optional, List
from fastmcp import FastMCP
import jieba

# ---------- 嵌入/重排配置（环境变量，不硬编码）----------
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")      # 如 https://xxx/compatible-mode/v1
EMBED_API_KEY   = os.environ.get("EMBED_API_KEY", "")
EMBED_MODEL     = os.environ.get("EMBED_MODEL", "")        # 如 qwen3.7-text-embedding
def _int_env(k, dflt):
    """建议16: 环境变量整数解析（空/非法值回退默认，不崩溃）"""
    v = os.environ.get(k, "")
    try: return int(v) if v else dflt
    except ValueError:
        print(f"[wiki-search] WARNING: 环境变量 {k}={v!r} 非法，使用默认 {dflt}", file=sys.stderr)
        return dflt

EMBED_DIM       = _int_env("EMBED_DIM", 1024)
EMBED_BATCH     = _int_env("EMBED_BATCH", 8)
RERANK_BASE_URL = os.environ.get("RERANK_BASE_URL", "")    # 留空 = 禁用重排
RERANK_API_KEY   = os.environ.get("RERANK_API_KEY", "")
RERANK_MODEL     = os.environ.get("RERANK_MODEL", "")
RERANK_FORMAT    = os.environ.get("RERANK_FORMAT", "dashscope")  # openai / dashscope
RERANK_URL       = os.environ.get("RERANK_URL", "")        # 完整 rerank 端点（可选，覆盖 base 拼接）
VEC0_PATH        = os.environ.get("VEC0_PATH", str(Path.home() / "wiki-search" / "vec0.so"))

EMBED_ENABLED = bool(EMBED_BASE_URL and EMBED_API_KEY and EMBED_MODEL)
RERANK_ENABLED = bool(RERANK_BASE_URL and RERANK_API_KEY and RERANK_MODEL)

# ---------- jieba 分词配置 ----------
for _w in ("B站", "小红书", "抖音", "快手", "GitHub", "MCP", "RAG", "Obsidian",
           "YouTube", "Kimi", "DeepSeek", "LLM", "知识库", "收藏夹", "搜索引擎"):
    jieba.add_word(_w)
STOPWORDS = {"的", "了", "和", "是", "在", "有", "怎么", "如何", "什么", "一个",
             "可以", "这个", "那个", "以及", "与", "或", "都", "也", "很", "我",
             "你", "他", "它", "我们", "你们", "对", "为", "从", "把", "被", "就",
             "然后", "这样", "那样", "关于", "对于", "因为", "所以"}

def _jieba_seg(text: str) -> str:
    # 搜索引擎模式（cut_for_search）：多粒度分词，短词/部分词也能命中，召回率显著提升（实测 0% 漏召回 vs 精确 10-12%）
    words = [w for w in jieba.lcut_for_search(text) if w.strip() and not re.fullmatch(r"[\W_]+", w)]
    return " ".join(words)

# ---------- 路径配置 ----------
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/path/to/obsidian/vault"))  # 脱敏：默认改通用路径，实际用环境变量
WIKI_ROOT  = Path(os.environ.get("WIKI_ROOT", str(VAULT_ROOT)))
DB_PATH    = Path(os.environ.get("WIKI_DB", str(Path.home() / "wiki-search" / "wiki.db")))

MAX_INDEX_SIZE = 1024 * 1024
MAX_LINES_HARD = 5000
FTS_LIMIT_MULT = 4
FILTER_LIMIT_MULT = 3
SNIPPET_RADIUS = 50
VEC_TOP_K = 20          # 向量召回数

ATTACH_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic",
    ".heif", ".avif", ".tiff", ".pdf", ".epub", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".txt", ".csv", ".mp3", ".mp4", ".mov", ".mkv", ".wav",
    ".m4a", ".flac", ".aac", ".ogg", ".webm", ".zip", ".gz", ".tar", ".7z",
    ".json", ".html", ".excalidraw", ".canvas",
}
ALLOWED_TABLES = {"pages_fts", "attachments_fts"}

ENABLE_TRIGRAM = os.environ.get("ENABLE_TRIGRAM", "0") == "1"
SCHEMA_VERSION = "3"

BM25_WEIGHTS = {
    "pages_fts": "8.0, 6.0, 1.0, 2.0",
    "pages_fts_jieba": "8.0, 6.0, 1.0, 2.0",
    "attachments_fts": "6.0, 1.0",
    "attachments_fts_jieba": "6.0, 1.0",
}

_sync_lock = threading.Lock()
_sync_thread = None  # #2 后台同步线程（懒同步不阻塞查询）
_embed_lock = threading.Lock()  # 嵌入计数线程锁
_embed_tokens_used = 0  # 本进程累计嵌入 tokens（每次成功批次落库 meta，防重启丢失）

# 同义词/别名扩展：搜索时互相补全
SYNONYMS = {
    "卡片盒": "Zettelkasten", "Zettelkasten": "卡片盒",
    "知识库": "知识管理", "知识管理": "知识库",
    "收藏夹": "书签", "书签": "收藏夹",
    "LLM": "大模型", "大模型": "LLM",
    "RAG": "检索增强", "检索增强": "RAG",
    "MCP": "模型上下文协议", "模型上下文协议": "MCP",
}
for _k, _v in SYNONYMS.items():  # 同义词加入 jieba 词典（防二次切碎）
    jieba.add_word(_k); jieba.add_word(_v)

def _persist_embed_usage(total: int):
    """把累计嵌入 tokens 写入 meta（锁冲突重试；失败记录 error，不再静默）"""
    for _attempt in range(4):  # 后台 reindex 长事务持锁时等待重试
        try:
            db = sqlite3.connect(str(DB_PATH), timeout=5)  # busy_timeout=5s 等锁
            try:
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embed_tokens_used',?)",
                           (json.dumps(total),))
                db.commit()
            finally:
                db.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                time.sleep(0.5)
                continue
            logger.error("embed usage 落库失败: %s", e)  # #5 监控告警
            return
        except Exception as e:
            logger.error("embed usage 落库失败: %s", e)  # #5 监控告警
            return
    logger.error("embed usage 落库失败(重试耗尽): database busy")

@lru_cache(maxsize=256)
def _jieba_cached(q: str) -> tuple:
    return tuple(jieba.lcut_for_search(q))  # 与索引分词一致（搜索引擎模式）

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE  = os.environ.get("LOG_FILE", str(Path.home() / "wiki-search" / "wiki-search.log"))

# 日志：控制台 + 滚动文件（1MB × 3），级别可配
import logging.handlers as _lh
_handlers: list = [logging.StreamHandler()]
try:
    _fh = _lh.RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")  # 优化：日志 5MB×3
    _handlers.append(_fh)
except Exception as _e:
    # 日志文件创建失败不静默（至少 stderr 可见）
    print(f"[wiki-search] WARNING: 日志文件创建失败({LOG_FILE}): {_e}", file=sys.stderr)
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=_handlers)
logger = logging.getLogger("wiki-search")

mcp = FastMCP("wiki-search")
VAULT_NAME = WIKI_ROOT.resolve().name  # 防止尾斜杠导致空名

def _obsidian_url(rel_path: str) -> str:
    from urllib.parse import quote
    return f"obsidian://open?vault={quote(VAULT_NAME)}&file={quote(rel_path, safe='/')}"

# ---------- 数据库 ----------
_schema_initialized = False
_schema_lock = threading.Lock()  # 初始化加锁防并发重复 DDL

def _check_vec_dim(db):
    """检查 pages_vec 表维度与 EMBED_DIM 是否一致（不一致提示 full reindex）"""
    if not EMBED_ENABLED:
        return
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE name='pages_vec'").fetchone()
        if row and row[0]:
            m = re.search(r"float\[(\d+)\]", row[0])
            if m and int(m.group(1)) != EMBED_DIM:
                logger.warning("EMBED_DIM=%d 与 pages_vec 表维度 %s 不一致！请 reindex(full=True) 重建向量表",
                               EMBED_DIM, m.group(1))
    except sqlite3.Error:
        pass

def get_db():
    global _schema_initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.isolation_level = None
    # vec0 是 SQLite 连接级扩展——每个连接都必须加载（不能只在首次）
    if Path(VEC0_PATH).exists():
        try:
            db.enable_load_extension(True)
            db.load_extension(str(VEC0_PATH))
        except Exception as e:
            logger.warning("vec0 加载失败(无语义检索): %s", e)
    if not _schema_initialized:
        with _schema_lock:  # 防止多线程同时初始化
            if not _schema_initialized:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                init_schema(db)
                _schema_initialized = True
    # EMBED 启用时：确保 pages_vec 存在（表缺失自动建）+ 维度一致性检查
    if EMBED_ENABLED:
        try:
            db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS pages_vec USING vec0(
                embedding float[{EMBED_DIM}], path TEXT)""")
            _check_vec_dim(db)
        except sqlite3.Error as e:
            logger.warning("pages_vec 检查失败: %s", e)
    return db

def init_schema(db):
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts_jieba USING fts5(
        path, title, body, tags, tokenize='unicode61')""")
    if ENABLE_TRIGRAM:
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            path, title, body, tags, tokenize='trigram')""")
    db.execute("""CREATE TABLE IF NOT EXISTS page_meta(
        path TEXT PRIMARY KEY, title TEXT, page_type TEXT, tags TEXT, aliases TEXT,
        size INT, updated REAL)""")
    if ENABLE_TRIGRAM:
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
            path, filename, tokenize='trigram')""")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts_jieba USING fts5(
        path, filename, tokenize='unicode61')""")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS attachment_content_fts USING fts5(
        path, body, tokenize='unicode61')""")
    db.execute("""CREATE TABLE IF NOT EXISTS attachments(
        path TEXT PRIMARY KEY, filename TEXT, ext TEXT, size INT, updated REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS attachment_links(
        page_path TEXT, att_path TEXT, PRIMARY KEY(page_path, att_path))""")
    db.execute("""CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)""")
    if EMBED_ENABLED:
        try:
            db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS pages_vec USING vec0(
                embedding float[{EMBED_DIM}], path TEXT)""")
        except Exception as e:
            logger.warning("pages_vec 建表失败: %s", e)
    # 迁移：page_meta 补 aliases 列（旧库升级）
    try:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(page_meta)")]
        if "aliases" not in cols:
            db.execute("ALTER TABLE page_meta ADD COLUMN aliases TEXT")
    except sqlite3.Error:
        pass

# ---------- 嵌入 API（OpenAI 兼容，可配置）----------
# 嵌入并发（按模型自动降级）：显式设置优先；free 模型低并发防 429；常规模型（阿里云 v4 1800RPM）高并发提速
_ec_raw = os.environ.get("EMBED_CONCURRENCY", "")
if _ec_raw:
    EMBED_CONCURRENCY = int(_ec_raw)
elif "free" in EMBED_MODEL.lower():
    EMBED_CONCURRENCY = 1   # OpenRouter free：低并发防 429
else:
    EMBED_CONCURRENCY = 4   # 阿里云 v4 等常规模型：限流 1800RPM 可承受

def embed_texts(texts: List[str]) -> List[Optional[List[float]]]:  # 类型注解精确化
    """调用可配置的 OpenAI 兼容嵌入接口。
    返回与输入**等长**的列表，失败位填 None（保证索引对齐，杜绝向量错位）。
    #3 并发：多线程同时发多个批次（EMBED_CONCURRENCY），大幅提速 full reindex"""
    if not EMBED_ENABLED:
        return [None] * len(texts)
    out: List[Optional[List[float]]] = [None] * len(texts)
    any_fail = False

    def _work(start: int):
        """单批嵌入，返回 (start, vecs 或 None)"""
        batch = texts[start:start + EMBED_BATCH]
        body = json.dumps({"model": EMBED_MODEL, "input": batch,
                           "dimensions": EMBED_DIM}).encode()  # 显式传维度，杜绝默认维度漂移
        for attempt in range(3):  # 总 3 次尝试（初始+2重试），timeout 20s 控制最坏耗时
            try:
                req = urllib.request.Request(
                    EMBED_BASE_URL.rstrip("/") + "/embeddings",
                    data=body, headers={"Authorization": "Bearer " + EMBED_API_KEY,
                                        "Content-Type": "application/json"})
                d = json.loads(urllib.request.urlopen(req, timeout=20).read()) # nosec B310
                batch_vecs = [x["embedding"] for x in d["data"]]
                if len(batch_vecs) != len(batch):  # 数量防御：异常 provider 返回数不匹配
                    logger.warning("嵌入返回数量 %d != 请求 %d，视为失败", len(batch_vecs), len(batch))
                    return start, None
                bad = {len(v) for v in batch_vecs if len(v) != EMBED_DIM}
                if bad:
                    logger.warning("嵌入维度异常 %s（期望 %d），尝试 %d/3", bad, EMBED_DIM, attempt + 1)  # 计数修正
                    time.sleep(2)
                    continue
                global _embed_tokens_used
                tok = (d.get("usage") or {}).get("total_tokens", 0) or 0
                with _embed_lock:  # 线程安全累加
                    _embed_tokens_used += tok
                return start, batch_vecs
            except Exception as e:
                if attempt == 2:
                    logger.warning("嵌入批次失败: %s", e)
                    return start, None
                time.sleep(2)
        return start, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=EMBED_CONCURRENCY) as ex:
        futures = [ex.submit(_work, i) for i in range(0, len(texts), EMBED_BATCH)]
        for f in futures:
            start, vecs = f.result()
            if vecs:
                out[start:start + len(vecs)] = vecs  # 按位置写回，失败位保持 None
            else:
                any_fail = True
    if _embed_tokens_used:
        _persist_embed_usage(_embed_tokens_used)
    if any_fail:
        logger.warning("部分嵌入批次失败，失败位已填 None（保持索引对齐）")
    return out

# ---------- 重排 API（可配置格式）----------
def rerank(query: str, documents: List[str]) -> Optional[List[int]]:
    """重排文档，返回按相关性降序的索引列表。RERANK_ENABLED 才执行。"""
    if not RERANK_ENABLED or not documents:
        return None
    # 格式自动判断：URL 以 /rerank 结尾（OpenAI 兼容）时强制 openai，避免 dashscope URL 误判
    fmt = "openai" if (RERANK_URL and RERANK_URL.rstrip("/").endswith("/rerank")) else RERANK_FORMAT
    url = RERANK_URL or (RERANK_BASE_URL.rstrip("/") + "/api/v1/services/rerank/text-rerank/text-rerank")
    body: dict
    if fmt == "openai":
        body = {"model": RERANK_MODEL, "query": query, "documents": documents}
        url = RERANK_URL or (RERANK_BASE_URL.rstrip("/") + "/rerank")
    else:  # dashscope 原生
        body = {"model": RERANK_MODEL, "input": {"query": query, "documents": documents},
                "parameters": {"top_n": len(documents)}}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + RERANK_API_KEY, "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())  # 重排超时收紧 # nosec B310
        if fmt == "openai":  # 用 fmt（URL 自动判断后的格式），而非原始 RERANK_FORMAT
            results = d.get("results", [])
        else:
            results = d.get("output", {}).get("results", [])
        scored = sorted(((r.get("index"), r.get("relevance_score", 0)) for r in results),
                        key=lambda x: -x[1])
        return [i for i, _ in scored]
    except Exception as e:
        logger.warning("重排失败: %s", e)
        return None

# ---------- 安全 ----------
def safe_resolve(root: Path, rel: str) -> Optional[Path]:
    try:
        target = (root / rel).resolve()
        # 防超长路径：单段 >200 或总长 >2000 直接拒绝（is_file/stat 会抛 ENAMETOOLONG）
        if len(str(target)) > 2000 or any(len(p) > 200 for p in target.parts):
            return None
        target.relative_to(root.resolve())
        return target  # 返回 resolve 后的规范路径（安全且一致）
    except (ValueError, OSError):
        return None

# ---------- 解析 ----------
def parse_frontmatter(text: str):
    title, ptype, tags, aliases = None, None, "", ""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if m:
        fm = m.group(1)
        t = re.search(r"^title:[ \t]*[\"']?(.*?)[\"']?[ \t]*$", fm, re.M)
        if t and t.group(1).strip(): title = t.group(1).strip()
        else:
            tm = re.search(r"^title:[ \t]*\|?[ \t]*\r?\n((?:[ \t]{2,}.*(?:\r?\n)?)+)", fm, re.M)
            if tm: title = " ".join(l.strip() for l in tm.group(1).splitlines() if l.strip())
        p = re.search(r"^type:[ \t]*(\w+)", fm, re.M)
        if p: ptype = p.group(1)
        g = re.search(r"^tags:[ \t]*\[(.*?)\]", fm, re.M)
        if g: tags = g.group(1)
        else:
            gm = re.search(r"^tags:[ \t]*[\"']?(.*?)[\"']?[ \t]*$", fm, re.M)
            if gm and gm.group(1).strip(): tags = gm.group(1)
            else:
                gl = re.findall(r"^tags:.*\r?\n((?:[ \t]+-\s*\S+.*(?:\r?\n)?)+)", fm, re.M)
                if gl: tags = " ".join(re.findall(r"-\s*(\S+)", gl[0]))
        a = re.search(r"^aliases:\s*\[(.*?)\]", fm, re.M)
        if a: aliases = a.group(1)
        else:
            al = re.findall(r"^aliases:.*\r?\n((?:[ \t]+-\s*\S+.*(?:\r?\n)?)+)", fm, re.M)
            if al: aliases = " ".join(re.findall(r"-\s*(\S+)", al[0]))
    return title, ptype, tags, aliases

def cjk_chunks(s: str):
    return re.findall(
        r"[\u2E80-\u9FFF\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFFEF"
        r"\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF\uAC00-\uD7AF\u3400-\u4DBF"
        r"\U00020000-\U0002FFFF\U00030000-\U0003FFFF]+", s)  # 含 CJK 扩展区生僻字

def tokenize_query(query: str):
    fts_terms, like_terms = [], []
    for seg in cjk_chunks(query):
        if len(seg) >= 3:
            if len(seg) <= 6: fts_terms.append(seg)
            for i in range(len(seg) - 2): fts_terms.append(seg[i:i + 3])
        else: like_terms.append(seg)
    for w in re.findall(r"[a-zA-Z0-9_\-]+", query):
        if len(w) >= 3: fts_terms.append(w.lower())
        else: like_terms.append(w)
    return list(dict.fromkeys(fts_terms)), list(dict.fromkeys(like_terms))

def _highlight(seg: str, terms: list) -> str:
    for term in dict.fromkeys(t for t in terms if len(t) >= 2):
        parts = re.split(r"(⟪[^⟫]*⟫)", seg)
        parts = [re.sub(re.escape(term), lambda m: "⟪" + m.group(0) + "⟫", p, flags=re.I)
                 if not p.startswith("⟪") else p for p in parts]
        seg = "".join(parts)
    return seg

def make_snippet(text: str, terms: list, radius: int = SNIPPET_RADIUS) -> str:
    body = text
    m = re.match(r"^---\r?\n.*?\r?\n---\r?\n", text, re.S)
    if m: body = text[m.end():]
    if not terms: return body[:150].replace("\n", " ") + "…"
    low = body.lower()
    # 定位最靠前的命中词（优化：多词取最前）
    best_idx, best_term = -1, None
    for term in terms:
        if len(term) < 2: continue
        idx = low.find(term.lower())
        if idx >= 0 and (best_idx < 0 or idx < best_idx):
            best_idx, best_term = idx, term
    if best_idx < 0:
        return body[:150].replace("\n", " ") + "…"
    assert best_term is not None # nosec B101
    start = max(0, best_idx - radius); end = min(len(body), best_idx + len(best_term) + radius)
    seg = body[start:end].replace("\n", " ")
    seg = _highlight(seg, terms)  # 优化：段内所有命中词高亮（不只定位词）
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""  # 到文末不加省略号
    return prefix + seg + suffix

def first_heading(text: str) -> Optional[str]:
    m = re.search(r"^\s*#\s+(.+)$", text, re.M)  # 兼容带前导空格的标题
    return m.group(1).strip() if m else None

def set_meta(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, json.dumps(v)))

def get_meta(db, k, default=None):
    r = db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    if not r: return default
    try: return json.loads(r["value"])
    except (ValueError, TypeError, json.JSONDecodeError): return default

# ---------- 懒同步 ----------
_last_sync_check = 0.0

def _iter_md_files():
    """#4 os.scandir 递归遍历 .md（比 Path.rglob 快 2-3 倍），跳过隐藏目录"""
    stack = [WIKI_ROOT]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith("."):
                                stack.append(Path(e.path))
                        elif e.name.endswith(".md"):
                            yield Path(e.path)
                    except OSError:
                        continue
        except OSError:
            continue

def _maybe_sync(db):
    global _last_sync_check
    now = time.time()
    if now - _last_sync_check < 60: return
    if not _sync_lock.acquire(blocking=False): return
    try:
        _last_sync_check = now
        if not WIKI_ROOT.exists():  # 目录不存在直接跳过，不抛异常
            logger.warning("WIKI_ROOT 不存在: %s", WIKI_ROOT)
            return
        page_mtimes = get_meta(db, "page_mtimes", {})
        changed = False
        # #4 并行 stat 加速：16 线程 + scandir 遍历（1774 文件 ~0.3s）
        from concurrent.futures import ThreadPoolExecutor
        def _stat(f):
            try: return str(f.relative_to(WIKI_ROOT)), f.stat().st_mtime
            except (OSError, ValueError): return None  # 含 symlink 路径异常
        files = _iter_md_files()  # 直接传生成器（不 list 物化，省内存）
        with ThreadPoolExecutor(max_workers=16) as ex:
            for res in ex.map(_stat, files):
                if res is None: continue
                rel, mt = res
                if page_mtimes.get(rel) != mt:
                    changed = True
                    break
        if changed:
            logger.info("检测到 vault 变化，后台增量同步索引...")
            _spawn_sync_thread()  # #2 后台线程执行 reindex，不阻塞当前查询
    finally:
        _sync_lock.release()

def _spawn_sync_thread():
    """后台线程执行 reindex（懒同步不阻塞 search 等查询）"""
    global _sync_thread
    with _sync_lock:  # 严重2: 检查+赋值原子化，防并发双线程
        if _sync_thread is not None and _sync_thread.is_alive():
            logger.info("后台同步已在运行，跳过本次触发")
            return
        def _worker():
            try:
                reindex(full=False)
                logger.info("后台增量同步完成")
            except Exception as e:
                logger.exception("后台同步失败: %s", e)
        _sync_thread = threading.Thread(target=_worker)  # 非 daemon：退出时等待同步完成
        _sync_thread.start()

# ---------- 检索核心 ----------
_IS_TEMPLATE = lambda rel: rel.startswith(("wiki/Welcome", "wiki/欢迎", "wiki/schema/"))  # 模块级模板判定

def _query_nature(query: str) -> str:
    """判断查询性质：short(短词/专有名词) / desc(描述句) / balanced(均衡)。
    用于 hybrid 融合的动态权重（短词关键词主导，描述句语义主导）"""
    q = query.strip()
    if not q: return "balanced"
    n = len(q)
    has_special = bool(re.search(r"[A-Z0-9\-\.]", q))  # 大写/数字/符号 = 专有名词特征
    has_desc = bool(re.search(r"(怎么|如何|什么|为什么|哪些|有没有|是不是|了解|整理|管理|搭建|使用|学习)", q))
    words = [w for w in _jieba_cached(q) if len(w) >= 2]
    if n <= 4 or (n <= 8 and has_special):
        return "short"   # 短词/专有名词（B站、LLM、MCP）→ 关键词主导
    if n >= 8 and (has_desc or len(words) >= 3):
        return "desc"    # 描述句（怎么整理视频）→ 语义主导
    return "balanced"

def _search_table(db, table: str, query: str, limit: int):
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table: {table}")
    jtable = table + "_jieba"
    results: dict = {}
    def add(k, norm, kind):
        if k not in results or norm > results[k][0]:
            results[k] = (norm, kind)
    words = [w for w in _jieba_cached(query) if w.strip() and w not in STOPWORDS]
    good = [w for w in words if len(w) >= 2]
    if good:
        terms = [w.translate(str.maketrans("", "", "*()-+:^")) for w in good[:8]]  # 严重3: 剔除 FTS5 特殊字符
        terms = [w for w in terms if w]
        if not terms:
            return results
        for expr, weight, kind in ((" ".join('"%s"' % w for w in terms), 10.0, "jieba"),
                                   (" OR ".join('"%s"' % w for w in terms), 5.0, "jieba_or")):
            try:
                rows = db.execute(
                    f"SELECT path, bm25({jtable}, {BM25_WEIGHTS[jtable]}) AS rank FROM {jtable} " # nosec B608
                    f"WHERE {jtable} MATCH ? ORDER BY rank LIMIT ?",
                    (expr, limit * FTS_LIMIT_MULT))
                for row in rows:
                    add(row["path"], -float(row["rank"]) + weight, kind)
            except sqlite3.Error:
                break
    fts_terms, like_terms = tokenize_query(query)
    if ENABLE_TRIGRAM and fts_terms:
        expr = " OR ".join('"%s"' % t.replace('"', "") for t in fts_terms)  # 过滤引号防 FTS 语法错误
        try:
            rows = db.execute(
                f"SELECT path, bm25({table}, {BM25_WEIGHTS[table]}) AS rank FROM {table} " # nosec B608
                f"WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
                (expr, limit * FTS_LIMIT_MULT))
            for row in rows:
                add(row["path"], -float(row["rank"]), "fts")
        except sqlite3.Error: pass
    for t in like_terms:
        t_esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # 转义 SQL 通配符
        pat = "%" + t_esc + "%"
        # LIKE 后备查原始内容表（jieba 分词列短语匹配失效）
        try:
            if table == "pages_fts":
                rows = db.execute(
                    "SELECT path FROM page_meta WHERE path LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' LIMIT 20",
                    (pat, pat))
            else:
                rows = db.execute(
                    "SELECT path FROM attachments WHERE path LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\' LIMIT 20",
                    (pat, pat))
            for row in rows:
                add(row["path"], 0.1, "like")
        except sqlite3.Error:
            pass
    return results

def _vector_search(db, query: str, limit: int) -> dict:
    """语义检索：查询嵌入 → vec0 KNN。返回 {path: score}（score=-distance，越小越近排序正确）"""
    if not EMBED_ENABLED:
        return {}
    try:
        vec = embed_texts([query])
        if not vec or vec[0] is None:
            return {}
        rows = db.execute(
            "SELECT path, distance FROM pages_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (json.dumps(vec[0]), limit))
        # vec0 默认 L2 距离（1024 维通常 >1），用 -distance：越小越近 → 负值越大（score 越高）
        return {r["path"]: -r["distance"] for r in rows}
    except sqlite3.Error as e:
        if "no such table" in str(e):
            logger.warning("pages_vec 表不存在，请 reindex(full=True) 建立向量索引")
        else:
            logger.warning("向量检索 SQL 失败: %s", e)
        return {}
    except Exception as e:
        logger.warning("向量检索失败: %s", e)
        return {}

RERANK_TOP = 15         # 重排只处理 top N 候选（控制延迟）

def _rerank_paths(query: str, paths: List[str]) -> List[str]:
    """对候选 path 重排（只重排前 RERANK_TOP 条，失败返回原顺序）"""
    if not RERANK_ENABLED or len(paths) < 2:
        return paths
    docs, valid = [], []
    for p in paths[:RERANK_TOP]:
        f = WIKI_ROOT / p
        try:
            with f.open("r", encoding="utf-8", errors="ignore") as fh:  # 只读前 500 字符，不全量加载
                docs.append(fh.read(500))
            valid.append(p)
        except Exception: # nosec B110
            pass
    if not valid: return paths
    order = rerank(query, docs)
    if order is None: return paths
    ranked = [valid[i] for i in order if i < len(valid)]
    return ranked + [p for p in paths if p not in ranked]

# ---------- 索引构建 ----------
@mcp.tool()
def reindex(full: bool = False) -> dict:
    """重建索引。默认增量（只处理变化的文件）；full=True 全量重建（DROP 后重建全部表，含向量化）。""",
    """嵌入模型按环境变量配置（EMBED_*），未配置时自动跳过向量化。"""
    if not WIKI_ROOT.exists():  # 手动调用时目录缺失不崩溃
        return {"error": "WIKI_ROOT 不存在: " + str(WIKI_ROOT)}
    with closing(get_db()) as db:
        page_mtimes = get_meta(db, "page_mtimes", {})
        att_mtimes = get_meta(db, "att_mtimes", {})
        updated_pages = set()
        all_pages = []
        embedded: set = set()   # 向量化成功集合（提前初始化，防止 pages_data 空时 NameError）
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        _reindex_t0 = time.time()
        db.execute("BEGIN")
        try:
            if full:
                for t in ("pages_fts", "attachments_fts", "pages_fts_jieba",
                          "attachments_fts_jieba", "attachment_content_fts"):
                    db.execute(f"DROP TABLE IF EXISTS {t}")
                db.execute("DROP TABLE IF EXISTS pages_vec")  # 无条件清残留（即使嵌入当前禁用）
                init_schema(db)
                for t in ("page_meta", "attachments", "attachment_links"):
                    db.execute(f"DELETE FROM {t}") # nosec B608
                db.execute("DELETE FROM meta WHERE key IN ('page_mtimes','att_mtimes')")
                page_mtimes, att_mtimes = {}, {}
            # --- 页面（企业级分批 flush：每 5000 页 commit，支持百万量级）---
            pages_upd = 0
            pages_data, meta_data = [], []
            REINDEX_FLUSH = 5000

            def _flush_pages_batch(pd: list, md: list):
                """分批写库并 commit，返回空列表（调用方重新赋值）。pd/md 参数化避免 nonlocal 语法限制。"""
                if not pd:
                    return [], []
                # 增量前先 DELETE（防 FTS5 重复记录）
                for rel, *_ in pd:
                    try: db.execute("DELETE FROM pages_fts WHERE path=?", (rel,))
                    except sqlite3.Error: pass
                    db.execute("DELETE FROM pages_fts_jieba WHERE path=?", (rel,))
                if ENABLE_TRIGRAM:
                    db.executemany("INSERT INTO pages_fts(path,title,body,tags) VALUES(?,?,?,?)", pd)
                db.executemany("INSERT INTO pages_fts_jieba(path,title,body,tags) VALUES(?,?,?,?)",
                               [(rel, _jieba_seg(title), _jieba_seg(text) + (" " + aliases.replace(",", " ") if aliases else ""), tags)
                                for rel, title, text, tags in pd])
                db.executemany("INSERT OR REPLACE INTO page_meta(path,title,page_type,tags,aliases,size,updated) VALUES(?,?,?,?,?,?,?)", md)
                if EMBED_ENABLED and pd:
                    try:
                        texts = [text[:800] for _, _, text, _ in pd]
                        vecs = embed_texts(texts)  # 等长，失败位 None
                        for i, (rel, _, _, _) in enumerate(pd):
                            v = vecs[i] if i < len(vecs) else None
                            db.execute("DELETE FROM pages_vec WHERE path=?", (rel,))
                            if v is not None:
                                db.execute("INSERT OR REPLACE INTO pages_vec(path,embedding) VALUES(?,?)", (rel, json.dumps(v)))
                    except Exception as e:
                        logger.warning("向量化分批失败（跳过该批）: %s", e)
                db.commit()
                return [], []

            for f in WIKI_ROOT.rglob("*.md"):
                try: st = f.stat()
                except OSError: continue
                mt = st.st_mtime
                try: rel = str(f.relative_to(WIKI_ROOT))
                except ValueError: continue  # symlink 逃逸跳过
                if full: all_pages.append(rel)
                if not full and page_mtimes.get(rel) == mt: continue
                try: text = f.read_text(encoding="utf-8", errors="ignore")
                except (OSError, UnicodeDecodeError): continue
                if len(text) > MAX_INDEX_SIZE: text = text[:MAX_INDEX_SIZE]
                title, ptype, tags, aliases = parse_frontmatter(text)
                title = title or first_heading(text) or f.stem
                pages_data.append((rel, title, text, tags))
                meta_data.append((rel, title, ptype or "note", tags, aliases, st.st_size, mt))
                page_mtimes[rel] = mt; pages_upd += 1; updated_pages.add(rel)
                if len(pages_data) >= REINDEX_FLUSH:
                    pages_data, meta_data = _flush_pages_batch(pages_data, meta_data)
            for rel in (k for k in page_mtimes if not (WIKI_ROOT / k).exists()):  # 生成器省内存
                try: db.execute("DELETE FROM pages_fts WHERE path=?", (rel,))
                except sqlite3.Error: pass
                db.execute("DELETE FROM pages_fts_jieba WHERE path=?", (rel,))
                db.execute("DELETE FROM page_meta WHERE path=?", (rel,))
                db.execute("DELETE FROM attachment_links WHERE page_path=?", (rel,))  # 清理引用僵尸
                if EMBED_ENABLED:
                    try: db.execute("DELETE FROM pages_vec WHERE path=?", (rel,))
                    except sqlite3.Error: pass
                page_mtimes.pop(rel); updated_pages.discard(rel)
            pages_data, meta_data = _flush_pages_batch(pages_data, meta_data)  # 尾部剩余批次
            # --- 附件 ---
            atts_upd = 0
            atts_data, atts_fts_data, atts_content = [], [], []
            DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
                        ".txt", ".csv", ".html", ".htm", ".epub", ".md"}
            # #1 内容转换跳过缓存（失败/空文本文档 1h 内不重试，避免懒同步重复 markitdown）
            att_content_skip = get_meta(db, "att_content_skip", {}) if not full else {}
            if att_content_skip:
                # 清理 >24h 过期条目（防永久失败文件无限累积）
                _now = time.time()
                att_content_skip = {k: v for k, v in att_content_skip.items() if _now - v < 86400}
            _md = None
            try:
                from markitdown import MarkItDown
                _md = MarkItDown()  # 实例化提到循环外
            except Exception: # nosec B110
                pass
            for f in VAULT_ROOT.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in ATTACH_EXTS: continue
                if any(p.startswith(".") for p in f.relative_to(VAULT_ROOT).parts[:-1]): continue
                try: st = f.stat()
                except OSError: continue
                mt = st.st_mtime
                try: rel = str(f.relative_to(VAULT_ROOT))
                except ValueError: continue  # symlink 逃逸跳过
                if not full and att_mtimes.get(rel) == mt: continue
                atts_data.append((rel, f.name, f.suffix.lower(), st.st_size, mt))
                atts_fts_data.append((rel, f.name)); att_mtimes[rel] = mt; atts_upd += 1
                # 文档类附件：markitdown 提取文本 → 内容索引
                if f.suffix.lower() in DOC_EXTS and _md is not None:
                    if rel in att_content_skip and time.time() - att_content_skip[rel] < 3600:
                        continue  # 1h 内跳过（失败/空文本，防每次懒同步重试）
                    try:
                        t = _md.convert(str(f)).text_content or ""
                        if t.strip():
                            atts_content.append((rel, _jieba_seg(t[:MAX_INDEX_SIZE])))
                            att_content_skip.pop(rel, None)
                        else:
                            att_content_skip[rel] = time.time()  # 空文本（如扫描件无文本层）跳过
                    except Exception:
                        att_content_skip[rel] = time.time()  # 转换失败跳过
            for rel in (k for k in att_mtimes if not (VAULT_ROOT / k).exists()):  # 生成器省内存
                try: db.execute("DELETE FROM attachments_fts WHERE path=?", (rel,))
                except sqlite3.Error: pass
                db.execute("DELETE FROM attachments_fts_jieba WHERE path=?", (rel,))
                db.execute("DELETE FROM attachment_content_fts WHERE path=?", (rel,))
                db.execute("DELETE FROM attachment_links WHERE att_path=?", (rel,))  # 清理引用僵尸
                db.execute("DELETE FROM attachments WHERE path=?", (rel,)); att_mtimes.pop(rel)
            if atts_data:
                # 增量前先 DELETE（防 FTS5 重复）
                for rel, *_ in atts_fts_data:
                    try: db.execute("DELETE FROM attachments_fts WHERE path=?", (rel,))
                    except sqlite3.Error: pass
                    db.execute("DELETE FROM attachments_fts_jieba WHERE path=?", (rel,))
                if ENABLE_TRIGRAM:
                    db.executemany("INSERT INTO attachments_fts(path,filename) VALUES(?,?)", atts_fts_data)
                db.executemany("INSERT INTO attachments_fts_jieba(path,filename) VALUES(?,?)",
                               [(rel, _jieba_seg(fname)) for rel, fname in atts_fts_data])
                db.executemany("INSERT OR REPLACE INTO attachments(path,filename,ext,size,updated) VALUES(?,?,?,?,?)", atts_data)
            if atts_content:
                for rel, _ in atts_content:
                    try:  # 异常保护（防虚拟表损坏导致整个事务回滚）
                        db.execute("DELETE FROM attachment_content_fts WHERE path=?", (rel,))
                    except sqlite3.Error:
                        pass
                db.executemany("INSERT INTO attachment_content_fts(path,body) VALUES(?,?)", atts_content)
                logger.info("附件内容索引: %d 个文档", len(atts_content))
            # --- 引用关系 ---
            links = 0
            link_sources = list(updated_pages) if not full else all_pages
            if full: db.execute("DELETE FROM attachment_links")
            # 附件引用模式：![[]] / ![]() / [[]]（含相对路径、无扩展名、大小写变体）
            pat = re.compile(
                r"!?\[\[([^\]|#]+(?:\.[A-Za-z0-9]{1,15})?)\]\]"   # 长扩展名(.canvas/.excalidraw)
                r"|!\[[^\]]*\]\(([^)]+)\)")                       # ![](path)
            for pref in link_sources:
                f = WIKI_ROOT / pref
                try: text = f.read_text(encoding="utf-8", errors="ignore")
                except (OSError, UnicodeDecodeError): continue
                db.execute("DELETE FROM attachment_links WHERE page_path=?", (pref,))
                for m in pat.finditer(text):
                    ref = (m.group(1) or m.group(2) or "").strip()
                    if not ref: continue
                    ref = ref.split("#")[0].split("?")[0]
                    found = _resolve_attachment(db, f, ref)
                    if found:
                        db.execute("INSERT OR IGNORE INTO attachment_links(page_path,att_path) VALUES(?,?)", (pref, found))
                        links += 1
            # #13 同义词自动学习：从 aliases 学"别名→标题词"映射（全量重建，基于 page_meta）
            learned: dict = {}
            for r in db.execute("SELECT title, aliases FROM page_meta WHERE aliases != ''"):
                title_words = [w for w in _jieba_cached(r["title"]) if len(w) >= 2] if r["title"] else []
                if not title_words: continue
                for a in re.split(r"[,，\s]+", r["aliases"] or ""):
                    if a: learned.setdefault(a, " ".join(title_words))
            # 优化：同义词轻量扩充——概念/实体页标题 ↔ 正文高频词（提升描述性查询召回）
            for r in db.execute("SELECT path, title FROM page_meta WHERE page_type IN ('concept','entity') AND title != ''"):
                f = WIKI_ROOT / r["path"]
                if not f.exists(): continue
                try:
                    t = f.read_text(encoding="utf-8", errors="ignore")[:2000]
                    words = [w for w in _jieba_cached(t) if len(w) >= 2 and w not in STOPWORDS]
                    cnt: dict = {}
                    for w in words: cnt[w] = cnt.get(w, 0) + 1
                    top = [w for w, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:5]]
                    tw = [w for w in _jieba_cached(r["title"]) if len(w) >= 2]
                    if tw and top:
                        learned.setdefault(" ".join(tw), " ".join(top))
                except Exception: # nosec B110
                    pass
            set_meta(db, "page_mtimes", {str(k): v for k, v in page_mtimes.items()})
            set_meta(db, "att_mtimes", {str(k): v for k, v in att_mtimes.items()})
            set_meta(db, "att_content_skip", {str(k): v for k, v in att_content_skip.items()})  # #1 转换跳过缓存
            set_meta(db, "learned_synonyms", learned)  # #13 自动学习同义词
            set_meta(db, "last_reindex", now_str)
            set_meta(db, "schema_version", SCHEMA_VERSION)
            set_meta(db, "embed_model", EMBED_MODEL if EMBED_ENABLED else "")
            set_meta(db, "rerank_model", RERANK_MODEL if RERANK_ENABLED else "")
            db.commit()
            # 优化：full 重建后 checkpoint 压缩 WAL（峰值可达 15MB+）
            try:
                if full:
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            logger.info("reindex %s 完成: 页=%d 附件=%d 链接=%d 向量=%d 耗时=%.0fs",
                        "full" if full else "incremental", pages_upd, atts_upd, links,
                        len(embedded) if EMBED_ENABLED else -1, time.time() - _reindex_t0)
        except Exception:
            try:  # rollback 保护：无活跃事务时不让 OperationalError 覆盖原始异常
                db.rollback()
            except sqlite3.OperationalError:
                pass
            logger.exception("reindex failed")
            raise
    return {"mode": "full" if full else "incremental",
            "pages_indexed": pages_upd, "attachments_indexed": atts_upd,
            "links_found": links, "last_reindex": now_str}

def _resolve_attachment(db, page_file: Path, ref: str) -> Optional[str]:
    """解析附件引用（增强：相对路径 + 无扩展名 + 大小写宽容 + 目录回溯）"""
    name = Path(ref).name
    candidates = []
    # 1. 页面所在目录（相对路径）
    candidates.append(page_file.parent / ref)
    # 2. vault 根
    candidates.append(VAULT_ROOT / ref)
    # 3. 无扩展名 → 尝试常见扩展名
    if not Path(ref).suffix:
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf"):
            candidates.append(page_file.parent / (ref + ext))
            candidates.append(VAULT_ROOT / (ref + ext))
    for p in candidates:
        try:
            if p.exists() and p.suffix.lower() in ATTACH_EXTS:
                return str(p.relative_to(VAULT_ROOT))
        except ValueError:
            continue
    # 4. 文件名精确匹配（忽略大小写）
    row = db.execute("SELECT path FROM attachments WHERE lower(filename)=lower(?)", (name,)).fetchone()
    if row: return row["path"]
    # 5. 文件名模糊（不含扩展名匹配）
    stem = Path(name).stem
    if stem:
        row = db.execute("SELECT path FROM attachments WHERE lower(path) LIKE ? OR lower(filename) LIKE ? LIMIT 1",
                         (f"%{stem.lower()}%", f"%{stem.lower()}%")).fetchone()
        if row: return row["path"]
    return None

# ---------- 检索工具 ----------
@mcp.tool()
def search(query: str, limit: int = 10, page_type: str = "", tags: str = "",
           mode: str = "hybrid", since: str = "", group_by: str = "") -> dict:
    """混合检索笔记（核心工具）。
    mode: hybrid(默认, 关键词+向量语义RRF融合) / keyword(纯关键词) / semantic(纯语义)。
    page_type: 类型过滤，多值逗号分隔（concept,entity）；tags: 标签过滤，多值 AND。
    since: YYYY-MM-DD 只查该日期后更新；group_by="dir" 按顶层目录聚合。
    返回 {results: [...], total: 总命中数}。"""
    if not query or not query.strip():  # 建议11: 空查询提前返回
        return {"results": [], "total": 0}
    fts_terms, like_terms = tokenize_query(query)
    if mode not in ("hybrid", "keyword", "semantic"):
        mode = "hybrid"  # 非法 mode 回退默认
    if mode == "semantic" and not EMBED_ENABLED:
        return {"error": "语义检索需配置 EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL 环境变量", "total": 0}
    # 同义词扩展：jieba 词级匹配，避免子串误触发
    query_ext = query
    _qwords = set(_jieba_cached(query))
    for k, v in SYNONYMS.items():
        if k in _qwords and v not in _qwords:
            query_ext += " " + v
    # snippet 只保留 jieba 完整词（避免 3-gram 干扰高亮/定位）
    snippet_terms = [w for w in _jieba_cached(query) if len(w) >= 2]
    _t0 = time.time()
    since_ts = None
    if since:
        try:  # naive datetime 按本地时区解析（与 st_mtime 语义一致，防 UTC 偏移 8h 漏查）
            import datetime as _dt
            since_ts = _dt.datetime.strptime(since, "%Y-%m-%d").timestamp()
        except ValueError:
            logger.warning("since 参数解析失败: %r，时间过滤已禁用", since)  # 不再静默
            since_ts = None
    pt_list = [p.strip() for p in page_type.split(",") if p.strip()] if page_type else []
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []  # tags 多值
    limit = max(1, min(limit, 50))  # limit 校验提前（防负切片）
    eff_limit = limit * FILTER_LIMIT_MULT if (page_type or tags or since) else limit
    with closing(get_db()) as db:
        _maybe_sync(db)
        # #13 同义词自动学习：别名分词后与查询词有交集即触发（3.4 多词别名匹配修正）
        learned = get_meta(db, "learned_synonyms", {})
        _qw = set(_jieba_cached(query_ext))
        for a, t in learned.items():
            if t in _qw: continue
            aw = set(_jieba_cached(a))
            if aw & _qw:  # 别名分词与查询词有交集（"卡片盒笔记法" 切碎后也能命中）
                query_ext += " " + t
        # RRF 融合（关键词 + 语义 rank 融合，量纲一致，语义真正参与排序）
        kw_rank, sv_rank = {}, {}
        real_total = None  # 关键词路真实命中数（FTS count，不截断，用于 total 语义修正）
        if mode in ("hybrid", "keyword"):
            kw = _search_table(db, "pages_fts", query_ext, eff_limit)
            kw_rank = {p: i + 1 for i, p in enumerate(sorted(kw.keys(), key=lambda p: -kw[p][0]))}
            try:
                # total 用原始查询词（不含同义词扩展——扩展词会 OR 膨胀 total）
                _qw2 = [w for w in _jieba_cached(query) if len(w) >= 2]
                if _qw2:
                    _expr = " OR ".join('"%s"' % w.replace('"', "") for w in _qw2)
                    real_total = db.execute(
                        "SELECT count(*) c FROM pages_fts_jieba WHERE pages_fts_jieba MATCH ?",
                        (_expr,)).fetchone()["c"]
                else:
                    real_total = None
            except Exception:
                real_total = None
        if mode in ("hybrid", "semantic"):
            sv = _vector_search(db, query, VEC_TOP_K)
            sv_rank = {p: i + 1 for i, p in enumerate(sorted(sv.keys(), key=lambda p: -sv[p]))}
        # 查询类型感知动态权重（short 关键词主导 / desc 语义主导 / balanced 均衡 RRF）
        nature = _query_nature(query)
        w_kw, w_sv = {"short": (2.0, 0.5), "desc": (0.5, 2.0), "balanced": (1.0, 1.0)}[nature]
        cand = {}
        for p in set(kw_rank) | set(sv_rank):
            s = 0.0
            if p in kw_rank: s += w_kw / (60 + kw_rank[p])
            if p in sv_rank: s += w_sv / (60 + sv_rank[p])
            cand[p] = s
        # 兜底链：主导路不足 → 另一路结果进候选
        if nature == "short" and kw_rank and len(kw_rank) < 3:
            for p in sv_rank: cand.setdefault(p, 0.0)
        elif nature == "desc" and sv_rank and len(sv_rank) < 3:
            for p in kw_rank: cand.setdefault(p, 0.0)
        logger.debug("hybrid nature=%s w_kw=%.1f w_sv=%.1f", nature, w_kw, w_sv)
        # 重排（hybrid/semantic 且启用时，只对 top 候选精排）
        paths = [p for p, _ in sorted(cand.items(), key=lambda kv: -kv[1])[:eff_limit]]
        if mode in ("hybrid", "semantic") and RERANK_ENABLED:
            paths = _rerank_paths(query, paths)
        # 过滤（page_type 多值 / tags / since）并统计 total
        filtered = []
        for path in paths:
            meta = db.execute("SELECT title,page_type,tags,updated FROM page_meta WHERE path=?", (path,)).fetchone()
            if not meta: continue
            if pt_list and meta["page_type"] not in pt_list: continue
            if tag_list:  # 标签集合精确匹配（"ai" 不匹配 "ai-ethics"）
                tag_set = set(re.split(r"[,，\s]+", meta["tags"] or ""))
                if not tag_set.issuperset(tag_list): continue
            if since_ts is not None and (meta["updated"] or 0) < since_ts: continue
            filtered.append((path, meta))
        total = len(filtered)
        if real_total is not None and not (pt_list or tag_list or since):
            total = real_total  # 无过滤时 total = 真实命中数（不被 limit 截断）
        out = []
        for path, meta in filtered[:limit]:
            snippet = ""
            f_ = safe_resolve(WIKI_ROOT, path)
            if f_:
                try:
                    # #2 流式读前 256KB（不全量加载大文件，命中词通常在文档前部）
                    with f_.open("r", encoding="utf-8", errors="ignore") as fh:
                        head = fh.read(262144)
                    snippet = make_snippet(head, snippet_terms)
                except (OSError, UnicodeDecodeError): snippet = ""
            out.append({"path": path, "abs_path": str(WIKI_ROOT / path),
                        "obsidian_url": _obsidian_url(path),
                        "title": meta["title"], "page_type": meta["page_type"],
                        "score": round(cand.get(path, 0.0), 3),
                        "match_kind": "hybrid" if mode == "hybrid" else mode,
                        "snippet": snippet})
        max_score = max((o["score"] for o in out), default=0.0)
        if max_score > 0:
            for o in out: o["score"] = round(o["score"] / max_score, 3)
        logger.info("search | q=%r mode=%s limit=%d 命中=%d total=%d 耗时=%.0fms rerank=%s",
                    query, mode, limit, len(out), total, (time.time() - _t0) * 1000,
                    "on" if (mode in ("hybrid", "semantic") and RERANK_ENABLED) else "off")
    # #11 结果聚合（group_by="dir" 按顶层目录分组）
    if group_by == "dir" and out:
        groups: dict = {}
        for o in out:
            top = o["path"].split("/")[0] if "/" in o["path"] else "(根)"
            groups.setdefault(top, []).append(o["path"])
        return {"results": out, "total": total, "groups": groups}
    return {"results": out, "total": total}

@mcp.tool()
def similar(path: str, limit: int = 5) -> list:
    """语义相似推荐：基于向量嵌入，找与指定笔记语义最接近的其它笔记（知识探索用）。"""
    limit = max(1, min(limit, 50))  # 参数校验
    if not EMBED_ENABLED:
        return [{"error": "嵌入未配置（EMBED_BASE_URL/KEY/MODEL）"}]
    f = safe_resolve(WIKI_ROOT, path)
    if not f or not f.is_file():
        return [{"error": "INVALID PATH: " + path}]
    text = f.read_text(encoding="utf-8", errors="ignore")[:800]
    with closing(get_db()) as db:
        vec = embed_texts([text])
        if not vec or vec[0] is None:
            return [{"error": "嵌入失败"}]
        rows = db.execute(
            "SELECT path, distance FROM pages_vec WHERE path != ? AND embedding MATCH ? ORDER BY distance LIMIT ?",
            (path, json.dumps(vec[0]), limit))
        out = []
        for r in rows:
            meta = db.execute("SELECT title FROM page_meta WHERE path=?", (r["path"],)).fetchone()
            out.append({"path": r["path"], "title": meta["title"] if meta else r["path"],
                        "distance": round(r["distance"], 3),
                        "obsidian_url": _obsidian_url(r["path"])})
    return out

@mcp.tool()
def get(path: str, max_lines: int = 200, from_line: int = 0) -> str:
    """读取笔记全文（markdown），用于精读检索命中的笔记。
    参数: path=笔记相对路径(必填); max_lines=最多行数(默认200); from_line=起始行(默认0)"""
    f = safe_resolve(WIKI_ROOT, path)
    if not f or not f.is_file(): return "INVALID PATH: " + path
    from_line = max(0, from_line)
    max_lines = max(1, min(max_lines, MAX_LINES_HARD))
    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[from_line:from_line + max_lines])

@mcp.tool()
def preview(path: str, max_lines: int = 30) -> str:
    """快速预览笔记开头（frontmatter + 前几行），用于快速判断相关性，省 token。
    参数: path=笔记相对路径(必填); max_lines=行数(默认30)"""
    f = safe_resolve(WIKI_ROOT, path)
    if not f or not f.is_file(): return "INVALID PATH: " + path
    max_lines = max(1, min(max_lines, MAX_LINES_HARD))
    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[:max_lines])

@mcp.tool()
def search_attachment(query: str, ext: str = "", limit: int = 10) -> list:
    """检索附件：按文件名/路径/文档内容（PDF/Office 正文）检索，返回附件位置和引用它的页面。
    参数: query=检索词(必填); ext=扩展名过滤如png/pdf(可选); limit=条数(默认10)"""
    limit = max(1, min(limit, 50))  # 参数校验
    with closing(get_db()) as db:
        _maybe_sync(db)
        results = _search_table(db, "attachments_fts", query, limit)
        # 内容索引补充：附件正文（PDF/Office 等文档的 markitdown 文本）
        try:
            words = [w for w in _jieba_cached(query) if len(w) >= 2]
            if words:
                expr = " OR ".join('"%s"' % w for w in words[:8])
                rows = db.execute(
                    "SELECT path, bm25(attachment_content_fts, 6.0, 1.0) AS rank "
                    "FROM attachment_content_fts WHERE attachment_content_fts MATCH ? ORDER BY rank LIMIT ?",
                    (expr, limit * 2))
                for row in rows:
                    if row["path"] not in results:
                        results[row["path"]] = (5.0 - (float(row["rank"]) if row["rank"] else 0), "content")
                    else:  # 内容得分覆盖文件名低分
                        s = 5.0 - (float(row["rank"]) if row["rank"] else 0)
                        if s > results[row["path"]][0]:
                            results[row["path"]] = (s, "content")
        except sqlite3.Error:
            pass
        out = []
        for path, (score, kind) in sorted(results.items(), key=lambda kv: kv[1][0], reverse=True):
            a = db.execute("SELECT filename,ext,size FROM attachments WHERE path=?", (path,)).fetchone()
            if not a: continue
            if ext and a["ext"].lstrip(".") != ext.lstrip(".").lower(): continue
            refs = [r["page_path"] for r in db.execute(
                "SELECT page_path FROM attachment_links WHERE att_path=?", (path,))]
            out.append({"path": path, "abs_path": str(VAULT_ROOT / path),
                        "obsidian_url": _obsidian_url(path),
                        "filename": a["filename"], "ext": a["ext"], "size": a["size"],
                        "score": round(score, 3), "match_kind": kind, "referenced_by": refs[:5]})
            if len(out) >= limit: break
        max_score = max((o["score"] for o in out), default=0.0)
        if max_score > 0:
            for o in out: o["score"] = round(o["score"] / max_score, 3)
    return out

@mcp.tool()
def get_attachment(path: str) -> dict:
    """获取附件线索：实际位置 + Obsidian 链接 + 引用它的页面（结构化返回）。
    参数: path=附件相对路径(必填)"""
    f = safe_resolve(VAULT_ROOT, path)
    if not f or not f.is_file(): return {"error": "INVALID PATH: " + path}
    with closing(get_db()) as db:
        refs = [r["page_path"] for r in db.execute(
            "SELECT page_path FROM attachment_links WHERE att_path=?", (path,))]
    try: size = f.stat().st_size
    except OSError: size = 0
    return {"path": path, "abs_path": str(VAULT_ROOT / path), "filename": f.name,
            "size": size, "obsidian_url": _obsidian_url(path), "referenced_by": refs}

@mcp.tool()
def page_attachments(path: str) -> list:
    """列出指定笔记引用的附件清单。
    参数: path=笔记相对路径(必填)"""
    f = safe_resolve(WIKI_ROOT, path)
    if not f or not f.is_file(): return [{"error": "INVALID PATH: " + path}]
    rel = str(f.relative_to(WIKI_ROOT.resolve()))  # f 是 resolve 后路径，root 对齐
    with closing(get_db()) as db:
        out = []
        for r in db.execute("SELECT att_path FROM attachment_links WHERE page_path=?", (rel,)):
            a = db.execute("SELECT filename,ext,size FROM attachments WHERE path=?", (r["att_path"],)).fetchone()
            if a:
                out.append({"path": r["att_path"], "filename": a["filename"], "ext": a["ext"], "size": a["size"]})
    return out

@mcp.tool()
def list_pages(page_type: str = "", tags: str = "") -> list:
    """列出索引中的笔记，可按类型/标签过滤（用于了解知识库全貌）。
    参数: page_type=类型过滤 concept/entity/note(可选); tags=标签过滤(可选)"""
    with closing(get_db()) as db:
        pt_list = [p.strip() for p in page_type.split(",") if p.strip()] if page_type else []
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []  # tags 多值
        # 标签集合精确匹配（与 search 语义一致）
        if pt_list:
            ph = ",".join("?" * len(pt_list))
            rows = db.execute(f"SELECT path,title,page_type,tags FROM page_meta WHERE page_type IN ({ph}) ORDER BY path", pt_list) # nosec B608
        else:
            rows = db.execute("SELECT path,title,page_type,tags FROM page_meta ORDER BY path")
        out = []
        for r in rows:
            if tag_list:
                tag_set = set(re.split(r"[,，\s]+", r["tags"] or ""))
                if not tag_set.issuperset(tag_list): continue
            out.append({"path": r["path"], "title": r["title"], "page_type": r["page_type"], "tags": r["tags"]})
        return out

@mcp.tool()
def status() -> dict:
    """索引健康状态与统计：页面/附件/引用数、类型分布、嵌入与重排模型、向量数、嵌入 tokens 用量、上次重建时间。""",
    """监控嵌入额度前先看 embed_tokens_used。"""
    with closing(get_db()) as db:
        total = db.execute("SELECT count(*) c FROM page_meta").fetchone()["c"]
        atts = db.execute("SELECT count(*) c FROM attachments").fetchone()["c"]
        links = db.execute("SELECT count(*) c FROM attachment_links").fetchone()["c"]
        by_type = {r["page_type"]: r["c"] for r in db.execute(
            "SELECT page_type, count(*) c FROM page_meta GROUP BY page_type")}
        vec_count = 0
        if EMBED_ENABLED:
            try: vec_count = db.execute("SELECT count(*) c FROM pages_vec").fetchone()["c"]
            except sqlite3.Error: vec_count = -1
        # #12 统计扩展：近30天更新 / 月度分布 / 热门标签
        import datetime as _dt
        now_ts = time.time()
        pages_30d = db.execute("SELECT count(*) c FROM page_meta WHERE updated >= ?",
                               (now_ts - 30 * 86400,)).fetchone()["c"]
        by_month: dict = {}
        for r in db.execute("SELECT updated FROM page_meta"):
            try:
                k = _dt.datetime.fromtimestamp(r["updated"]).strftime("%Y-%m")
                by_month[k] = by_month.get(k, 0) + 1
            except (ValueError, OSError, OverflowError):
                pass
        by_month = dict(sorted(by_month.items()))  # 月份有序
        top_tags: dict = {}
        for r in db.execute("SELECT tags FROM page_meta WHERE tags != ''"):
            for t in re.split(r"[,，\s]+", r["tags"] or ""):
                if t: top_tags[t] = top_tags.get(t, 0) + 1
        top_tags_sorted = sorted(top_tags.items(), key=lambda kv: -kv[1])[:10]
        return {"total_pages": total, "total_attachments": atts, "links": links,
                "by_type": by_type, "last_reindex": get_meta(db, "last_reindex", "never"),
                "schema_version": get_meta(db, "schema_version", "?"),
                "embed_model": get_meta(db, "embed_model", "") or (EMBED_MODEL if EMBED_ENABLED else "未配置"),
                "rerank_model": get_meta(db, "rerank_model", "") or (RERANK_MODEL if RERANK_ENABLED else "未配置"),
                "vec_indexed": vec_count, "embed_tokens_used": get_meta(db, "embed_tokens_used", 0),
                "pages_30d": pages_30d, "pages_by_month": by_month, "top_tags": dict(top_tags_sorted),
                "db": str(DB_PATH), "vault_root": str(VAULT_ROOT)}

@mcp.tool()
def fetch_url(url: str, max_chars: int = 20000) -> dict:
    """抓取网页转 markdown（本地 Defuddle 解析，去广告导航）。返回 {"content": 文本} 或 {"error": 原因}。
    参数: url=http/https 链接(必填); max_chars=返回最大字符数(默认20000)"""
    max_chars = max(1, max_chars)  # 参数校验
    import subprocess, shutil # nosec B404
    if not shutil.which("defuddle"):  # 建议20: 前置检查（比 FileNotFoundError 更友好）
        return {"error": "defuddle 未安装（npm install -g defuddle，需 Node.js）"}
    if not url.startswith(("http://", "https://")):
        return {"error": "INVALID URL: 仅支持 http/https 链接"}
    try:
        r = subprocess.run(["defuddle", "parse", url, "--md"], # nosec B607 B603
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"error": "抓取失败: " + (r.stderr or r.stdout)[:300]}  # 错误结构化
        return {"content": r.stdout[:max_chars]}
    except FileNotFoundError:
        return {"error": "defuddle 未安装（Termux: 需 defuddle CLI）"}
    except subprocess.TimeoutExpired:
        return {"error": "抓取超时（60s）"}
    except Exception as e:
        return {"error": f"抓取异常: {type(e).__name__}: {e}"}

@mcp.tool()
def related(path: str, limit: int = 5) -> list:
    """相关笔记推荐：基于共享引用链接 + 关键词重叠（互补 similar 的纯语义探索）。
    参数: path=笔记相对路径(必填); limit=条数(默认5)"""
    limit = max(1, min(limit, 50))  # 参数校验
    f = safe_resolve(WIKI_ROOT, path)
    if not f or not f.is_file():
        return [{"error": "INVALID PATH: " + path}]
    rel = str(f.relative_to(WIKI_ROOT.resolve()))  # f 是 resolve 后路径，root 也要 resolve 对齐
    text = f.read_text(encoding="utf-8", errors="ignore")[:20000]
    words = [w for w in _jieba_cached(text) if len(w) >= 2 and w not in STOPWORDS]
    top = sorted(set(words), key=lambda w: -words.count(w))[:10]
    with closing(get_db()) as db:
        scores: dict = {}
        # 关键词重叠
        for w in top:
            try:
                rows = db.execute(
                    "SELECT path FROM pages_fts_jieba WHERE pages_fts_jieba MATCH ? LIMIT 20",
                    ('"%s"' % w,))
                for r in rows:
                    if r["path"] != rel:
                        scores[r["path"]] = scores.get(r["path"], 0) + 1
            except sqlite3.Error:
                pass
        # 共享附件引用（权重高）
        refs = [r["att_path"] for r in db.execute(
            "SELECT att_path FROM attachment_links WHERE page_path=?", (rel,))]
        if refs:
            ph = ",".join("?" * len(refs))
            for r in db.execute(
                    f"SELECT DISTINCT page_path FROM attachment_links WHERE att_path IN ({ph}) AND page_path != ?", # nosec B608
                    refs + [rel]):
                scores[r["page_path"]] = scores.get(r["page_path"], 0) + 3
        out = []
        for p, s in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
            meta = db.execute("SELECT title FROM page_meta WHERE path=?", (p,)).fetchone()
            out.append({"path": p, "title": meta["title"] if meta else p, "score": s,
                        "obsidian_url": _obsidian_url(p)})
    return out

@mcp.tool()
def lint(path: str = "wiki", limit: int = 100) -> dict:
    """知识库体检：扫描 wikilink 断链（指向不存在的页面/附件）。
    智能判定：
    - 精确匹配：vault 相对路径存在
    - 模糊匹配：目标文件名在 vault 任意位置存在（如 [[sources/x]] 实际在 wiki/sources/）
    - 模板豁免：wiki/Welcome*/欢迎*/schema/ 占位链接不判链
    参数: path=扫描目录(vault 相对, 默认 wiki); limit=最多返回断链数"""
    limit = max(1, min(limit, 500))
    # 安全校验：path 必须在 vault 内（防 ../../etc 目录遍历逃逸）
    if not safe_resolve(WIKI_ROOT, path):
        return {"error": "INVALID PATH: " + path, "broken": [], "checked": 0, "total_links": 0}
    scan_root = WIKI_ROOT / path
    if not scan_root.is_dir():
        return {"error": "INVALID PATH: " + path, "broken": [], "checked": 0, "total_links": 0}
    pat = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
    broken, checked, total, exempt = [], 0, 0, 0
    with closing(get_db()) as db:
        # all_names 从 DB 构建（page_meta + attachments，避免全库文件遍历）
        all_names = set()
        for r in db.execute("SELECT path FROM page_meta UNION SELECT path FROM attachments"):
            p = r["path"]
            all_names.add(p.split("/")[-1])
            all_names.add(Path(p).stem)
        for f in scan_root.rglob("*.md"):
            checked += 1
            rel = str(f.relative_to(WIKI_ROOT))
            if _IS_TEMPLATE(rel):
                exempt += 1
                continue  # 模板页占位链接豁免
            try: text = f.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError): continue
            for m in pat.finditer(text):
                target = m.group(1).strip()
                if not target or target.startswith(("http://", "https://")): continue
                total += 1
                name = target.split("/")[-1]
                # 1. 精确：vault 根相对存在
                exists = False
                for cand in (WIKI_ROOT / target, WIKI_ROOT / (target + ".md")):
                    try:
                        if cand.exists(): exists = True; break
                    except OSError: pass
                # 2. 模糊：目标文件名在 vault 任意位置存在（路径写错但文件存在不算断链）
                if not exists:
                    for ext in ("", ".md", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                                ".svg", ".pdf", ".canvas", ".excalidraw"):
                        if name + ext in all_names:
                            exists = True
                            break
                if not exists:
                    # suggest 从数据库查真实存在的路径（LIKE 通配符转义）
                    sug = ""
                    try:
                        name_esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        r = db.execute("SELECT path FROM page_meta WHERE path LIKE ? ESCAPE '\\' LIMIT 1",
                                       (f"%{name_esc}%",)).fetchone()
                        if r: sug = r["path"]
                        else:
                            r = db.execute("SELECT path FROM attachments WHERE filename LIKE ? ESCAPE '\\' LIMIT 1",
                                           (f"%{name_esc}%",)).fetchone()
                            if r: sug = r["path"]
                    except Exception: # nosec B110
                        pass
                    broken.append({"page": rel, "link": target,
                                   "suggest": str(sug)})  # str() 转换防 Path 不可序列化
                    if len(broken) >= limit: break
            if len(broken) >= limit: break
    return {"checked_pages": checked, "total_links": total,
            "template_exempt": exempt, "broken_count": len(broken), "broken": broken[:limit]}

@mcp.tool()
def near_duplicates(path: str = "wiki", threshold: float = 0.7, limit: int = 50) -> list:
    """近似重复检测：找出内容高度相似的页面对（Jaccard 相似度，用于 lint 去重）。
    参数: path=扫描目录(vault 相对, 默认 wiki); threshold=Jaccard 相似度阈值(默认0.7); limit=最多对数"""
    limit = max(1, min(limit, 200))
    threshold = max(0.0, min(threshold, 1.0))  # 钳制到 [0,1]
    # 安全校验：path 必须在 vault 内（防目录遍历逃逸）
    if not safe_resolve(WIKI_ROOT, path):
        return [{"error": "INVALID PATH: " + path}]
    scan_root = WIKI_ROOT / path
    if not scan_root.is_dir():
        return [{"error": "INVALID PATH: " + path}]
    docs = {}
    for f in scan_root.rglob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")[:10240]  # 截断 10KB 防 _jieba_cached 大 key 污染
            words = set(w for w in _jieba_cached(text) if len(w) >= 2)
            if len(words) >= 10:  # 太短的页不参与
                docs[str(f.relative_to(WIKI_ROOT))] = words
        except (OSError, UnicodeDecodeError):
            continue
    paths = list(docs)
    out = []
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            a, b = docs[paths[i]], docs[paths[j]]
            inter = len(a & b)
            if inter < 10: continue  # 预筛：共享词太少
            union = len(a | b)
            sim = inter / union if union else 0
            if sim >= threshold:
                out.append({"a": paths[i], "b": paths[j], "similarity": round(sim, 3)})
                if len(out) >= limit: break
        if len(out) >= limit: break
    return out

@mcp.tool()
def extract_document(path: str, max_chars: int = 20000, backend: str = "markitdown",
                     page_range: str = "") -> dict:
    """本地文档/图片转 markdown/描述。返回 {"content": 文本} 或 {"error": 原因}。
    backend: markitdown(本地离线,默认) / mineru(云端免费,≤10MB/≤20页) / mineru_pro(精准版,需key,≤200MB)。
    page_range: 仅 mineru 后端有效，如 "1-20"（超 20 页必须指定）。
    参数: path=文件相对路径(必填); max_chars=返回最大字符数(默认20000)"""
    max_chars = max(1, max_chars)  # 参数校验
    f = safe_resolve(VAULT_ROOT, path)
    if not f or not f.is_file():
        return {"error": "INVALID PATH: " + path}
    if backend == "mineru":
        return _extract_mineru(f, max_chars, page_range)
    if backend == "mineru_pro":
        return _extract_mineru_pro(f, max_chars)
    try:
        from markitdown import MarkItDown
        r = MarkItDown().convert(str(f))
        return {"content": r.text_content[:max_chars]}
    except ModuleNotFoundError:
        return {"error": "markitdown 未安装（Termux: pip install markitdown[pdf,docx,pptx]）"}
    except Exception as e:
        return {"error": f"转换失败: {type(e).__name__}: {str(e)[:200]}"}

def _extract_mineru_pro(f: Path, max_chars: int) -> dict:  # 签名修正
    """MinerU 精准解析 API（/api/v4）：需 MINERU_API_KEY，≤200MB/≤200 页，vlm 模型。
    流程：POST /api/v4/file-urls/batch 申请签名上传 → PUT 到 OSS →
    GET /api/v4/extract-results/batch/{batch_id} 轮询 → done 后下载 zip 取 full.md。"""
    key = os.environ.get("MINERU_API_KEY", "")
    if not key:
        return {"error": "精准 API 需配置 MINERU_API_KEY（mineru.net API 管理页创建 token）"}
    try: size = f.stat().st_size
    except OSError: size = 0
    if size > 200 * 1024 * 1024:
        return {"error": f"MinerU 精准 API 限制 ≤200MB（当前 {size // 1048576}MB）"}
    base = os.environ.get("MINERU_API_BASE", "https://mineru.net").rstrip("/")
    try:
        import requests
    except ImportError:
        return {"error": "MinerU 路径需要 requests 库（pip install requests）"}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    try:
        # 1. 申请批量上传 URL（单文件）
        resp = requests.post(base + "/api/v4/file-urls/batch",
                             json={"files": [{"name": f.name}], "model_version": "vlm"},
                             headers=headers, timeout=30)
        d = resp.json()
        if d.get("code") != 0:
            return {"error": "MinerU 申请失败: " + str(d.get("msg", d))[:200]}
        batch_id = d["data"]["batch_id"]
        url = d["data"]["file_urls"][0]
        # 2. PUT 上传到 OSS
        with f.open("rb") as fh:
            up = requests.put(url, data=fh.read(), timeout=120)
            if up.status_code not in (200, 201):
                return {"error": f"MinerU 文件上传失败 HTTP {up.status_code}"}
        # 3. 轮询 batch 结果（最多 10 分钟）
        for _ in range(200):
            rr = requests.get(base + f"/api/v4/extract-results/batch/{batch_id}",
                              headers=headers, timeout=20)
            try:
                task = rr.json()["data"]["extract_result"][0]
            except Exception:
                time.sleep(3); continue
            state = str(task.get("state", "")).lower()
            if state == "done":
                zip_url = task.get("full_zip_url")
                if zip_url:
                    zdata = requests.get(zip_url, timeout=180).content
                    import io, zipfile
                    with zipfile.ZipFile(io.BytesIO(zdata)) as z:
                        md = None
                        for n in z.namelist():
                            if n.endswith(".md") and "full" in n.lower():
                                md = z.read(n).decode("utf-8", errors="ignore"); break
                        if md is None:
                            mds = [n for n in z.namelist() if n.endswith(".md")]
                            if mds: md = z.read(mds[0]).decode("utf-8", errors="ignore")
                    if md: return {"content": md[:max_chars]}
                return {"error": "MinerU 完成但 zip 内无 markdown"}
            if state == "failed":
                return {"error": "MinerU 解析失败: " + str(task.get("err_msg", "未知"))[:200]}
            time.sleep(3)
        return {"error": "MinerU 解析超时（batch_id=" + str(batch_id) + "）"}
    except Exception as e:
        return {"error": f"MinerU 精准解析失败: {type(e).__name__}: {str(e)[:200]}"}

def _extract_mineru(f: Path, max_chars: int, page_range: str = "") -> dict:  # 签名修正
    """MinerU Agent 轻量云 API（官方文档确认：免 token，IP 限频，≤10MB/≤20 页，免费）。
    流程：POST /api/v1/agent/parse/file 申请签名上传 → PUT 到 OSS → GET /api/v1/agent/parse/{task_id} 轮询
    → done 后 GET markdown_url 下载 markdown。用 requests（urllib TLS 指纹被 WAF 403）。"""
    try: size = f.stat().st_size
    except OSError: size = 0
    if size > 10 * 1024 * 1024:
        return {"error": f"MinerU Agent API 限制 ≤10MB（当前 {size // 1048576}MB），可用本地 markitdown"}
    base = os.environ.get("MINERU_API_BASE", "https://mineru.net").rstrip("/")
    try:
        import requests  # Termux 已装（markitdown 依赖）；urllib TLS 指纹会被 mineru WAF 拒
    except ImportError:
        return {"error": "MinerU 路径需要 requests 库（pip install requests）"}
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android) wiki-search/1.0"}
        body = {"file_name": f.name}
        if page_range:
            body["page_range"] = page_range  # 超 20 页必须指定
        # 1. 申请签名上传 URL（Agent API 免 token）
        resp = requests.post(base + "/api/v1/agent/parse/file",
                             json=body, headers=headers, timeout=30)
        d = resp.json()
        if d.get("code") != 0:
            return {"error": "MinerU 申请失败: " + str(d.get("msg", d))[:200]}
        task_id, file_url = d["data"]["task_id"], d["data"]["file_url"]
        # 2. PUT 上传到 OSS（签名 URL）
        with f.open("rb") as fh:
            up = requests.put(file_url, data=fh.read(), timeout=60)
            if up.status_code not in (200, 201):
                return {"error": f"MinerU 文件上传失败 HTTP {up.status_code}"}
        # 3. 轮询结果（官方端点），最多 5 分钟；首次立即查（4.2 省 3s 延迟）
        for _ in range(100):
            rr = requests.get(base + f"/api/v1/agent/parse/{task_id}", headers=headers, timeout=20)
            data = rr.json().get("data") or {}
            state = str(data.get("state", "")).lower()
            if state == "done":
                md_url = data.get("markdown_url")
                if md_url:
                    md = requests.get(md_url, timeout=30).text
                    if md: return {"content": md[:max_chars]}
                return {"error": "MinerU 完成但无 markdown_url"}
            if state == "failed":
                return {"error": "MinerU 解析失败: " + str(data.get("err_msg", "未知"))[:200]}
            time.sleep(3)
        return {"error": "MinerU 解析超时（task_id=" + str(task_id) + "）"}
    except Exception as e:
        return {"error": f"MinerU 调用失败: {type(e).__name__}: {str(e)[:200]}"}

if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        logger.info("MCP server 被用户中断")
    except Exception as e:
        logger.error("MCP server 启动/运行失败: %s", e)
        raise
