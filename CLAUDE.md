# 智能技术学习系统（RAG 学习版）开发文档

> 版本：v0.7（2026-08-28）
> 架构来源：飞书文档《RAG 项目架构》（本地权威副本：`.cursor/rules/rag-feishu-architecture.mdc`）
> 协作约定：助手可直接读写仓库代码（v0.3 起取消「代码全部手写」限制），改动需与本文档的架构和目录约定保持一致，关键决策做简要说明。

---

## 一、项目定位

一个「边爬边学」的 AI 技术知识学习系统：

1. **爬取**：从互联网自动爬取编程 / AI 相关技术文档（Python、FastAPI、Pydantic、AIGC……）
2. **加工**：对知识做切块（chunk）、Embedding 向量化，写入向量库（Milvus）
3. **检索**：BM25 关键词检索 + 向量语义检索 → 混合融合 → Rerank 重排序，找出最相关的知识片段
4. **对话学习**（两种模式）：
   - **讲解模式（问）**：我发 `aigc`，AI 当老师，基于知识库把我讲明白
   - **费曼模式（教）**：我发 `aigc`，AI 变学生，我当老师讲给它听；它负责追问、挑毛病、最后总结并给我的讲解打分
5. **每日学习卡片**：主页每天随机展示一个技术知识点，碎片化学习

---

## 二、技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | 对外接口、流式响应 |
| 参数校验/配置 | pydantic / pydantic-settings | DTO（schema）、`.env` 全局配置 |
| 关系库 | MySQL + SQLAlchemy + PyMySQL | 知识原始数据、会话/卡片等业务记录 |
| 缓存/队列 | Redis | 爬虫任务队列、URL 去重、每日卡片缓存 |
| 爬虫 | httpx + BeautifulSoup + lxml | 抓取、解析、扩链 |
| 向量库 | Milvus（pymilvus） | 向量存储与 ANN 检索 |
| Embedding | BAAI/bge-m3（OpenAI 兼容接口） | 文本向量化（1024 维） |
| 关键词检索 | rank_bm25 + jieba | BM25，与向量结果混合 |
| 重排序 | bge-reranker（API） | 混合结果精排 |
| LLM | 大模型 Chat API（流式，双协议） | 双模式对话生成；OpenAI 兼容协议（httpx）+ Anthropic Messages 协议（官方 anthropic SDK），用户可自配 base_url/api_key/model |

---

## 三、总体架构（数据流）

```text
  ┌────────────┐     ┌──────────────┐     ┌─────────────────────┐
  │ 技术网站    │ ──▶ │ 分布式爬虫    │ ──▶ │ MySQL knowledge 表   │
  │ fastapi/py… │     │ Redis队列+去重│     │ （原始知识，待向量化） │
  └────────────┘     └──────────────┘     └──────────┬──────────┘
                                                     │ 切块 + Embedding
                                                     ▼
  ┌────────────┐     ┌──────────────┐     ┌─────────────────────┐
  │ 每日学习卡片 │ ◀── │ 知识/术语     │     │ Milvus 向量库        │
  └────────────┘     └──────────────┘     └──────────┬──────────┘
                                                     │
        用户提问/讲解 ──▶ BM25 + 向量混合检索 ──▶ Rerank ──▶ LLM 生成
                                                     │
                              ┌──────────────────────┴───────────┐
                              ▼                                  ▼
                       讲解模式（AI当老师）                费曼模式（AI当学生）
```

---

## 四、目录结构（与飞书架构对齐，不得擅改）

