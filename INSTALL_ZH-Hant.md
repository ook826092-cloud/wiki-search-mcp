# 📖 安裝教學

本文教你從零安裝並執行 **wiki-search MCP Server**。支援 **Termux（Android）** 與 **Linux / macOS / Windows（WSL）**。

---

## 1. 環境需求

| 項目 | 需求 |
|---|---|
| Python | 3.10+（建議 3.12+） |
| 作業系統 | Termux (Android) / Linux / macOS / Windows (WSL) |
| 嵌入 API | 任意 OpenAI 相容嵌入服務（OpenAI、阿里雲百煉、OpenRouter 免費模型等） |
| 可選 | sqlite-vec（向量語意檢索）、Node.js（網頁抓取） |

---

## 2. 安裝依賴

### 2.1 核心依賴

```bash
pip install fastmcp jieba requests
```

### 2.2 文件轉換（markitdown）

```bash
pip install "markitdown[pdf,docx,pptx]"
```

> **Termux 用戶注意**：若 `pip` 編譯 C 擴充失敗，用 Termux 預編譯套件：
> ```bash
> pkg install python-numpy python-pillow python-lxml
> ```

### 2.3 向量語意檢索（可選但建議）

**方案 A：sqlite-vec（本地向量引擎）**

從 [sqlite-vec Releases](https://github.com/asg017/sqlite-vec/releases) 下載 `vec0.so`，放到 server.py 同目錄，設定環境變數：

```bash
export VEC0_PATH="/path/to/vec0.so"
```

**方案 B：無向量（純關鍵詞）**

不安裝 vec0 也可用（關鍵詞檢索 + 附件內容），只是沒有語意檢索。

### 2.4 網頁抓取（可選）

```bash
npm install -g defuddle   # Node.js 需要 >= 18
```

### 2.5 雲端高保真解析（可選）

需要 [mineru.net](https://mineru.net) 註冊 token（精準 API 版）；Agent 輕量版免費無需 token。

---

## 3. 下載程式碼

```bash
git clone https://github.com/ook826092-cloud/wiki-search-mcp.git
cd wiki-search-mcp
```

---

## 4. 設定環境變數

建立啟動腳本（或直接命令列設定）：

```bash
# 必需：vault 路徑
export VAULT_ROOT="/path/to/your/obsidian/vault"
export WIKI_ROOT="$VAULT_ROOT"          # 可選：檢索根（預設同 vault）

# 必需：嵌入 API（OpenAI 相容）
export EMBED_BASE_URL="https://api.openai.com/v1"       # 換成你的供應商
export EMBED_API_KEY="sk-xxx"
export EMBED_MODEL="text-embedding-3-small"
export EMBED_DIM="1024"                 # 維度需與模型一致
export EMBED_BATCH="8"                  # 批次（≤ 供應商上限）

# 可選：向量引擎
export VEC0_PATH="/path/to/vec0.so"

# 可選：重排
export RERANK_BASE_URL="https://api.openai.com/v1"
export RERANK_API_KEY="sk-xxx"
export RERANK_MODEL="your-rerank-model"
export RERANK_FORMAT="openai"           # openai / dashscope

# 可選：MinerU 精準解析（需註冊）
export MINERU_API_KEY="your-mineru-token"

# 可選：並發（預設自動：free 模型=1，其他=4）
# export EMBED_CONCURRENCY="2"

# 可選：日誌
export LOG_LEVEL="INFO"
```

---

## 5. 啟動

```bash
nohup fastmcp run server.py --transport http --port 8181 > mcp.log 2>&1 &
```

驗證：

```bash
fastmcp list  http://127.0.0.1:8181/mcp          # 應列出 15 個工具
fastmcp call  http://127.0.0.1:8181/mcp search query="測試" limit=1
```

---

## 6. 連接 MCP 用戶端

在支援 MCP 的用戶端（Cherry Studio、RikkaHub、Claude Desktop 等）新增伺服器：

```
類型：Streamable HTTP
URL： http://127.0.0.1:8181/mcp
```

連接後即可使用 `search` / `get` / `extract_document` / `lint` 等 15 個工具。

---

## 7. 首次使用建議

```bash
# 1. 查看索引狀態（頁面/附件/向量數/嵌入用量）
fastmcp call http://127.0.0.1:8181/mcp status

# 2. 建立全量索引（首次，耗時取決於庫大小）
fastmcp call http://127.0.0.1:8181/mcp reindex full=true

# 3. 體檢斷鏈
fastmcp call http://127.0.0.1:8181/mcp lint path=wiki
```

---

## 8. 常見問題

### Q1: 語意檢索無效？
- 檢查 `status` 的 `vec_indexed`（>0 才有語意）
- `EMBED_DIM` 必須與模型實際維度一致
- vec0.so 路徑正確

### Q2: 嵌入報 403？
- API key 失效 / 額度耗盡
- 檢查 `EMBED_API_KEY` / 控制台餘額

### Q3: markitdown 裝不上（Termux）？
- 用 `pkg install python-numpy python-pillow python-lxml` 預編譯套件

### Q4: 中文短詞（"B站"）搜不到？
- 已內建 jieba 詞表 + 同義詞擴展；可在 server.py 的 `jieba.add_word` 或 `SYNONYMS` 新增

### Q5: 資料庫在哪 / 備份？
- 預設 `wiki.db`（同目錄）；用 `backup.sh` 備份

---

## 9. 解除安裝

```bash
pkill -f "fastmcp run server.py"
# 刪除目錄 + pip uninstall fastmcp jieba markitdown
```

---

*如果遇到問題，歡迎提出 [Issue](https://github.com/ook826092-cloud/wiki-search-mcp/issues)*
