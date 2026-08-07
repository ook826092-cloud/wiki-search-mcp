"""属性测试：hypothesis 随机/边界输入轰炸核心函数"""
import os, tempfile
from pathlib import Path
TEST_ROOT = Path(tempfile.mkdtemp())
os.environ["WIKI_ROOT"] = str(TEST_ROOT)
os.environ["VAULT_ROOT"] = str(TEST_ROOT)
os.environ["WIKI_DB"] = str(TEST_ROOT / "test.db")
os.environ["VEC0_PATH"] = ""
import server  # noqa: E402
from hypothesis import given, strategies as st, settings
settings.register_profile("ci", max_examples=30, deadline=2000)
settings.load_profile("ci")

@given(st.text())
def test_parse_frontmatter_never_crashes(text):
    title, ptype, tags, aliases = server.parse_frontmatter(text)
    assert isinstance(title, (str, type(None)))
    assert isinstance(ptype, (str, type(None)))
    assert isinstance(tags, str)
    assert isinstance(aliases, str)

@given(st.text(max_size=500))
def test_tokenize_query_never_crashes(q):
    fts, like = server.tokenize_query(q)
    assert isinstance(fts, list) and isinstance(like, list)

@given(st.text(max_size=100))
def test_query_nature_valid(q):
    assert server._query_nature(q) in ("short", "desc", "balanced")

@given(st.text())
def test_cjk_chunks_preserves(s):
    chunks = server.cjk_chunks(s)
    assert "".join(chunks) == s

@given(st.text(max_size=200), st.lists(st.text(max_size=10), max_size=5))
def test_make_snippet_no_crash(body, terms):
    sn = server.make_snippet(body, terms)
    assert isinstance(sn, str)

@given(st.text(max_size=50), st.text(max_size=100))
def test_safe_resolve_never_escapes(root, rel):
    p = server.safe_resolve(Path(tempfile.mkdtemp()), rel)
    # 结果要么 None，要么在 root 内
    if p is not None:
        assert str(p).startswith(str(server.WIKI_ROOT))

@given(st.text(max_size=200))
def test_jieba_seg_never_crashes(s):
    r = server._jieba_seg(s)
    assert isinstance(r, str)