```text
project/
├── data/                    # 数据目录（上传文件、BM25缓存）
├── sessions/                # 会话目录（用户对话历史）
├── storage/                 # 存储目录（JD截图、临时文件）
├── frontend/                # 前端静态站（原生 HTML/CSS/JS，10001 端口）
│   ├── index.html           # 主页：每日学习卡片
│   ├── chat.html            # 对话页：讲解/费曼双模式
│   ├── kb.html              # 知识库页：分类聚合+条目列表+正文弹窗
│   ├── css/style.css
│   └── js/                  # api.js / home.js / chat.js / kb.js / highlight.js（代码高亮+富文本）
└── src/
    ├── main.py              # FastAPI 入口（现暂用根目录 main.py，后续迁移）
    ├── config/
    │   └── config.py        # Settings 全局配置（现暂用 core/config.py）
    ├── controller/          # 控制器层（接口路由）
    │   ├── spider_controller.py
    │   ├── knowledge_controller.py # 知识库+上传（现为 konwledge_controller.py）✅
    │   ├── embedding_controller.py # 触发向量化流水线 ✅
    │   ├── retrieval_controller.py # 混合检索调试 ✅
    │   ├── card_controller.py      # 每日学习卡片 ✅
    │   ├── jd_controller.py        # JD分析接口 ✅
    │   ├── evaluate_controller.py  # 知识点评估接口 ✅
    │   └── chat_controller.py      # 问答接口（讲解/费曼双模式）✅
    ├── service/             # 业务逻辑层
    │   ├── spider_service.py
    │   ├── knowledge_service.py
    │   ├── jd_service.py
    │   └── evaluate_service.py
    ├── dao/                 # 数据访问层（现目录名 DAO/）
    │   ├── knowledge_dao.py
    │   ├── jd_dao.py
    │   └── evaluate_dao.py
    ├── database/            # engine、sessionmaker、Base、get_db
    │   └── session.py
    ├── model/               # ORM 数据模型
    │   ├── KnowledgeModel.py       # 知识库表 ✅已建
    │   ├── JDModel.py
    │   ├── TechStackModel.py
    │   └── EvaluateModel.py
    ├── schema/              # Pydantic DTO
    │   ├── knowledge.py ✅
    │   ├── jd.py
    │   ├── evaluate.py
    │   └── chat.py                 # 对话请求/响应（含 mode 字段）
    ├── milvus/              # 向量库模块
    │   ├── ingestion/       # 向量入库流水线
    │   │   ├── loader.py           # 从MySQL读status=0的知识
    │   │   ├── spliter.py          # 切块（按段落/长度，带重叠）
    │   │   ├── embeddings.py       # 调 bge-m3 拿向量
    │   │   ├── VectorStore.py      # Milvus 集合封装
    │   │   └── pipeline.py         # 串起整条流水线，回写status
    │   └── retrieval/       # 检索模块
    │       ├── bm25.py             # BM25 关键词检索
    │       ├── retriever.py        # 向量检索
    │       ├── hybird.py           # 混合融合（RRF/加权）
    │       └── reranker.py         # Rerank 精排
    ├── generation/          # LLM 生成模块
    │   ├── llm.py                  # Chat 客户端（流式，双协议：OpenAI 兼容 / Anthropic Messages）
    │   ├── prompts.py              # 讲解模式/费曼模式提示词
    │   └── chain.py                # 检索结果+历史+提示词 组装
    ├── ocr/                 # OCR 模块（JD截图识别，视觉模型实现）✅
    │   └── ocr_client.py
    ├── spider/              # 分布式爬虫模块
    │   ├── scheduler.py     # 调度器（任务分发、队列管理）
    │   ├── worker.py        # 爬虫 Worker（并发爬取）
    │   ├── tech_spider.py   # 技术知识爬虫（具体爬虫逻辑）
    │   ├── task_queue.py    # 任务队列（Redis List）✅
    │   ├── deduplicator.py  # URL 去重器（Redis Set）✅
    │   ├── middleware.py    # 中间件（UA轮换、重试、退避）✅
    │   ├── sites.py         # 站点配置 ✅
    │   └── spider_util.py   # 抓取/解析工具 ✅
    ├── evaluate/            # 知识点评估模块 ✅
    │   ├── evaluator.py     # 评分文本解析（正则+LLM兜底）与落库
    │   ├── prompts.py       # 兜底提取提示词
    │   └── rubric.py        # 0-10 分 → 掌握档位
    ├── jd_analyzer/         # JD分析模块 ✅
    │   ├── analyzer.py
    │   └── prompts.py
    ├── interview/           # 模拟面试模块 ✅（JD+简历双图）
    │   ├── analyzer.py      # 简历结构化 + JD-简历差距计算
    │   └── prompts.py       # 面试官/总评/简历提取提示词
    ├── util/                # 工具函数
    │   └── thread_pool_util.py
    └── core/                # 核心公共模块（现 config.py 所在）
```

---

## 五、数据库设计

