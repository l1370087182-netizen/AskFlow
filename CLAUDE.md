# 智能技术学习系统（RAG 学习版）开发文档

> 版本：v0.8（2026-09-01）
> 架构来源：飞书文档《RAG 项目架构》（本地权威副本：`.cursor/rules/rag-feishu-architecture.mdc`）
> 协作约定：助手可直接读写仓库代码（v0.3 起取消「代码全部手写」限制），改动需与本文档的架构和目录约定保持一致，关键决策做简要说明。
> v0.8 新增：用户鉴权（邮箱验证码注册/登录/忘记密码）+ 全部用户数据按账号隔离（对话/评估/面试/模型配置），见「阶段 11」。

---

## 一、项目定位

一个「边爬边学」的 AI 技术知识学习系统：

1. **爬取**：从互联网自动爬取编程 / AI 相关技术文档（Python、FastAPI、Pydantic、AIGC……）
2. **加工**：对知识做切块（chunk）、Embedding 向量化，写入向量库（Milvus）
3. **检索**：BM25 关键词检索 + 向量语义检索 → 混合融合 → Rerank 重排序，找出最相关的知识片段
4. **对话学习**（两种模式）：
   - **讲解模式（问）**：我发 `aigc`，AI 当老师，基于知识库把我讲明白
   - **费曼模式（教）**：我发 `aigc`，AI 变学生，我当老师讲给它听；它负责追问、挑毛病、最后总结并给我的讲解打分
5. **学习卡片**：主页展示一个技术知识点，点「换一个」手动刷新（原为每日自动换，2026-09 改），碎片化学习

---

