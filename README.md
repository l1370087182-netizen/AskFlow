# 问渠 AskFlow · Agentic RAG 学习系统

> **「问渠那得清如许，为有源头活水来」**——一个「边爬边学」的 AI 技术知识学习系统：
> 自动爬取技术文档 → 切块向量化 → 混合检索 → 大模型双模式对话学习；
> 再由多 Agent 任务引擎**消费知识、也生产知识**，形成闭环学习系统。

![Python](https://img.shields.io/badge/Python-3.13+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141+-009688)
![Milvus](https://img.shields.io/badge/Milvus-向量库-E34F26)
![MySQL](https://img.shields.io/badge/MySQL-8.0-4479A1)
![Redis](https://img.shields.io/badge/Redis-队列/缓存-DC382D)

---

## ✨ 核心特性

### 📚 知识引擎（RAG）

- **分布式爬虫**：Redis List 任务队列 + Set URL 去重，多 Worker 线程并发，UA 轮换 / 重试退避，正文过短自动丢弃
- **向量化流水线**：递归切块（≈500 字、50 字重叠）→ bge-m3 Embedding（1024 维）→ 写入 Milvus，幂等重跑
- **混合检索**：BM25（jieba 分词）+ 向量 ANN 双路召回 → RRF 融合 → bge-reranker 精排
- **相关度阈值**：rerank 分数低于阈值判定「知识库无资料」，不再把无关块误当命中；自动触发缺资料补爬
- **编排检索**：多查询改写 + 跨变体 RRF 融合，全程可降级（超时/失败自动放行原始结果，对话永不因此变慢）
- **联网搜索补爬**：知识库没有的，AI 生成检索词 → **博查联网搜索** → LLM 过滤无价值网页（含 SSRF 防护、库内去重）→ 异步爬取入库，知识缺口自动补齐
- **知识库翻译**：英文文档一键翻译（Edge 免密钥接口）

### 💬 对话学习（双模式 · SSE 流式 · 会话记忆）

- **讲解模式（AI 当老师）**：只基于检索片段作答，「是什么 → 为什么 → 怎么用 → 示例」结构化讲解
- **费曼模式（AI 当学生）**：你当老师讲给 AI 听，它逐轮追问挑毛病，结束后输出**总结复述 + 1-10 评分 + 讲对/讲错/遗漏清单**，评分自动落库
- **双协议接入**：OpenAI 兼容协议（httpx 直连）/ Anthropic Messages 协议（官方 SDK）。**服务端不内置默认模型**：用户注册后需在 ⚙️ 配置自己的模型（API 地址 / Key / 模型名，服务端 Fernet 加密存储），未配置时登录后自动弹窗引导；对话、面试、OCR、知识清洗统一走用户模型

### 🎯 学习闭环

- **学习卡片**：术语 + 一句话简介 + 示例，手动「换一个」，碎片化学习；对话中命中术语自动附卡片兜底
- **知识点评估**：费曼评分记录聚合统计，掌握度画像
- **JD 分析**：上传 JD 截图 → 视觉模型 OCR → LLM 提炼结构化技术栈
- **模拟面试**：JD + 简历双图上传 → OCR → 逐轮追问点评 → 总评 + **结构化弱项落库**（附答题原话证据）
- **学习任务板**：发布学习目标 / 面试结果一键上板 → `planner` 拆解任务树 → 检索知识库编写学习材料；**缺资料自动联网补爬**（检索 → 过滤 → 爬取全程进度条，显示爬到哪一页），随时可取消且真正停得下来，知识资产越用越厚

### 🤖 多 Agent 任务引擎

- **任务地基**：`agent_task` 表（MySQL 唯一真相源）+ 两层锁认领（Redis SETNX + DB CAS）+ 幂等写回 + 强制 work_log 审计 + 存储层状态机 + 超时回收器（重试 3 次耗尽即跳过，流水线不挂死）；**取消原子化**：取消即终止运行中任务（执行侧心跳探针逐页检查），级联取消整条爬取链，不产生孤儿任务
- **知识生产流水线**：`producer`（爬取 + AI 清洗，×3 并行）→ `reviewer`（质检：规则优先、模型兜底，≤2 轮）→ 向量化 → `curator`（术语提炼注册，反哺学习卡片）
- **联网检索**：`searcher` 生成检索词 → 博查搜索 → 过滤候选页 → 派生子爬取任务（先写回后建子任务，取消不遗留）
- **学习规划**：`planner` 消费面试记录 / 费曼评分 / 学习目标 → 弱项定位 → 拆解任务 → 编写材料

### 🔐 用户鉴权与数据隔离

- 邮箱验证码注册 / 登录 / 忘记密码（限流、防双花、防邮箱枚举）
- JWT（HS256 + `token_ver` 改密踢下线），全部用户数据按账号隔离：对话 / 评估 / 面试 / 个人知识库 / 学习任务 / 模型配置
- 管理员后台：`.env` 固定凭证，用户总览

---

## 🏗 系统架构

```text
                    ┌────────────── 学习任务板（发布目标 / 面试上板）─────────────┐
                    ▼                                                             │
 学习规划 planner ─（意图打标+缺口拆解）─▶ 缺口×N ─┬─▶ 混合检索（有资料）─▶ 编写学习材料
                                                 └─▶ 缺失 → 联网检索（搜索→过滤）─▶ 派生爬取子任务 ─┐
                                                                                                   │
 知识生产流水线（任务引擎，后台异步，可恢复可审计、可取消）：                                          │
   producer×3（爬取+清洗）→ reviewer（质检 ≤2 轮）→ 向量化 → curator（术语注册） ◀────────────────────┘
                    │
                    ▼
  ┌────────────┐  ┌──────────────┐  ┌─────────────────────┐
  │ 技术网站    │─▶│ 分布式爬虫    │─▶│ MySQL knowledge 表   │
  │ 文档/博客   │  │ Redis队列+去重│  │ （原始知识，待向量化） │
  └────────────┘  └──────────────┘  └──────────┬──────────┘
                                               │ 切块 + Embedding
                                               ▼
  ┌────────────┐  ┌──────────────┐  ┌─────────────────────┐
  │ 学习卡片    │◀─│ 知识/术语     │  │ Milvus 向量库        │
  └────────────┘  └──────────────┘  └──────────┬──────────┘
                                               │
       用户提问/讲解 ──▶ BM25 + 向量混合检索 ──▶ Rerank ──▶ 相关度阈值
                                               │              │
                                               │ 命中          │ 无资料 ──▶ 联网补爬（异步入库，再问即有）
                             ┌─────────────────┴────────────┐
                             ▼                              ▼
                      讲解模式（AI当老师）            费曼模式（AI当学生）
                             │                              │
                             └──── 评分/面试弱项 → planner → 学习计划 → 任务板（闭环）
```

**中心思想**：知识库是唯一真相资产。对话侧编排是同步函数组合（不进任务引擎、不牺牲延迟）；后台生产 / 质检 / 规划走任务引擎（可并行、可恢复、可审计）。

---

## 🧰 技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | API、SSE 流式响应 |
| 校验/配置 | pydantic / pydantic-settings | DTO、`.env` 全局配置 |
| 关系库 | MySQL + SQLAlchemy + PyMySQL | 知识、任务、用户等业务数据 |
| 缓存/队列 | Redis | 爬虫队列、URL 去重、任务认领锁、卡片缓存 |
| 爬虫 | httpx + BeautifulSoup + lxml | 抓取、解析、扩链 |
| 向量库 | Milvus / Milvus Lite（pymilvus） | 向量存储与 ANN 检索（Lite 供低内存部署） |
| Embedding | BAAI/bge-m3（OpenAI 兼容 API） | 文本向量化（1024 维） |
| 关键词检索 | rank_bm25 + jieba | BM25，与向量结果混合 |
| 重排序 | BAAI/bge-reranker-v2-m3 | 混合结果精排 |
| 联网搜索 | 博查 Bocha web-search API（httpx） | 联网补爬的候选网页来源 |
| LLM | Chat API 双协议 | OpenAI 兼容（httpx）+ Anthropic Messages（官方 SDK） |
| 鉴权 | PyJWT + pbkdf2 + Fernet | JWT 凭证、密码哈希、私有 api_key 加密 |
| 前端 | 原生 HTML / CSS / JS | 无框架静态站，端口 10001 |

---

## 📁 项目结构

```text
project/
├── main.py                  # FastAPI 入口（端口 4399）
├── frontend/                # 前端静态站（端口 10001）
│   ├── index.html           # 主页：学习卡片
│   ├── chat.html            # 对话学习：讲解/费曼双模式
│   ├── kb.html              # 知识库：分类列表 + 个人库 + 爬取面板
│   ├── interview.html       # 模拟面试：双图上传 + 逐轮追问 + 记录
│   ├── learning.html        # 学习任务板
│   ├── login.html           # 登录/注册/忘记密码
│   ├── admin.html           # 管理后台（用户总览）
│   └── js/                  # api / home / chat / kb / interview / learning / login / admin …
├── scripts/                 # 脚本：建表 / 爬虫 / 术语种子 / 探测 / 前端服务
├── deploy/                  # 服务器一键部署（nginx + systemd，不入库）
└── src/
    ├── core/config.py       # Settings 全局配置（.env）
    ├── controller/          # 控制器层（12 个路由模块）
    ├── service/             # 业务逻辑层
    ├── DAO/                 # 数据访问层
    ├── model/               # ORM 模型（knowledge/agent_task/user/interview_record/…）
    ├── schema/              # Pydantic DTO
    ├── auth/                # 鉴权：密码哈希 / JWT / Fernet / 邮箱验证码
    ├── spider/              # 分布式爬虫（队列/去重/中间件/站点配置）
    ├── milvus/
    │   ├── ingestion/       # 切块 → 向量化 → Milvus 入库流水线
    │   └── retrieval/       # BM25 / 向量 / RRF 混合 / Rerank
    ├── generation/          # LLM 双协议客户端 + 提示词 + 检索编排器
    ├── search/              # 联网搜索补爬（博查客户端 + 检索词生成/候选过滤提示词）
    ├── agent_engine/        # 多 Agent 任务引擎（两层锁/CAS/work_log/超时回收/取消探针）
    ├── agents/              # producer / searcher / reviewer / curator / planner
    ├── ocr/                 # 视觉模型读图（JD/简历）
    ├── interview/           # 模拟面试（简历结构化 + 差距分析 + 弱项提取）
    ├── jd_analyzer/         # JD 技术栈提取
    └── evaluate/            # 费曼评分解析（正则优先 + LLM 兜底）与评分规则
```

---

## 🚀 快速开始

### 环境要求

- Python **3.13+**（使用 [uv](https://github.com/astral-sh/uv) 管理依赖）
- MySQL 8.0+、Redis
- Milvus（或低内存场景用嵌入式 Milvus Lite，见下方 `.env`）
- 一个 OpenAI 兼容 / Anthropic 的大模型 API（Embedding + 对话 + Rerank）

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置 `.env`（项目根目录）

```ini
# ---- Embedding / Rerank（OpenAI 兼容接口）----
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_KEY=sk-xxx
EMBEDDING_BASE_URL=https://xxx/v1/embeddings
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_BASE_URL=                    # 服务根地址（代码自动拼 /rerank），留空复用 EMBEDDING_BASE_URL 同域
RERANK_KEY=                         # 留空复用 EMBEDDING_KEY

# ---- MySQL / Redis ----
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=xxx
MYSQL_DATABASE=RAG
MYSQL_CHARSET=utf8mb4
MYSQL_POOL_SIZE=5
MYSQL_POOL_MAX_OVERFLOW=10
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0

# ---- Milvus：二选一 ----
MILVUS_HOST=127.0.0.1               # standalone 模式
MILVUS_PORT=19530
MILVUS_LITE_PATH=                   # 非空则用嵌入式 Milvus Lite（本地文件路径）

# ---- 对话大模型：可留空 ----
# 产品策略：服务端不内置默认模型，每个用户登录后在 ⚙️ 配置自己的模型；
# 留空即无服务端兜底。如需恢复默认兜底再填写：
CHAT_MODEL=                         # 如 Qwen/Qwen2.5-72B-Instruct 或 claude-opus-4-8
CHAT_KEY=
CHAT_BASE_URL=                      # OpenAI 兼容地址或 Anthropic 地址（含中转站）
CHAT_PROVIDER=auto                  # openai / anthropic / auto（按地址识别）

# ---- 视觉 OCR（同对话：用用户 ⚙️ 模型；以下为可选服务端兜底，一般留空）----
OCR_MODEL=
OCR_BASE_URL=                       # 留空复用 CHAT_BASE_URL
OCR_KEY=                            # 留空复用 CHAT_KEY

# ---- 联网搜索补爬（博查；SEARCH_API_KEY 留空 = 静默关闭该功能）----
SEARCH_PROVIDER=bocha               # 当前仅实现 bocha（预留多供应商）
SEARCH_API_KEY=                     # 博查 AI 搜索开放平台的 API Key；留空则禁用联网补爬
SEARCH_BASE_URL=                    # 覆盖地址；本地联调可指向 scripts/mock_bocha_server.py
SEARCH_MAX_RESULTS=8                # 每条检索词的候选网页数
SEARCH_MAX_KEEP=5                   # LLM 过滤后最多保留并爬取的页数

# ---- 用户鉴权 ----
AUTH_SECRET_KEY=                    # 必填！生成：python -c "import secrets;print(secrets.token_urlsafe(48))"
AUTH_TOKEN_TTL_MIN=1440
FERNET_KEY=                         # 留空则由 AUTH_SECRET_KEY 派生

# ---- 管理员后台（留空密码 = 禁用）----
ADMIN_USERNAME=admin
ADMIN_PASSWORD=

# ---- 验证码邮件（未配置时验证码打印到后端控制台，便于本地开发）----
SMTP_HOST=                          # 如 smtp.qq.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USER=
SMTP_PASSWORD=                      # SMTP 授权码，非登录密码
SMTP_FROM=
```

### 3. 启动

```bash
# ① 建表（首次；后续加列迁移幂等，可重复执行）
uv run python scripts/init_db.py

# ② 后端 API（端口 4399）
uv run python main.py

# ③ 前端静态站（端口 10001）
uv run python scripts/run_frontend.py
# 浏览器打开 http://127.0.0.1:10001/

# ④ 爬虫（可选：灌入初始技术文档语料）
uv run python scripts/run_spider.py            # 默认 2 个 worker，增量
uv run python scripts/run_spider.py -w 3 --reset   # 3 个 worker，全量重爬

# ⑤ 向量化流水线（也可登录后调 POST /api/embedding/run）
#    注意：Milvus Lite 模式下必须先停后端再跑离线脚本（单进程独占，见下文注意事项）
```

其他辅助脚本：

| 脚本 | 用途 |
|---|---|
| `scripts/seed_terms.py` | 从已入库文章提炼术语灌 `tech_term` 表（卡片数据源），`--enrich` 补详情 |
| `scripts/seed_ai_knowledge.py` | LLM 批量生成 AI/Python 高频知识讲解入库 |
| `scripts/probe_llm.py` | 探测哪套模型配置真正可用（文本 + 图片双通道） |
| `scripts/chat_test_client.py` | 对话接口联调客户端 |
| `scripts/mock_anthropic_server.py` | 无 Claude 密钥时验证 Anthropic 协议的 mock 服务 |
| `scripts/mock_bocha_server.py` | 无博查密钥时离线联调「联网补爬」全链路的 mock 搜索服务 |

---

## 🖥 前端页面

| 页面 | 功能 |
|---|---|
| 学习卡片 `index.html` | 术语卡片 + 简介，「换一个」手动刷新，一键跳对话页深问 |
| 对话学习 `chat.html` | 讲解/费曼双模式、SSE 流式、多会话管理、评分卡片、消息撤回、⚙️ 个人模型配置 |
| 知识库 `kb.html` | 全局分类聚合 + 正文弹窗 + 翻译；**个人知识库**：手工增删改、整站浅爬（AI 清洗）、AI 添加（对话式定题自动爬取）、爬取任务实时面板（进度条 + 运行中可取消） |
| 模拟面试 `interview.html` | JD + 简历双图上传 → 逐轮追问 → 总评卡片 → 面试记录列表 → 一键生成学习计划上板 |
| 任务板 `learning.html` | 学习目标发布 / 面试上板，任务树进度（补爬进度条：检索中流光条 / 爬取百分比 + 当前页）、材料阅读、取消（级联终止爬取链） |
| 管理后台 `admin.html` | 管理员专属：用户总览 |

---

## 🔌 API 概览

全部接口需 `Authorization: Bearer <JWT>`（仅 `/api/auth/*` 免鉴权）。

| 模块 | 路径 | 说明 |
|---|---|---|
| 鉴权 | `/api/auth/send-code` `/register` `/login` `/reset` `/me` | 验证码、注册、登录、忘记密码、当前用户 |
| 用户 | `GET/PUT /api/user/llm` | 个人模型配置（api_key 加密存储、脱敏返回） |
| 知识库 | `/api/knowledge/…` | 列表 / 分类 / 详情 / 上传；`/my/*` 个人库增删改、整站浅爬（提交 / 进度 / **取消**）、AI 添加 |
| 翻译 | `POST /api/translate/knowledge/{id}` | 知识条目翻译 |
| 向量化 | `POST /api/embedding/run` | 触发切块 + 向量化流水线 |
| 检索 | `POST /api/retrieval/search` | 混合检索调试 |
| 对话 | `POST /api/chat`（SSE）、`/history`、`/undo`、`/ping`、`/sessions` | 双模式对话、历史、撤回、模型连通性测试、会话管理（检索无资料时推 `kb_gap` 事件：已自动联网补爬 + 通用知识简答） |
| 卡片 | `GET /api/card/today`、`POST /api/card/refresh`、`GET /api/card/overview` | 当前卡片、换一张、学习总览 |
| JD | `POST /api/jd/analyze`、`GET /api/jd/{id}`、`GET /api/jd/` | JD 截图 → OCR → 技术栈 |
| 面试 | `POST /api/interview/start` `/answer`（SSE）、`GET /records`、`/records/{id}`、`/records/{id}/plan` | 双图面试、逐轮追问、记录、生成学习计划 |
| 评估 | `/api/evaluate/…` | 费曼评分记录 / 统计 / 详情 |
| 任务板 | `POST /api/board/goals` `/from-interview`、`GET /api/board/`、`/tasks/{id}`、`/tasks/{id}/cancel` | 发布目标、面试上板、任务树查询、取消 |

---

## 🖧 服务器部署

`deploy/` 目录提供 Ubuntu 22.04/24.04（2 核 2G 轻量机）一键部署方案（该目录含密钥与数据，已被 `.gitignore` 排除）：

- `setup.sh`：一键装环境（nginx / redis / mysql / uv + swap）→ 恢复数据 → 先向量化后启服务
- `rag-api.service`：systemd 守护；`nginx-rag.conf`：反向代理 4399 + 前端静态站
- `vectorize.py`：离线向量化脚本（内置后端存活检测，规避 Milvus Lite 双进程锁）

---

## ⚠️ 注意事项 / FAQ

1. **Milvus Lite 单进程独占**：配置了 `MILVUS_LITE_PATH` 时，同一 `.db` 文件同一时刻只能被一个进程打开。后端运行中不要另起进程跑离线向量化脚本；用进程内的 `POST /api/embedding/run` 无此问题。混合检索已做降级：向量路不可用自动退化为纯 BM25。
2. **勿用 `uvicorn --reload` 跑后端**：任务引擎（Producer Agent 池 + 超时回收器）随后端启动，多进程会重复消费任务。
3. **`AUTH_SECRET_KEY` 一经设定勿更换**：它同时是用户私有 api_key 的加密派生源，更换会导致存量密文解不出。
4. **本地 Redis 端口**：Windows 本机 6379 可能落入 Hyper-V 保留端口段无法监听，可改用其他端口（如 6500）；Linux 部署无此限制。
5. **未配置 SMTP 时**：注册/重置密码的验证码直接打印在后端控制台，方便本地开发。
6. **联网补爬需要个人模型**：检索词生成 / 候选过滤 / 页面清洗都用你 ⚙️ 里配置的**个人模型**（与个人爬取同口径），未配置时该功能静默跳过；`SEARCH_API_KEY` 留空 = 整体关闭。
7. **取消不回滚已入库数据**：取消爬取/检索任务会立即终止执行，但取消前已爬到的页面保留在知识库。
8. **密钥安全**：`.env` 与 `deploy/` 已被 `.gitignore` 忽略，切勿提交任何真实密钥。

---

## 📖 项目文档

- `CLAUDE.md`：完整开发文档（架构、目录约定、数据库设计、开发进度）
- `docs/RAG×多Agent融合升级方案.md`：多 Agent 融合设计方案与实施进度
- `AGENTS.md`：协作说明

---

## 🗺 路线图

- [ ] 搜索引擎可插拔：Bing / Tavily 等更多供应商（`SEARCH_PROVIDER` 已预留）
- [ ] 任务板补爬「静默放弃」原因可见化（写入子题并在前端展示）
- [ ] 宽泛主题降级为「拆到能爬为止」而非放弃
- [ ] Agent 活动可视化（气泡 / trace 泳道）、work_log 查看器
- [ ] 任务事件推送（Redis Pub/Sub + SSE 替代前端轮询）