### 5.1 knowledge 知识库表（✅ 已建）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | 主键 |
| title | varchar(512) | 标题 |
| content | longtext | 正文（纯文本）。原为 TEXT，因 64KB 装不下长页面（如 release-notes 385KB）扩为 LONGTEXT |
| source_url | varchar(768) UNIQUE | 来源链接（同 URL 不重复入库） |
| category | varchar(128) | 技术分类：fastapi / python / pydantic / ai… |
| source_type | varchar(32) | 来源类型：spider / upload |
| status | int | 0=待向量化，1=已向量化，2=失败 |
| created_at / updated_at | datetime | 时间戳 |

### 5.2 后续建的表（✅ 均已建）

- **tech_term（技术术语表，每日卡片数据源）**：`term`（术语，唯一，如 aigc）、`alias`（别名，逗号分隔）、`category`、`brief`（一句话简介）、`detail`（详细讲解）、`example`（示例）、`source_url`。由 `scripts/seed_terms.py` 从已入库文章标题 + LLM 辅助提炼，归一化判重（忽略大小写/空格/连字符），可重复执行扩充；`--enrich` 模式为存量术语补 `detail`/`example`。
- **jd / tech_stack（JD 分析，阶段 8）**：`jd` 存截图路径 + OCR 文本 + 职位标题/概括 + 分析原文；`tech_stack` 按 `jd_id` 存技术条目（名称/分类/required-bonus/JD 语境）。
- **evaluate（费曼讲解评分记录，阶段 9）**：`session_id`、`topic`、`rounds`（追问轮数）、`score`（0-10，可空）、`summary`（总结复述）、`correct_points` / `wrong_points` / `missed_points`（JSON 数组）、`raw`（评分 markdown 原文）。由对话接口在评分产生时联动写入。

---

## 六、开发顺序与当前进度

| # | 阶段 | 状态 | 说明 |
|---|---|---|---|
| 1 | MySQL 底层 | ✅ 完成 | `database/session.py`、`scripts/init_db.py`、knowledge 表已建 |
| 2 | FastAPI 骨架 | ✅ 完成 | 根目录 `main.py`，health/列表/创建接口已通 |
| 3 | 分布式爬虫 | ✅ 完成 | 底层五件 + `tech_spider.py` / `worker.py` / `scheduler.py` + `scripts/run_spider.py` 全部就绪 |
| 4 | 文档上传入库 | ✅ 完成 | `POST /api/knowledge/upload`，md/txt 校验 + 落盘 + upsert（伪 URL 作唯一键） |
| 5 | 切块 + 向量化 + Milvus | ✅ 完成 | `milvus/ingestion/` 五件套 + `POST /api/embedding/run`，4700+ 块入库，幂等重跑 |
| 6 | 检索（BM25→向量→混合→Rerank） | ✅ 完成 | `milvus/retrieval/` 四件套 + `POST /api/retrieval/search`，RRF 融合 |
| 7 | LLM 双模式问答（流式+记忆） | ✅ 完成 | `generation/` + `chat_controller.py`，讲解/费曼双模式 + sessions 记忆 |
| 7.5 | 每日学习卡片 | ✅ 完成 | `tech_term` 表 + `GET /api/card/today`，Redis 按日期缓存 + 日期种子（飞书原计划无，新增需求） |
| 8 | JD 分析（OCR+技术栈） | ✅ 完成 | `ocr/ocr_client.py`（视觉模型读图）+ `jd_analyzer/`（LLM 提炼结构化技术栈）+ `/api/jd/*` |
| 9 | 知识点评估 | ✅ 完成 | `evaluate/`（解析+评分规则）+ evaluate 表 + `/api/evaluate/*`，与费曼评分联动落库 |
| 10 | 前端 | ✅ 完成 | 原生 HTML/CSS/JS：主页（每日卡片+跳转）+ 对话页（双模式切换、SSE 流式、评分卡片），端口 10001 |

### 爬虫模块已完成部分清点