## 二、技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | 对外接口、流式响应 |
| 参数校验/配置 | pydantic / pydantic-settings | DTO（schema）、`.env` 全局配置 |
| 关系库 | MySQL + SQLAlchemy + PyMySQL | 知识原始数据、会话/卡片等业务记录 |
| 缓存/队列 | Redis | 爬虫任务队列、URL 去重、学习卡片缓存 |
| 爬虫 | httpx + BeautifulSoup + lxml | 抓取、解析、扩链 |
| 联网搜索 | 博查 Bocha web-search API（httpx） | 联网搜索补爬：生成检索词→搜索候选→过滤→爬取 |
| 向量库 | Milvus（pymilvus） | 向量存储与 ANN 检索 |
| Embedding | BAAI/bge-m3（OpenAI 兼容接口） | 文本向量化（1024 维） |
| 关键词检索 | rank_bm25 + jieba | BM25，与向量结果混合 |
| 重排序 | bge-reranker（API） | 混合结果精排 |
| LLM | 大模型 Chat API（流式，双协议） | 双模式对话生成；OpenAI 兼容协议（httpx）+ Anthropic Messages 协议（官方 anthropic SDK），用户可自配 base_url/api_key/model |
| 鉴权 | PyJWT + 标准库（pbkdf2/smtplib）+ cryptography(Fernet) | 用户注册/登录/忘记密码、JWT 凭证、密码哈希、邮箱验证码、私有 api_key 加密（仅新增 PyJWT 一个依赖） |

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
  │ 学习卡片    │ ◀── │ 知识/术语     │     │ Milvus 向量库        │
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
│   ├── index.html           # 主页：学习卡片
│   ├── chat.html            # 对话页：讲解/费曼双模式
│   ├── kb.html              # 知识库页：分类聚合+条目列表+正文弹窗
│   ├── interview.html       # 模拟面试页：双图上传+逐轮追问
│   ├── login.html           # 登录/注册/忘记密码三合一（阶段11）
│   ├── css/style.css
│   └── js/                  # api.js / home.js / chat.js / kb.js / interview.js /
│                            # login.js / topbar-user.js（登录态+顶栏用户区）/ highlight.js
└── src/
    ├── main.py              # FastAPI 入口（现暂用根目录 main.py，后续迁移）
    ├── config/
    │   └── config.py        # Settings 全局配置（现暂用 core/config.py）
    ├── controller/          # 控制器层（接口路由）
    │   ├── spider_controller.py
    │   ├── knowledge_controller.py # 知识库+上传（现为 konwledge_controller.py）✅
    │   ├── embedding_controller.py # 触发向量化流水线 ✅
    │   ├── retrieval_controller.py # 混合检索调试 ✅
    │   ├── card_controller.py      # 学习卡片（手动刷新） ✅
    │   ├── jd_controller.py        # JD分析接口 ✅
    │   ├── evaluate_controller.py  # 知识点评估接口 ✅
    │   ├── chat_controller.py      # 问答接口（讲解/费曼双模式）✅
    │   ├── interview_controller.py # 模拟面试（JD+简历双图）✅
    │   ├── auth_controller.py      # 注册/登录/忘记密码/当前用户 ✅（唯一免鉴权）
    │   └── user_controller.py      # 用户私有模型配置 /api/user/llm ✅
    ├── service/             # 业务逻辑层
    │   ├── spider_service.py
    │   ├── knowledge_service.py
    │   ├── jd_service.py
    │   └── evaluate_service.py
    ├── dao/                 # 数据访问层（现目录名 DAO/）
    │   ├── knowledge_dao.py
    │   ├── jd_dao.py
    │   ├── evaluate_dao.py
    │   ├── tech_term_dao.py
    │   └── user_dao.py             # 用户账号/改密/私有模型配置 ✅
    ├── database/            # engine、sessionmaker、Base、get_db
    │   └── session.py
    ├── model/               # ORM 数据模型
    │   ├── KnowledgeModel.py       # 知识库表 ✅已建
    │   ├── JDModel.py              # +user_id（阶段11）
    │   ├── TechStackModel.py
    │   ├── EvaluateModel.py        # +user_id（阶段11）
    │   ├── TechTermModel.py
    │   └── UserModel.py            # 用户账号+私有模型配置 ✅（阶段11）
    ├── schema/              # Pydantic DTO
    │   ├── knowledge.py ✅
    │   ├── jd.py
    │   ├── evaluate.py
    │   ├── auth.py                 # 注册/登录/验证码/用户/模型配置 DTO ✅（阶段11）
    │   └── chat.py                 # 对话请求/响应（含 mode 字段）
    ├── auth/                # 用户鉴权模块 ✅（阶段11）
    │   ├── security.py     # pbkdf2 密码哈希 + JWT + Fernet 加解密
    │   ├── deps.py         # get_current_user 依赖（Bearer）
    │   └── mailer.py       # 邮箱验证码（Redis 存取/限流 + SMTP 发送）
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
    │   ├── shallow_crawler.py  # 浅层爬虫（整站 BFS；iter_urls 显式列表直抓）✅
    │   └── spider_util.py   # 抓取/解析工具 ✅
    ├── search/              # 联网搜索补爬模块 ✅（新）
    │   ├── web_search.py    # 博查客户端 + 生成检索词/过滤候选/提交检索任务
    │   └── prompts.py       # 检索词生成 + 候选网页过滤提示词
    ├── agents/              # 任务引擎的各角色 Agent（生产/质检/规划/检索）
    │   ├── producer.py      # 爬取生产者（含取消探针，逐页检查）
    │   ├── searcher.py      # 联网检索：生成query→搜索→过滤→派生子爬取 ✅（新）
    │   ├── planner.py       # 学习规划（含相关度阈值过滤）
    │   └── reviewer.py / curator.py
    ├── agent_engine/        # 任务引擎（BaseAgent 线程 + manager 装配 + reaper 回收）
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
- **evaluate（费曼讲解评分记录，阶段 9）**：`session_id`、`topic`、`rounds`（追问轮数）、`score`（0-10，可空）、`summary`（总结复述）、`correct_points` / `wrong_points` / `missed_points`（JSON 数组）、`raw`（评分 markdown 原文）。由对话接口在评分产生时联动写入。**阶段 11 加 `user_id` 列（可空、索引），按用户隔离；存量行留 NULL 作废。**
- **user（用户账号，阶段 11）**：`email`（唯一，登录名）、`password_hash`（pbkdf2_sha256）、`nickname`、`token_ver`（改密 +1 使旧 JWT 失效）、`llm_provider` / `llm_base_url` / `llm_api_key_enc`（Fernet 密文）/ `llm_model`（用户私有模型配置，空=用服务端默认）、`created_at` / `updated_at`。
- **jd 表（阶段 11 补 `user_id` 列）**：同 evaluate，按用户隔离，存量行留 NULL 作废。

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
| 7.5 | 学习卡片 | ✅ 完成 | `tech_term` 表 + `GET /api/card/today` + `POST /api/card/refresh`（手动刷新「换一个」，原每日缓存制已于 2026-09 移除）；对话链路有术语卡片兜底 |
| 8 | JD 分析（OCR+技术栈） | ✅ 完成 | `ocr/ocr_client.py`（视觉模型读图）+ `jd_analyzer/`（LLM 提炼结构化技术栈）+ `/api/jd/*` |
| 9 | 知识点评估 | ✅ 完成 | `evaluate/`（解析+评分规则）+ evaluate 表 + `/api/evaluate/*`，与费曼评分联动落库 |
| 10 | 前端 | ✅ 完成 | 原生 HTML/CSS/JS：主页（每日卡片+跳转）+ 对话页（双模式切换、SSE 流式、评分卡片），端口 10001 |
| 11 | 用户鉴权 + 数据隔离 | ✅ 完成 | 邮箱验证码注册/登录/忘记密码；对话/评估/面试/模型配置按用户隔离；仅新增 PyJWT 依赖 |
| 12 | 相关度阈值 + 联网搜索补爬 + 任务治理 | ✅ 完成 | 阈值判「无资料」；博查联网补爬（SearcherAgent，异步）；取消真正生效（CAS+探针+级联）；任务板进度条 |

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

