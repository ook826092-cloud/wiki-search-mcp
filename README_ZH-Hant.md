# wiki-search MCP Server

**SQLite FTS5 關鍵詞 + 向量語意 混合檢索 MCP Server** —— 為 Obsidian 知識庫提供毫秒級檢索與素材處理能力。

基於 **Karpathy 的 LLM Wiki 模式**設計：讓 AI 透過 MCP 工具對知識庫做程式化檢索（混合檢索/附件內容/斷鏈檢測/文件轉換），取代「遍歷檔案 + grep」的低效方式。

## ✨ 特性

- **混合檢索**：jieba 詞級關鍵詞（BM25）+ 向量語意（vec0 KNN）+ **查詢類型感知動態權重**（短詞/專有名詞關鍵詞主導，描述句語意主導）+ RRF 融合
- **15 個 MCP 工具**：search / related / similar / get / preview / fetch_url / extract_document / search_attachment / get_attachment / page_attachments / list_pages / status / reindex / lint / near_duplicates
- **附件內容索引**：PDF/Office 文件自動提取文字（markitdown），可搜內文
- **網頁抓取**：Defuddle CLI 抓 URL → markdown（ingest 素材）
- **雲端文件解析**：MinerU（Agent 免費版 / 精準 API 版）高保真解析複雜文件
- **斷鏈檢測**（lint）：模板頁豁免 + 檔名模糊比對 + 修復建議 —— 機制層治本
- **近似重複檢測**（near_duplicates）：Jaccard 相似頁面對
- **同義詞擴展**：手寫映射 + aliases 自動學習 + 概念頁高頻詞擴充
- **懶同步**：60s 偵測 vault 變化 → 背景執行緒增量索引（不阻塞查詢）
- **嵌入用量監控**：status 顯示累計 tokens（防額度超支）
- **安全加固**：路徑遍歷防護、SQL 參數化、LIKE 轉義、symlink 逃逸防禦、參數校驗

## 🏗️ 架構

```
查詢
 ├─ 關鍵詞路：jieba 詞級 FTS5（BM25 欄位權重）→ top-N
 ├─ 語意路：嵌入 API → vec0 KNN → top-K
 └─ 動態權重融合（short 關鍵詞主導 / desc 語意主導 / balanced RRF）→ 可選 rerank
      → 過濾（類型/標籤/時間）→ {results, total, groups?}
```

| 元件 | 技術 |
|---|---|
| 儲存 | SQLite（WAL）+ FTS5 + vec0（sqlite-vec） |
| 中文分詞 | jieba（詞級索引 + 同義詞擴展） |
| 嵌入 | OpenAI 相容 API（環境變數設定，任意供應商） |
| 重排 | 可選（OpenAI / DashScope 格式） |
| 文件轉換 | markitdown（本地）+ MinerU（雲端） |
| 網頁抓取 | Defuddle CLI |
| 協定 | MCP（Streamable HTTP） |

## 🚀 快速開始

```bash
# 1. 安裝依賴
pip install fastmcp jieba markitdown[pdf,docx,pptx]
# （可選）onnxruntime / sqlite-vec / requests

# 2. 設定環境變數（start.sh）
export VAULT_ROOT="/path/to/vault"
export EMBED_BASE_URL="https://xxx/compatible-mode/v1"   # OpenAI 相容嵌入
export EMBED_API_KEY="sk-xxx"
export EMBED_MODEL="text-embedding-v4"
export EMBED_DIM="1024"
# 可選：RERANK_*（重排）、MINERU_API_KEY（精準解析）、ENABLE_TRIGRAM

# 3. 啟動
nohup fastmcp run server.py --transport http --port 8181 &

# 4. 連接 MCP 用戶端
# URL: http://127.0.0.1:8181/mcp
```

> 📖 **完整安裝教學**：[點此查看 INSTALL.md（簡中）](INSTALL.md) · [English](INSTALL_EN.md) · [繁體中文](INSTALL_ZH-Hant.md)

## 🛠️ 工具一覽

| 工具 | 說明 |
|---|---|
| `search` | 混合檢索（mode: hybrid/keyword/semantic；page_type/tags 多值；since 時間；group_by 聚合） |
| `related` | 引用連結 + 關鍵詞重疊相關推薦 |
| `similar` | 向量語意相似 |
| `get` / `preview` | 讀全文 / 快速預覽 |
| `fetch_url` | 網頁 → markdown |
| `extract_document` | 本地文件 → markdown（backend: markitdown/mineru/mineru_pro） |
| `search_attachment` | 附件檢索（含文件內文） |
| `get_attachment` / `page_attachments` | 附件線索 / 頁面附件清單 |
| `list_pages` | 頁面清單（多值過濾） |
| `status` | 索引健康 + 統計 + 嵌入用量 |
| `reindex` | 重建索引（增量/全量） |
| `lint` | 斷鏈檢測（模板豁免 + 模糊比對 + 修復建議） |
| `near_duplicates` | 近似重複檢測 |

## 🔒 安全

- 路徑遍歷防護（safe_resolve + 目錄校驗）
- SQL 全參數化 + ALLOWED_TABLES 白名單
- LIKE 萬用字元轉義（ESCAPE）
- symlink 逃逸防禦
- 參數邊界校驗

## 📄 License

MIT