| 文件 | 职责 |
|---|---|
| `middleware.py` | UA 池随机轮换 + 失败重试 + 线性退避 |
| `spider_util.py` | fetch_html / parse_article（标题+正文+同站链接）/ crawl_one |
| `task_queue.py` | Redis List 分布式队列（rpush 进、blpop 阻塞出，多 worker 安全） |
| `deduplicator.py` | Redis Set URL 去重（sadd 原子性，多 worker 安全） |
| `sites.py` | 站点配置（fastapi 中文文档、python 官方教程），生成初始任务 |
| `tech_spider.py` | 技术知识爬虫：任务 → `crawl_one` 抓取解析 → `allowed_prefix` 过滤站内链接；正文 < 200 字判为无效不入库 |
| `worker.py` | 爬虫 Worker：blpop 取任务 → upsert 入库（独立 Session）→ 新链接去重回灌；连续空轮自动退出 |
| `scheduler.py` | 调度器：`seed()`（入口灌队列+写去重集合，支持全量/增量）、`run()`、`status()`（供后续 `/api/spider/status`） |
| `scripts/run_spider.py` | 启动脚本：`-w` 并发数、`--reset` 全量重爬 |

---

## 七、核心模块设计

### 7.1 爬虫模块（阶段 3）

**运行流程**：

```text
scheduler.seed()  ── 读 sites.py 入口 ──▶ Redis 队列（入口URL同时写入去重集合）
      │
      ▼
worker × N（线程并发）
      │  blpop 取任务
      ▼
tech_spider.crawl(task)  ── 抓取解析 ──▶ 正文太短丢弃；否则 DAO.upsert 入库（status=0）
      │
      ▼
新链接 ── dedup.add 过滤 ──▶ 回灌队列（只保留 allowed_prefix 内的站内链接）
```

**要点**：
- 每个 worker 用独立的 `SessionLocal()` 会话（线程安全）
- 队列连续空 N 轮后 worker 自动退出，爬虫自然结束
- 入库统一走 `KnowledgeDAO.upsert`：URL 已存在且内容有变才更新，状态重置为「待向量化」

**可扩展站点建议**（加到 `sites.py`）：
- pydantic 官方文档 `https://docs.pydantic.dev/latest/`
- AI 方向可从 Hugging Face blog、或自建 AI 术语文本批量导入（配合阶段 4 上传）

### 7.2 切块 + 向量化（阶段 5）

- `spliter.py`：递归切块，优先按段落/标题边界切；`chunk_size ≈ 500 字`，`overlap ≈ 50 字`；每块记录所属文档（knowledge_id、标题、source_url）
- `embeddings.py`：OpenAI 兼容接口调 `BAAI/bge-m3`，批量请求，失败重试
- `VectorStore.py`：封装 Milvus——建 collection（id、向量、knowledge_id、chunk 文本、category）、insert、search
- `pipeline.py`：扫 `status=0` 的知识 → 切块 → 向量化 → 写 Milvus → 回写 `status=1`（异常置 2）

### 7.3 混合检索（阶段 6）

```text
query ──┬── BM25（jieba分词 + rank_bm25，top 20）──┐
        └── 向量检索（Milvus，top 20）────────────┤
                                                  ▼
                              RRF 融合（或加权求和）── top 30
                                                  ▼
                              Rerank 模型精排 ── top 5 ──▶ 拼进 Prompt
```

- BM25 语料缓存落盘到 `data/`（知识库更新后重建）
- 检索时按 `category` 可选过滤

### 7.4 双模式对话（阶段 7，重点）

请求统一入口：`POST /api/chat`，body 含 `mode` 字段。

**① 讲解模式（mode="ask"）——AI 当老师**

- System Prompt 要点：你是资深技术导师；只基于【检索片段】回答，片段不足时明确说明；用"是什么→为什么→怎么用→示例"的结构讲；用户可能是初学者，避免堆术语。
- 流程：用户发 `aigc` → 检索 top5 片段 → 组装（系统提示 + 历史 + 片段 + 问题）→ 流式返回。

**② 费曼模式（mode="teach"）——AI 当学生**

- System Prompt 要点：你是好奇但严谨的学生，用户是老师。你先表示自己对这个主题感兴趣但基础有限；听讲解时每轮只做一件事：提问 / 指出含糊处 / 举反例追问；不要替老师把答案说出来。
- 流程：
  1. 用户选定主题（如 `aigc`），后台先检索该主题知识点备用（作为"标准答案"，不直接展示）
  2. 用户开始讲解，AI 逐轮追问（最多 N 轮）
  3. 用户说「结束」或 AI 判断讲完 → 输出：**总结复述 + 掌握度评分（1-10）+ 讲对/讲错/遗漏的知识点清单**