**记忆**：会话历史按「用户 + 会话」以 JSON 存 `sessions/{user_id}/{id}_{mode}.json`（阶段 11 起加用户子目录，天然隔离他人会话）。讲解/费曼两种模式历史完全分开：侧边栏按模式各自列出、独立新建/删除。对话页工具栏有显眼的当前模型徽标，用户可在 ⚙️ 自配 OpenAI/Anthropic 模型——**配置存服务端 user 表（api_key Fernet 加密），按用户隔离，不再随请求透传**（`/api/chat` 不再消费请求体 `llm` 字段，改读当前登录用户的库内配置）。

**③ 双协议接入（v0.4 新增）——OpenAI 兼容 / Anthropic Messages**

- `generation/llm.py` 的 `ChatLLM` 支持两种协议，由 `.env` 配置切换，用户可自由填入任意服务商的地址与密钥：
  - `CHAT_PROVIDER=openai`：OpenAI 兼容协议（SiliconFlow、DeepSeek、Moonshot、OpenAI 等），httpx 直连 `POST /chat/completions`
  - `CHAT_PROVIDER=anthropic`：Anthropic Messages 协议（Claude 官方或中转站），走官方 `anthropic` SDK 的 `messages.stream`
  - `CHAT_PROVIDER=auto`（默认）：按 `CHAT_BASE_URL` 识别——地址含 `anthropic`，或以已知 Anthropic 协议端点结尾（火山方舟 Agent Plan `/api/plan`、Coding Plan `/api/coding`、DouBaoSeed `/api/compatible`、Kimi For Coding `api.kimi.com/coding`；对应 `/v3`、`/v1` 结尾变体是 OpenAI 兼容）走 Messages 协议，否则按 OpenAI 兼容
- 协议差异在 `ChatLLM` 内部屏蔽，上层（`chain.py` / `chat_controller.py`）不感知：
  - OpenAI 风格的 `messages`（含 system 角色）发给 Anthropic 时自动转换：system 抽成独立参数、连续同角色消息合并（费曼结束轮有连续 user）、`max_tokens` 必填、新 Claude 模型不接受 `temperature` 故 anthropic 路径不传
