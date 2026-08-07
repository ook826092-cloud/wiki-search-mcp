# wiki-search MCP Server

**SQLite FTS5 keyword + vector semantic hybrid search MCP Server** — millisecond-level retrieval and material processing for Obsidian knowledge bases.

Designed around **Karpathy's LLM Wiki pattern**: AI retrieves knowledge base programmatically via MCP tools (hybrid search / attachment content / broken-link detection / document conversion), replacing inefficient "file traversal + grep".

## ✨ Features

- **Hybrid search**: jieba word-level keyword (BM25) + vector semantic (vec0 KNN) + **query-nature-aware adaptive weights** (short/proper-noun queries → keyword-dominant, descriptive queries → semantic-dominant) + RRF fusion
- **15 MCP tools**: search / related / similar / get / preview / fetch_url / extract_document / search_attachment / get_attachment / page_attachments / list_pages / status / reindex / lint / near_duplicates
- **Attachment content index**: PDF/Office documents auto-extracted (markitdown), full-text searchable
- **Web scraping**: Defuddle CLI → markdown (ingest material)
- **Cloud document parsing**: MinerU (free Agent API / precision API) for complex documents
- **Broken-link detection** (lint): template-page exemption + filename fuzzy match + repair suggestions
- **Near-duplicate detection** (near_duplicates): Jaccard-similar page pairs
- **Synonym expansion**: hand-written map + aliases auto-learning + concept-page keyword enrichment
- **Lazy sync**: 60s vault-change detection → background-thread incremental index (non-blocking)
- **Embedding usage tracking**: status shows cumulative tokens (budget protection)
- **Hardened security**: path-traversal protection, SQL parameterization, LIKE escaping, symlink-escape defense, input validation

## 🏗️ Architecture

```
Query
 ├─ Keyword path: jieba word-level FTS5 (BM25 column weights) → top-N
 ├─ Semantic path: embedding API → vec0 KNN → top-K
 └─ Adaptive-weight fusion (short → keyword-dominant / desc → semantic-dominant / balanced RRF) → optional rerank
      → filters (type/tags/time) → {results, total, groups?}
```

| Component | Technology |
|---|---|
| Storage | SQLite (WAL) + FTS5 + vec0 (sqlite-vec) |
| Chinese tokenization | jieba (word-level index + synonym expansion) |
| Embedding | OpenAI-compatible API (env-configurable, any provider) |
| Rerank | Optional (OpenAI / DashScope format) |
| Document conversion | markitdown (local) + MinerU (cloud) |
| Web scraping | Defuddle CLI |
| Protocol | MCP (Streamable HTTP) |

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install fastmcp jieba markitdown[pdf,docx,pptx]
# (optional) onnxruntime / sqlite-vec / requests

# 2. Configure environment (start.sh)
export VAULT_ROOT="/path/to/vault"
export EMBED_BASE_URL="https://xxx/compatible-mode/v1"   # OpenAI-compatible embedding
export EMBED_API_KEY="sk-xxx"
export EMBED_MODEL="text-embedding-v4"
export EMBED_DIM="1024"
# optional: RERANK_* (rerank), MINERU_API_KEY (precision parsing), ENABLE_TRIGRAM

# 3. Start
nohup fastmcp run server.py --transport http --port 8181 &

# 4. Connect MCP client
# URL: http://127.0.0.1:8181/mcp
```

> 📖 **Full installation tutorial**: [Click here for INSTALL.md (中文)](INSTALL.md) · [English](INSTALL_EN.md) · [繁體中文](INSTALL_ZH-Hant.md)

## 🛠️ Tools

| Tool | Description |
|---|---|
| `search` | Hybrid search (mode: hybrid/keyword/semantic; page_type/tags multi; since; group_by) |
| `related` | Citation + keyword-overlap related recommendations |
| `similar` | Vector semantic similarity |
| `get` / `preview` | Read full / quick preview |
| `fetch_url` | Web page → markdown |
| `extract_document` | Local document → markdown (backend: markitdown/mineru/mineru_pro) |
| `search_attachment` | Attachment search (incl. document body) |
| `get_attachment` / `page_attachments` | Attachment details / page attachment list |
| `list_pages` | Page list (multi-value filters) |
| `status` | Index health + stats + embedding usage |
| `reindex` | Rebuild index (incremental/full) |
| `lint` | Broken-link detection (template exempt + fuzzy match + suggestions) |
| `near_duplicates` | Near-duplicate detection |

## 🔒 Security

- Path-traversal protection (safe_resolve + directory validation)
- SQL parameterization + ALLOWED_TABLES whitelist
- LIKE wildcard escaping (ESCAPE)
- Symlink-escape defense
- Input boundary validation

## 📄 License

MIT
