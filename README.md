**🌐 Language**: [中文](README.md) · [English](README_EN.md) · [繁體中文](README_ZH-Hant.md)

---

# wiki-search MCP Server

**SQLite FTS5 关键词 + 向量语义 混合检索 MCP Server** —— 为 Obsidian 知识库提供毫秒级检索与素材处理能力。

基于 **Karpathy 的 LLM Wiki 模式**设计：让 AI 通过 MCP 工具对知识库做程序化检索（混合检索/附件内容/断链检测/文档转换），替代"遍历文件 + grep"的低效方式。

## ✨ 特性

- **混合检索**：jieba 词级关键词（BM25）+ 向量语义（vec0 KNN）+ **查询类型感知动态权重**（短词/专有名词关键词主导，描述句语义主导）+ RRF 融合
- **15 个 MCP 工具**：search / related / similar / get / preview / fetch_url / extract_document / search_attachment / get_attachment / page_attachments / list_pages / status / reindex / lint / near_duplicates
- **附件内容索引**：PDF/Office 文档自动提取文本（markitdown），可搜正文
- **网页抓取**：Defuddle CLI 抓 URL → markdown（ingest 素材）
- **云端文档解析**：MinerU（Agent 免费版 / 精准 API 版）高保真解析复杂文档
- **断链检测**（lint）：模板页豁免 + 文件名模糊匹配 + 修复建议 —— 机制层治本
- **近似重复检测**（near_duplicates）：Jaccard 相似页面对
- **同义词扩展**：手写映射 + aliases 自动学习 + 概念页高频词扩充
- **懒同步**：60s 检测 vault 变化 → 后台线程增量索引（不阻塞查询）
- **嵌入用量监控**：status 显示累计 tokens（防额度超支）
- **安全加固**：路径遍历防护、SQL 参数化、LIKE 转义、symlink 逃逸防御、参数校验

## 🏗️ 架构

```
查询
 ├─ 关键词路：jieba 词级 FTS5（BM25 列权重）→ top-N
 ├─ 语义路：嵌入 API → vec0 KNN → top-K
 └─ 动态权重融合（short 关键词主导 / desc 语义主导 / balanced RRF）→ 可选 rerank
      → 过滤（类型/标签/时间）→ {results, total, groups?}
```

| 组件 | 技术 |
|---|---|
| 存储 | SQLite（WAL）+ FTS5 + vec0（sqlite-vec） |
| 中文分词 | jieba（词级索引 + 同义词扩展） |
| 嵌入 | OpenAI 兼容 API（环境变量配置，任意提供商） |
| 重排 | 可选（OpenAI / DashScope 格式） |
| 文档转换 | markitdown（本地）+ MinerU（云端） |
| 网页抓取 | Defuddle CLI |
| 协议 | MCP（Streamable HTTP） |

## 📖 安装教程

> 🖱️ **[点击这里查看完整安装教程 → INSTALL.md](INSTALL.md)**
> 涵盖：环境要求 / 依赖安装（含 Termux 专项）/ 配置 / 启动 / 客户端连接 / 首次使用 / 常见问题

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install fastmcp jieba markitdown[pdf,docx,pptx]
# （可选）onnxruntime / sqlite-vec / requests

# 2. 配置环境变量（start.sh）
export VAULT_ROOT="/path/to/vault"
export EMBED_BASE_URL="https://xxx/compatible-mode/v1"   # OpenAI 兼容嵌入
export EMBED_API_KEY="sk-xxx"
export EMBED_MODEL="text-embedding-v4"
export EMBED_DIM="1024"
# 可选：RERANK_*（重排）、MINERU_API_KEY（精准解析）、ENABLE_TRIGRAM

# 3. 启动
nohup fastmcp run server.py --transport http --port 8181 &

# 4. 客户端连接 MCP
# URL: http://127.0.0.1:8181/mcp
```

## 🛠️ 工具一览

| 工具 | 说明 |
|---|---|
| `search` | 混合检索（mode: hybrid/keyword/semantic；page_type/tags 多值；since 时间；group_by 聚合） |
| `related` | 引用链接 + 关键词重叠相关推荐 |
| `similar` | 向量语义相似 |
| `get` / `preview` | 读全文 / 快速预览 |
| `fetch_url` | 网页 → markdown |
| `extract_document` | 本地文档 → markdown（backend: markitdown/mineru/mineru_pro） |
| `search_attachment` | 附件检索（含文档正文） |
| `get_attachment` / `page_attachments` | 附件线索 / 页面附件清单 |
| `list_pages` | 页面清单（多值过滤） |
| `status` | 索引健康 + 统计 + 嵌入用量 |
| `reindex` | 重建索引（增量/全量） |
| `lint` | 断链检测（模板豁免 + 模糊匹配 + 修复建议） |
| `near_duplicates` | 近似重复检测 |

## 🔒 安全

- 路径遍历防护（safe_resolve + 目录校验）
- SQL 全参数化 + ALLOWED_TABLES 白名单
- LIKE 通配符转义（ESCAPE）
- symlink 逃逸防御
- 参数边界校验

## 🧪 测试体系（进行中）

本项目配置了完整的企业级 CI/CD 测试体系（GitHub Actions），当前以下测试**正在进行中**，全部通过后将标注「企业级标准测试通过」。

| Workflow | 测什么 | 架构 | 触发 | 状态 |
|---|---|---|---|---|
| **静态+动态测试** | 语法/Ruff 静态错误/Bandit 安全/Semgrep OWASP 扫描/MyPy 类型 + 单元/故障注入/属性测试（hypothesis）/MCP 协议级/覆盖率 | x86_64 + ARM64 | push 自动 | 🔄 进行中（Semgrep 新增验证） |
| **安全分析（CodeQL）** | GitHub 官方语义安全分析（SQL 注入/XSS/路径遍历/敏感信息） | x86_64 | push 自动 | 🔄 x86 已通过；ARM64 官方不支持，由 Semgrep 覆盖 |
| **极限测试** | 企业级百万页全量索引（reindex 分批 flush）、1 万次查询压测（p50/p95/p99/QPS）、256 线程并发、50MB 巨页、5000 模糊查询、路径攻击、wikilink 注入 | x86_64 + ARM64 | 手动触发 | 🔄 进行中 |
| **移动端极限测试** | Termux（bionic）容器环境 + Android 模拟器（x86_64 / arm64-v8a 真机架构）真装 Termux APK 跑回归/敌意测试 | x86_64 + ARM64 模拟器 | 手动触发 | 🔄 进行中 |
| **变异测试** | mutmut 自动篡改 server.py 全部逻辑点，衡量测试套件杀伤力（杀死率 = 测试质量） | x86_64 + ARM64 | 手动触发 | 🔄 进行中 |

### 测试覆盖范围

- **静态**：语法编译、ruff 未定义/逻辑错误、bandit 安全模式、Semgrep OWASP 语义规则、mypy 类型检查
- **动态**：单元测试、故障注入（空查询/超长输入/FTS 特殊字符/嵌入禁用降级）、hypothesis 属性测试（随机边界轰炸）、MCP 协议级（真起 server + StreamableHttp 调 15 工具）
- **极限**：百万页索引、查询延迟基准、并发轰炸、路径攻击矩阵、编码混淆、SQL/命令注入、XSS、YAML 炸弹、DoS
- **安全**：CodeQL（x86）+ Semgrep（ARM64）+ bandit 三路安全扫描
- **移动端**：Termux bionic 真实环境 + Android 模拟器真机 Termux

> 📌 测试状态说明：进行中测试的最终结果将在通过后更新于此章节。

## 📄 License

MIT