- 新增依赖：`anthropic`（官方 SDK）；本地无 Claude 密钥时可用 `scripts/mock_anthropic_server.py` 起 mock 服务验证 anthropic 路径

### 7.5 学习卡片（阶段 7.5，原「每日卡片」——2026-09 改手动刷新）

- 数据源：`tech_term` 表（全局术语 + 本人个人术语）
- 接口：`GET /api/card/today`（当前卡片）+ `POST /api/card/refresh`（换一个）
  - Redis key：`card:current:v1:{user_id}`（**无 TTL**：卡片常驻，不按天换）
  - today 命中 → 直接返回；未命中 → 随机抽一条写回
  - refresh → 排除当前这张随机换新（只剩一张时原样返回）
- 重进主页看到的还是上一次那张，点「换一个」才换
- **术语兜底（卡片 → 对话联动）**：讲解模式每条消息、费曼模式选题时，都会在消息里做术语匹配（词边界正则，命中多个取名字最长的），命中就把 `tech_term` 的 brief/detail/example 以【术语卡片】块拼进参考片段——术语表与 knowledge 语料是两条独立链路，此联动保证「卡片里有的知识，问 AI 不至于答知识库没有」。见 `generation/chain.py` 的 `match_term`/`term_context`
- 前端主页卡片展示：术语 + 一句话简介 + 「换一个」按钮 + 「去问 AI」跳转按钮

### 7.6 用户鉴权与数据隔离（阶段 11，重点）

**凭证**：`Authorization: Bearer <JWT>`（HS256，payload 含 sub/exp/iat/ver）。因 CORS `allow_credentials=False`（跨域 cookie 走不通），统一用 Bearer 头；前端 `api.js` 四个请求函数统一注入，401 → 清 token 跳 `login.html`。

**注册/登录/忘记密码**（`/api/auth/*`，唯一免鉴权路由）：
- `POST /send-code {email, purpose: register|reset}`：邮箱发验证码（6 位，Redis 存 300s）。限流：60s 重发冷却、单邮箱日 10 封、校验错 5 次作废。`purpose=register` 查重、`reset` 查存在。
- `POST /register {email, code, password}`：验证码 `GETDEL` 原子消费（防双花）+ 建号，返回 token。密码 6–64 位。
- `POST /login`：统一话术「邮箱或密码错误」（防邮箱枚举）。
- `POST /reset`：改密同时 `token_ver += 1`，旧 JWT 立即失效。
- `GET /me`：当前用户（前端 topbar 展示 + token 探活）。
- 邮件：`SMTP_*` 配置走 `smtplib`；**未配置时验证码 `print` 到后端控制台**（本地开发兜底）。

**鉴权依赖**：`auth/deps.py` `get_current_user`（`HTTPBearer(auto_error=False)`，统一抛 401）。`main.py` 里 `include_router(..., dependencies=[Depends(get_current_user)])` 挂到除 auth 外全部路由；端点内再次 `Depends(get_current_user)` 命中请求内缓存，只查一次库。SSE 端点鉴权在流建立前完成，闭包只捕获 `uid: int`。

**数据隔离**：
- 对话/面试会话：`_session_path` 加 `user_id` 维度 → `sessions/{uid}/`。越权防护靠路径而非校验（拿他人 session_id 拼到自己目录，读=空、删=0）。面试 session_id 改 `secrets.token_urlsafe` 强随机。
- evaluate / jd：加 `user_id` 列，全部查询按用户过滤；越权查详情返回 404。`card/overview` 的 evals/平均分按用户，知识/术语保持全局。
- 用户私有模型配置：`GET/PUT /api/user/llm`；api_key Fernet 加密落库，读接口只回脱敏值（`sk-a***wxyz`），绝不回明文。

**关键坑**：`Base.metadata.create_all` 不给已存在表补列，`scripts/init_db.py` 用 `ensure_column` 对 evaluate/jd 补 `user_id`（幂等）。改密踢下线靠 `token_ver`。Fernet 解密失败返回空串（视为未配置自定义模型），不让对话接口 500。