- 评分记录落库（阶段 9 已联动）：评分产生时由 `evaluate/evaluator.py` 解析（正则优先、LLM 兜底）写入 evaluate 表
- 注意：**总结评分轮要切换成「评分员」系统提示**（退出学生人设）。沿用学生人设时模型会抗拒结束、继续追问，导致评分格式解析失败

**记忆**：会话历史按 `session_id` 以 JSON 存 `sessions/` 目录（`{id}_{mode}.json`）。讲解/费曼两种模式历史完全分开：侧边栏按模式各自列出、独立新建/删除（删除按模式删单个文件）；对话页工具栏有显眼的当前模型徽标，用户可在 ⚙️ 自配 OpenAI/Anthropic 模型（随请求透传 `llm` 字段）。

**③ 双协议接入（v0.4 新增）——OpenAI 兼容 / Anthropic Messages**

- `generation/llm.py` 的 `ChatLLM` 支持两种协议，由 `.env` 配置切换，用户可自由填入任意服务商的地址与密钥：
  - `CHAT_PROVIDER=openai`：OpenAI 兼容协议（SiliconFlow、DeepSeek、Moonshot、OpenAI 等），httpx 直连 `POST /chat/completions`
  - `CHAT_PROVIDER=anthropic`：Anthropic Messages 协议（Claude 官方或中转站），走官方 `anthropic` SDK 的 `messages.stream`
  - `CHAT_PROVIDER=auto`（默认）：按 `CHAT_BASE_URL` 识别——地址含 `anthropic` 走 Messages 协议，否则按 OpenAI 兼容
- 协议差异在 `ChatLLM` 内部屏蔽，上层（`chain.py` / `chat_controller.py`）不感知：
  - OpenAI 风格的 `messages`（含 system 角色）发给 Anthropic 时自动转换：system 抽成独立参数、连续同角色消息合并（费曼结束轮有连续 user）、`max_tokens` 必填、新 Claude 模型不接受 `temperature` 故 anthropic 路径不传
- 新增依赖：`anthropic`（官方 SDK）；本地无 Claude 密钥时可用 `scripts/mock_anthropic_server.py` 起 mock 服务验证 anthropic 路径

### 7.5 每日学习卡片（阶段 7.5）

- 数据源：`tech_term` 表
- 接口：`GET /api/card/today`
  - Redis key：`daily:card:YYYY-MM-DD`
  - 命中 → 直接返回；未命中 → 随机抽一条（用日期做随机种子，保证当天稳定）→ 写回 Redis，过期时间设到当天 24:00
- 同一天多次刷新卡片内容不变；换天自动换新
- 前端主页卡片展示：术语 + 一句话简介 + 「去问 AI」跳转按钮

---

## 八、接口规划

| 模块 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 知识库 | GET | `/api/knowledge/` | 分页列表（已实现） |
| 知识库 | GET | `/api/knowledge/categories` | 分类聚合计数（知识库页） |
| 知识库 | GET | `/api/knowledge/{id}` | 单条详情含正文（知识库页弹窗） |
| 知识库 | POST | `/api/knowledge/` | 手动创建（已实现） |
| 爬虫 | POST | `/api/spider/start` | 启动爬虫（阶段 3 收尾可选） |
| 爬虫 | GET | `/api/spider/status` | 队列长度/已访问数 |
| 上传 | POST | `/api/knowledge/upload` | 文档上传入库（阶段 4） |
| 向量化 | POST | `/api/embedding/run` | 触发入库流水线（阶段 5） |
| 检索 | POST | `/api/retrieval/search` | 调试用检索接口（阶段 6） |
| 对话 | POST | `/api/chat` | 双模式对话，SSE 流式（阶段 7）；body 可带 `llm` 自定义模型配置 |
| 对话 | GET | `/api/chat/history` | 拉取会话历史 |
| 对话 | POST | `/api/chat/ping` | 用户自定义模型连通性测试（双协议） |
| 对话 | GET | `/api/chat/sessions` | 会话列表（多会话管理） |
| 对话 | DELETE | `/api/chat/sessions/{id}` | 删除会话 |
| 面试 | POST | `/api/interview/start` | 上传 JD+简历截图 → OCR → 分析 → 首问（模拟面试） |
| 面试 | POST | `/api/interview/answer` | SSE 逐轮点评+追问；结束→总评+推荐学习卡片 |
| 卡片 | GET | `/api/card/today` | 每日学习卡片（阶段 7.5） |
| JD 分析 | POST | `/api/jd/analyze` | 上传 JD 截图 → OCR → 技术栈提取（阶段 8） |
| JD 分析 | GET | `/api/jd/{jd_id}` / `/api/jd/` | 单条结果 / 记录列表（阶段 8） |
| 评估 | GET | `/api/evaluate/` | 评分记录列表，可按 topic 过滤（阶段 9） |
| 评估 | GET | `/api/evaluate/stats` | 聚合统计：总条数/平均分/各主题掌握情况（阶段 9） |
| 评估 | GET | `/api/evaluate/{id}` | 单条评分详情（阶段 9） |

