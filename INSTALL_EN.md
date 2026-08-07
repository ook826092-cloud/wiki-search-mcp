# 📖 Installation Guide

Step-by-step guide to install and run **wiki-search MCP Server**. Supports **Termux (Android)**, **Linux / macOS / Windows (WSL)**.

---

## 1. Requirements

| Item | Requirement |
|---|---|
| Python | 3.10+ (3.12+ recommended) |
| OS | Termux (Android) / Linux / macOS / Windows (WSL) |
| Embedding API | Any OpenAI-compatible embedding service (OpenAI, Alibaba Bailian, OpenRouter free models, etc.) |
| Optional | sqlite-vec (vector search), Node.js (web scraping) |

---

## 2. Install Dependencies

### 2.1 Core

```bash
pip install fastmcp jieba requests
```

### 2.2 Document Conversion (markitdown)

```bash
pip install "markitdown[pdf,docx,pptx]"
```

> **Termux users**: if `pip` fails compiling C extensions, use Termux prebuilt packages:
> ```bash
> pkg install python-numpy python-pillow python-lxml
> ```

### 2.3 Vector Search (optional but recommended)

**Option A: sqlite-vec (local vector engine)**

Download `vec0.so` from [sqlite-vec Releases](https://github.com/asg017/sqlite-vec/releases), place it next to server.py, then:

```bash
export VEC0_PATH="/path/to/vec0.so"
```

**Option B: No vectors (keyword-only)**

Works without vec0 (keyword + attachment content), just no semantic search.

### 2.4 Web Scraping (optional)

```bash
npm install -g defuddle   # Node.js >= 18
```

### 2.5 Cloud Precision Parsing (optional)

Register at [mineru.net](https://mineru.net) for a token (precision API); Agent lightweight API is free without token.

---

## 3. Download Code

```bash
git clone https://github.com/ook826092-cloud/wiki-search-mcp.git
cd wiki-search-mcp
```

---

## 4. Configure Environment

Create a start script (or set via CLI):

```bash
# Required: vault path
export VAULT_ROOT="/path/to/your/obsidian/vault"
export WIKI_ROOT="$VAULT_ROOT"          # optional: search root (default same as vault)

# Required: embedding API (OpenAI-compatible)
export EMBED_BASE_URL="https://api.openai.com/v1"       # change to your provider
export EMBED_API_KEY="sk-xxx"
export EMBED_MODEL="text-embedding-3-small"
export EMBED_DIM="1024"                 # must match model dimension
export EMBED_BATCH="8"                  # batch (<= provider limit)

# Optional: vector engine
export VEC0_PATH="/path/to/vec0.so"

# Optional: rerank
export RERANK_BASE_URL="https://api.openai.com/v1"
export RERANK_API_KEY="sk-xxx"
export RERANK_MODEL="your-rerank-model"
export RERANK_FORMAT="openai"           # openai / dashscope

# Optional: MinerU precision parsing (register required)
export MINERU_API_KEY="your-mineru-token"

# Optional: concurrency (auto: free model=1, others=4)
# export EMBED_CONCURRENCY="2"

# Optional: logging
export LOG_LEVEL="INFO"
```

---

## 5. Start

```bash
nohup fastmcp run server.py --transport http --port 8181 > mcp.log 2>&1 &
```

Verify:

```bash
fastmcp list  http://127.0.0.1:8181/mcp          # should list 15 tools
fastmcp call  http://127.0.0.1:8181/mcp search query="test" limit=1
```

---

## 6. Connect MCP Client

Add server in any MCP-capable client (Cherry Studio, RikkaHub, Claude Desktop, etc.):

```
Type: Streamable HTTP
URL: http://127.0.0.1:8181/mcp
```

After connecting, use the 15 tools: `search` / `get` / `extract_document` / `lint` etc.

---

## 7. First-Use Tips

```bash
# 1. Check index status (pages/attachments/vectors/embedding usage)
fastmcp call http://127.0.0.1:8181/mcp status

# 2. Build full index (first time, duration depends on vault size)
fastmcp call http://127.0.0.1:8181/mcp reindex full=true

# 3. Health-check broken links
fastmcp call http://127.0.0.1:8181/mcp lint path=wiki
```

---

## 8. Install onnxruntime on Termux (optional, from Release)

Document conversion (markitdown) needs onnxruntime (for magika file-type detection). **Termux users can download directly from Release** (built via official termux-packages flow, same source as Termux repo, permanent):

```bash
# 1. Download two debs
# Option A: gh CLI
gh release download onnxruntime-termux-1.28.0 -R ook826092-cloud/wheel-forge
# Option B: curl
curl -L -o onnx.deb https://github.com/ook826092-cloud/wheel-forge/releases/download/onnxruntime-termux-1.28.0/onnxruntime_1.28.0_aarch64.deb
curl -L -o python-onnx.deb https://github.com/ook826092-cloud/wheel-forge/releases/download/onnxruntime-termux-1.28.0/python-onnxruntime_1.28.0_aarch64.deb

# 2. Install
dpkg -i onnxruntime_1.28.0_aarch64.deb python-onnxruntime_1.28.0_aarch64.deb

# 3. Verify
python3 -c "import onnxruntime; print(onnxruntime.__version__)"
```

> Release: https://github.com/ook826092-cloud/wheel-forge/releases/tag/onnxruntime-termux-1.28.0
> Artifacts: onnxruntime (C++ lib 4.7MB) + python-onnxruntime (bindings 5.5MB), aarch64/bionic

## 9. FAQ

### Q1: Semantic search not working?
- Check `status` `vec_indexed` (>0 required)
- `EMBED_DIM` must match actual model dimension
- Correct vec0.so path

### Q2: Embedding 403?
- API key invalid / quota exhausted
- Check `EMBED_API_KEY` / provider console balance

### Q3: markitdown install fails (Termux)?
- Use `pkg install python-numpy python-pillow python-lxml` prebuilt packages

### Q4: Short Chinese words (e.g. "B站") not found?
- Built-in jieba word list + synonym expansion; add to `jieba.add_word` / `SYNONYMS` in server.py

### Q5: Where is the database / backup?
- Default `wiki.db` (same directory); use `backup.sh` to back up

---

## 10. Uninstall

```bash
pkill -f "fastmcp run server.py"
# Remove directory + pip uninstall fastmcp jieba markitdown
```

---

*For issues, feel free to open an [Issue](https://github.com/ook826092-cloud/wiki-search-mcp/issues)*