### 7.7 相关度阈值判定 + 联网搜索补爬（新增）

**① 相关度阈值（判定「知识库无资料」）**
- 混合检索永远返回 top-k，哪怕全不相关也给「最不相关里最靠前」的块。因此用 **rerank 相关度阈值** `hybird.RELEVANCE_MIN_SCORE=0.3`：`relevant_hits()` 过滤低于阈值的命中（只过滤带 `rerank_score` 的；rerank 挂掉的 rrf 降级分不过滤，避免降级期误杀）。
- 讲解模式 `chain.build_ask` 检索结果先过阈值——**过滤后为空即「知识库无资料」**，触发联网补爬 + 让模型用自身知识简答（回答开头标注「非知识库资料」）。
- 任务板/学习计划编引用（`planner._collect_refs/_search_refs`）同口径过滤。

**② 联网搜索补爬（AI 生成 query → 搜索引擎 → 过滤 → 爬取，异步）**
- 触发点两处：讲解对话检索低于阈值（`source=chat`）；任务板子题缺资料（`planner._auto_crawl_topic`，`source=board`）。
- 链路：`submit_web_search` 建 `agent_task(kind=web_search)` → `SearcherAgent` 异步执行「生成检索词（用户模型）→ 博查搜索 → 用户模型按标题/摘要/URL 过滤候选（含 SSRF 校验 + 与本人/全局知识库去重）」→ 选中的页打包成**子 `crawl` 任务**（`parent_id` 关联）交给 `ProducerAgent` 爬取入库。
- 全程异步不阻塞：提交即返；对话只多推一个 `kb_gap` SSE 事件。
- **取消真正生效**（配套修复）：`cancel_task` CAS 化；`producer`/`searcher` 逐页/逐阶段用 `dao.heartbeat` 返回值做取消探针（False→读 DB 定性后终止，不 write_back、不派生孤儿任务）；任务板取消会级联取消子题引用的爬取/检索链（`_cancel_crawl_chain`）。
- **降级**：未配 `SEARCH_API_KEY` / 无用户模型 / 活跃检索达上限 → 静默跳过补爬，主流程照常。
- 任务板进度：`GET /api/board/` 每个子题附 `crawl_progress`（页数百分比 + 当前 URL），前端用不定长动画条表示检索阶段、确定进度条表示爬取阶段。

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
| 个人爬取 | POST | `/api/knowledge/my/crawl` | 提交浅爬任务（202 + task_id，任务引擎异步） |
| 个人爬取 | GET | `/api/knowledge/my/crawl/{task_id}` | 爬取/检索进度（含 searching/canceled 态） |
| 个人爬取 | POST | `/api/knowledge/my/crawl/{task_id}/cancel` | 取消爬取/检索任务（运行中即终止） |
| 个人爬取 | GET | `/api/knowledge/my/crawl/active` | 活跃任务列表（进度面板恢复） |
| 任务板 | GET | `/api/board/` | 任务板视图（子题附 crawl_progress 进度条数据） |
| 任务板 | POST | `/api/board/goals` | 发布学习目标（planner 异步拆解） |
| 任务板 | POST | `/api/board/tasks/{id}/cancel` | 取消目标/子题，级联取消爬取/检索链 |
| 向量化 | POST | `/api/embedding/run` | 触发入库流水线（阶段 5） |
| 检索 | POST | `/api/retrieval/search` | 调试用检索接口（阶段 6） |
| 对话 | POST | `/api/chat` | 双模式对话，SSE 流式（阶段 7）；body 可带 `llm` 自定义模型配置 |
| 对话 | GET | `/api/chat/history` | 拉取会话历史 |
| 对话 | POST | `/api/chat/ping` | 用户自定义模型连通性测试（双协议） |
| 对话 | GET | `/api/chat/sessions` | 会话列表（多会话管理） |
| 对话 | DELETE | `/api/chat/sessions/{id}` | 删除会话 |
| 面试 | POST | `/api/interview/start` | 上传 JD+简历截图 → OCR → 分析 → 首问（模拟面试） |
| 面试 | POST | `/api/interview/answer` | SSE 逐轮点评+追问；结束→总评+推荐学习卡片 |
| 卡片 | GET | `/api/card/today` | 当前学习卡片（手动刷新制） |
| 卡片 | POST | `/api/card/refresh` | 换一张卡片（排除当前这张随机换） |
| JD 分析 | POST | `/api/jd/analyze` | 上传 JD 截图 → OCR → 技术栈提取（阶段 8） |
| JD 分析 | GET | `/api/jd/{jd_id}` / `/api/jd/` | 单条结果 / 记录列表（阶段 8） |
| 评估 | GET | `/api/evaluate/` | 评分记录列表，可按 topic 过滤（阶段 9，按用户隔离） |
| 评估 | GET | `/api/evaluate/stats` | 聚合统计：总条数/平均分/各主题掌握情况（阶段 9，按用户） |
| 评估 | GET | `/api/evaluate/{id}` | 单条评分详情（阶段 9，越权返 404） |
| 鉴权 | POST | `/api/auth/send-code` | 发送邮箱验证码（注册/重置，免鉴权） |
| 鉴权 | POST | `/api/auth/register` | 邮箱验证码注册，返回 token（免鉴权） |
| 鉴权 | POST | `/api/auth/login` | 登录，返回 token（免鉴权） |
| 鉴权 | POST | `/api/auth/reset` | 忘记密码：验证码重置密码，旧 token 失效（免鉴权） |
| 鉴权 | GET | `/api/auth/me` | 当前用户信息（token 探活） |
| 用户 | GET/PUT | `/api/user/llm` | 读/存当前用户私有模型配置（api_key 加密+脱敏返回） |

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

