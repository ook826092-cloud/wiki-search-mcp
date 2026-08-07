# 📖 安装教程

本文教你从零安装并运行 **wiki-search MCP Server**。支持 **Termux（Android）** 与 **Linux / macOS / Windows（WSL）**。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10+（推荐 3.12+） |
| 操作系统 | Termux (Android) / Linux / macOS / Windows (WSL) |
| 嵌入 API | 任意 OpenAI 兼容嵌入服务（如 OpenAI、阿里云百炼、OpenRouter 免费模型等） |
| 可选 | sqlite-vec（向量语义检索需要）、Node.js（网页抓取需要） |

---

## 2. 安装依赖

### 2.1 核心依赖

```bash
pip install fastmcp jieba requests
```

### 2.2 文档转换（markitdown）

```bash
pip install "markitdown[pdf,docx,pptx]"
```

> **Termux 用户注意**：若 `pip` 编译 C 扩展失败，用 Termux 预编译包：
> ```bash
> pkg install python-numpy python-pillow python-lxml
> ```

### 2.3 向量语义检索（可选但推荐）

**方案 A：sqlite-vec（本地向量引擎）**

从 [sqlite-vec Releases](https://github.com/asg017/sqlite-vec/releases) 下载 `vec0.so`，放到 server.py 同目录，设置环境变量：

```bash
export VEC0_PATH="/path/to/vec0.so"
```

**方案 B：无向量（纯关键词）**

不安装 vec0 也可用（关键词检索 + 附件内容），只是没有语义检索。

### 2.4 网页抓取（可选）

```bash
npm install -g defuddle   # Node.js 需要 >= 18
```

### 2.5 云端高保真解析（可选）

需要 [mineru.net](https://mineru.net) 注册 token（精准 API 版）；Agent 轻量版免费无需 token。

---

## 3. 下载代码

```bash
git clone https://github.com/ook826092-cloud/wiki-search-mcp.git
cd wiki-search-mcp
```

---

## 4. 配置环境变量

创建启动脚本（或直接命令行设置）：

```bash
# 必需：vault 路径
export VAULT_ROOT="/path/to/your/obsidian/vault"
export WIKI_ROOT="$VAULT_ROOT"          # 可选：检索根（默认同 vault）

# 必需：嵌入 API（OpenAI 兼容）
export EMBED_BASE_URL="https://api.openai.com/v1"       # 换成你的提供商
export EMBED_API_KEY="sk-xxx"
export EMBED_MODEL="text-embedding-3-small"
export EMBED_DIM="1024"                 # 维度需与模型一致
export EMBED_BATCH="8"                  # 批次（≤ 提供商上限）

# 可选：向量引擎
export VEC0_PATH="/path/to/vec0.so"

# 可选：重排
export RERANK_BASE_URL="https://api.openai.com/v1"
export RERANK_API_KEY="sk-xxx"
export RERANK_MODEL="your-rerank-model"
export RERANK_FORMAT="openai"           # openai / dashscope

# 可选：MinerU 精准解析（需注册）
export MINERU_API_KEY="your-mineru-token"

# 可选：并发（默认自动：free 模型=1，其他=4）
# export EMBED_CONCURRENCY="2"

# 可选：日志
export LOG_LEVEL="INFO"
```

---

## 5. 启动

```bash
nohup fastmcp run server.py --transport http --port 8181 > mcp.log 2>&1 &
```

验证：

```bash
fastmcp list  http://127.0.0.1:8181/mcp          # 应列出 15 个工具
fastmcp call  http://127.0.0.1:8181/mcp search query="测试" limit=1
```

---

## 6. 连接 MCP 客户端

在支持 MCP 的客户端（如 Cherry Studio、RikkaHub、Claude Desktop 等）添加服务器：

```
类型：Streamable HTTP
URL： http://127.0.0.1:8181/mcp
```

连接后即可使用 `search` / `get` / `extract_document` / `lint` 等 15 个工具。

---

## 7. 首次使用建议

```bash
# 1. 查看索引状态（页面/附件/向量数/嵌入用量）
fastmcp call http://127.0.0.1:8181/mcp status

# 2. 建立全量索引（首次，耗时取决于库大小）
fastmcp call http://127.0.0.1:8181/mcp reindex full=true

# 3. 体检断链
fastmcp call http://127.0.0.1:8181/mcp lint path=wiki
```

---

## 8. 常见问题

### Q1: 语义检索无效？
- 检查 `status` 的 `vec_indexed`（>0 才有语义）
- `EMBED_DIM` 必须与模型实际维度一致
- vec0.so 路径正确

### Q2: 嵌入报 403？
- API key 失效 / 额度耗尽
- 检查 `EMBED_API_KEY` / 控制台余额

### Q3: markitdown 装不上（Termux）？
- 用 `pkg install python-numpy python-pillow python-lxml` 预编译包

### Q4: 中文短词（"B站"）搜不到？
- 已内置 jieba 词表 + 同义词扩展；可在 server.py 的 `jieba.add_word` 或 `SYNONYMS` 添加

### Q5: 数据库在哪 / 备份？
- 默认 `wiki.db`（同目录）；用 `backup.sh` 备份

---

## 9. 卸载

```bash
pkill -f "fastmcp run server.py"
# 删除目录 + pip uninstall fastmcp jieba markitdown
```

---

*如果遇到问题，欢迎提 [Issue](https://github.com/ook826092-cloud/wiki-search-mcp/issues)*