---

## 九、环境配置（.env）

已使用：

```ini
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_KEY=sk-xxx          # 不要提交到 git！
EMBEDDING_BASE_URL=...        # Embedding API 地址（OpenAI 兼容，可带可不带 /embeddings 后缀）

MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE / MYSQL_CHARSET
MYSQL_POOL_SIZE / MYSQL_POOL_MAX_OVERFLOW
DATA_DIR=./data
SESSION_DIR=./sessions
REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB

MILVUS_HOST=127.0.0.1
MILVUS_PORT=19530
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_BASE_URL=              # 留空复用 EMBEDDING_BASE_URL 同域

# 对话大模型（双协议，用户自填接入点）
CHAT_MODEL=...                # 如 Qwen/Qwen2.5-72B-Instruct 或 claude-opus-4-8
CHAT_KEY=sk-xxx
CHAT_BASE_URL=...             # OpenAI 兼容地址或 Anthropic 地址（含中转站）
CHAT_PROVIDER=auto            # openai / anthropic / auto（按地址识别）

# 视觉 OCR（JD 截图识别，默认复用 Chat 的地址与密钥）
OCR_MODEL=Qwen/Qwen3-VL-32B-Instruct
OCR_BASE_URL=                 # 留空复用 CHAT_BASE_URL
OCR_KEY=                      # 留空复用 CHAT_KEY
```

至此 `.env` 所需配置已全部就位，无后续规划项。

---

## 十、启动方式

```bash
# 1. 建表（首次）
uv run python scripts/init_db.py

# 2. 启动后端 API（端口 4399）
uv run python main.py

# 3. 启动前端（端口 10001；原计划 10000 与百度网盘检测服务冲突）
uv run python scripts/run_frontend.py
# 浏览器打开  http://127.0.0.1:10001/

# 4. 启动爬虫（需先启动 Redis 和 MySQL）
uv run python scripts/run_spider.py            # 默认 2 个 worker，增量
uv run python scripts/run_spider.py -w 3 --reset   # 3 个 worker，全量重爬
```

**端口约定**：后端 `4399`（CORS 已放开前端源），前端静态站 `10001`。
前端用原生 HTML/CSS/JS（无框架），`js/api.js` 按 `location.hostname:4399` 拼后端基址，本机和局域网访问均可用。

---

## 十一、遗留问题 / 注意事项

1. **目录拼写**：现有 `seesions/` 应为 `sessions/`（与 `.env` 的 `SESSION_DIR` 一致），建议改名。
2. **文件拼写**：`konwledge_controller.py` 拼错了（knowledge），后续迁移 controller 时顺手改掉。
3. **密钥安全**：`.env` 里有真实密钥，确认 `.gitignore` 已忽略，切勿提交。
4. **大小写**：现目录 `DAO/` 与飞书架构的 `dao/` 不一致，暂保留，大整理时统一。
5. **Milvus Lite 单进程独占**：配置了 `MILVUS_LITE_PATH` 时，同一个 `.db` 文件同一时刻只能被一个进程打开（官方不支持多进程并发，见 milvus-lite issue #195/#264）。后端运行时不要另起进程跑 `vectorize.py`，否则后来者会报 `Fail connecting to server on 127.0.0.1:<随机端口>`（该端口是 Milvus Lite 拉起的本地子进程，不在任何配置里）。正确姿势：停后端再跑离线脚本，或直接用后端内置的 `POST /api/embedding/run`（进程内执行，无冲突）。`vectorize.py` 已内置后端存活检测；`deploy/setup.sh` 已改为「先向量化、后启后端」。另外混合检索已做降级：向量路不可用时自动退化为纯 BM25，对话不整体失败；`VectorStore` 为进程级单例（`get_vector_store()`），勿改回每请求新建。