# 联网搜索补爬（博查；SEARCH_API_KEY 留空 = 静默关闭该功能）
SEARCH_PROVIDER=bocha         # 当前仅实现 bocha
SEARCH_API_KEY=               # 博查 API Key；留空禁用联网搜索补爬
SEARCH_BASE_URL=              # 覆盖地址；本地联调可指向 scripts/mock_bocha_server.py
SEARCH_MAX_RESULTS=8          # 每条检索词的候选网页数
SEARCH_MAX_KEEP=5             # LLM 过滤后最多保留并爬取的页数

# 用户鉴权（阶段 11）
AUTH_SECRET_KEY=...           # JWT 签名 + api_key 加密派生源，必填！生成：
                              #   python -c "import secrets;print(secrets.token_urlsafe(48))"
AUTH_TOKEN_TTL_MIN=1440       # token 有效期（分钟），默认 24h
FERNET_KEY=                   # 留空则由 AUTH_SECRET_KEY 派生

# 验证码邮件（SMTP 未配置时验证码只打印到后端控制台，便于本地开发）
SMTP_HOST=                    # 如 smtp.qq.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USER=                    # SMTP 登录账号（一般即发件邮箱）
SMTP_PASSWORD=                # SMTP 授权码（非邮箱登录密码）
SMTP_FROM=                    # 留空用 SMTP_USER
```

至此 `.env` 所需配置已全部就位。注：本地 `REDIS_PORT` 因 6379 落在 Windows Hyper-V 保留端口段（6346–6445）无法监听而改用 6500；服务器部署无此限制，可用 6379。

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
6. **阶段 11 会话目录迁移**：对话/面试会话改存 `sessions/{user_id}/` 子目录；`sessions/` 根目录下旧的 `{sid}_{mode}.json` 为鉴权前数据，作废不再读取（可手工清理）。鉴权前的 evaluate / jd 行 `user_id=NULL`，同样作废。
7. **本地 Redis 端口**：本机 6379 落在 Windows Hyper-V 保留端口段内无法监听，`.env` 用 `REDIS_PORT=6500`（Redis 跑在 WSL）；`deploy/` 服务器部署可用默认 6379，勿照搬本地端口。
