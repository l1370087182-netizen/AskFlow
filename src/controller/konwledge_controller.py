"""知识库接口：全局语料浏览 + 个人知识库（我的知识）。

路由顺序硬约束：/my* 系列必须声明在 /{knowledge_id} 之前，
否则 "my" 会被当成路径参数匹配走。

个人知识库越权约定（与规格 §八 对齐）：
- 读路（详情）：他人条目一律 404，隐藏存在性
- 写路（编辑/删除）：他人条目 403 明示，全局条目 403（不可改公共语料）
- 全局视图（列表/分类/统计/检索）只出 user_id==0 的行
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from DAO.knowledge_dao import KnowledgeDAO
from database.session import get_db
from core.config import settings
from milvus.ingestion.pipeline import IngestionPipeline
from milvus.ingestion.VectorStore import get_vector_store
from model.KnowledgeModel import KnowledgeModel
from model.UserModel import UserModel
from schema.knowledge import (
    KnowledgeItem,
    KnowledgeListResponse,
    KnowledgeCreateRequest,
    KnowledgeDetailResponse,
    KnowledgeMyCreateRequest,
    KnowledgeMyUpdateRequest,
    KnowledgeCrawlRequest,
    CrawlTaskOut,
    CrawlActiveOut,
    AIAddRequest,
    AIAddResponse,
)
from service import knowledge_service as kb_service

router = APIRouter(prefix="/api/knowledge", tags=["知识库"])

# 上传只允许这两种纯文本格式
ALLOWED_SUFFIXES = {".md", ".txt"}

# 手工添加正文最短长度（太短没有知识价值）
MIN_MANUAL_CONTENT_LEN = 50


@router.get("/health")
async def health():
    """健康检查"""
    return {"module": "knowledge", "status": "ok"}


@router.get("/categories")
def categories(
    scope: str = Query(default="global", description="global=全局 / mine=我的"),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """按分类聚合知识数量。默认全局视图（仅 user_id==0），
    scope=mine 时聚合本人个人条目——两种视图都不泄漏他人数据"""
    uid = user.id if scope == "mine" else 0
    rows = KnowledgeDAO(db).category_counts(user_id=uid)
    return {
        "total": sum(n for _, n in rows),
        "categories": [
            {"category": c, "count": n} for c, n in rows
        ],
    }


# ---------- 我的知识（个人知识库；务必声明在 /{knowledge_id} 之前） ----------

@router.get("/my", response_model=KnowledgeListResponse)
def my_list(
    category: str | None = None,
    status: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """我的个人知识列表（id 倒序），只出本人条目；total 为真实总数（分页用）"""
    dao = KnowledgeDAO(db)
    rows = dao.list_by_category(
        category=category,
        status=status,
        limit=limit,
        offset=offset,
        user_id=user.id,
    )
    return KnowledgeListResponse(
        total=dao.count_by_category(category=category, status=status, user_id=user.id),
        items=[KnowledgeItem.model_validate(r) for r in rows],
    )


@router.post("/my", response_model=KnowledgeItem)
def my_create(
    body: KnowledgeMyCreateRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """手工添加个人知识：校验 → 落库（manual:// 占位键）→ 同步向量化。

    返回最终 status：1=已向量化；2=已保存但向量化失败（前端据此提示）。
    """
    content = body.content.strip()
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(content) < MIN_MANUAL_CONTENT_LEN:
        raise HTTPException(
            status_code=400, detail=f"正文至少 {MIN_MANUAL_CONTENT_LEN} 字，请补充内容"
        )
    row = kb_service.add_manual(
        db,
        user.id,
        title=title,
        content=content,
        category=body.category.strip() or "general",
    )
    return KnowledgeItem.model_validate(row)


@router.post("/my/crawl", status_code=202)
def my_crawl_submit(
    body: KnowledgeCrawlRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """提交整站浅爬任务（后台线程池并行消费，202 + task_id，进度另查）。

    400：未配置个人模型（提示去 ⚙️）/ URL 非法 / SSRF 拦截
    409：活跃任务达上限（detail.message 提示，等待前面的任务完成）
    """
    try:
        task_id = kb_service.submit_crawl(
            db,
            uid=user.id,
            url=body.url,
            category=body.category.strip() or "general",
            max_pages=body.max_pages,
        )
    except kb_service.CrawlSubmitError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"message": e.message, "task_id": e.task_id},
        ) from e
    return {"task_id": task_id, "status": "pending"}


@router.get("/my/crawl/active", response_model=CrawlActiveOut)
def my_crawl_active(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """当前全部进行中的爬取/联网检索任务（前端进入「我的知识」时恢复进度面板）。

    务必声明在 /my/crawl/{task_id} 之前，否则 "active" 被当成 task_id。
    并行化后单用户可多个任务；无活跃任务（含悬挂超时/已终态）返回空列表。
    """
    states = kb_service.get_active_crawl_tasks(db, user.id)
    return CrawlActiveOut(tasks=[CrawlTaskOut(**s) for s in states])


@router.get("/my/crawl/{task_id}", response_model=CrawlTaskOut)
def my_crawl_progress(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """爬取任务进度（DB 生命周期为准，Redis 提供实时明细）。
    不存在/非本人/已过期统一 404"""
    state = kb_service.get_crawl_task(db, user.id, task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return CrawlTaskOut(**state)


@router.post("/my/crawl/{task_id}/cancel")
def my_crawl_cancel(
    task_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """取消本人的爬取/联网检索任务（含执行中——执行端每页心跳探针会尽快停下）。

    WEB_SEARCH 顺带级联取消其子 crawl；已终态返回 canceled=false。
    取消不回滚已入库数据（取消前爬到的页面保留）。
    """
    from DAO.agent_task_dao import AgentTaskDAO
    from model.AgentTaskModel import TaskKind

    dao = AgentTaskDAO(db)
    task = dao.get(task_id)
    if not task or task.user_id != user.id or task.kind not in (
        TaskKind.CRAWL, TaskKind.WEB_SEARCH
    ):
        raise HTTPException(status_code=404, detail="未找到该任务")

    canceled = dao.cancel_task(task_id) is not None
    if canceled and task.kind == TaskKind.WEB_SEARCH:
        child = dao.find_child(task_id, TaskKind.CRAWL)
        if child is not None:
            dao.cancel_task(child.id)  # 尽力级联；子任务已终态则无操作
    return {"canceled": canceled, "task_id": task_id}


@router.post("/my/ai-add", response_model=AIAddResponse)
def my_ai_add(
    body: AIAddRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """AI 添加：对话式定题 → 自动提交爬取。

    每轮带全量对话历史（服务端不存状态）。模型判断：
    主题宽泛/疑似拼错 → action=ask 追问或确认纠错；
    主题明确 → 选官方文档根地址，探活后直接提交爬取，action=crawl 带 task_id。
    """
    try:
        return AIAddResponse(
            **kb_service.ai_add_chat(
                db, user.id, [m.model_dump() for m in body.messages]
            )
        )
    except kb_service.CrawlSubmitError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"message": e.message, "task_id": e.task_id},
        ) from e


@router.put("/my/{knowledge_id}", response_model=KnowledgeItem)
def my_update(
    knowledge_id: int,
    body: KnowledgeMyUpdateRequest,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """编辑我的个人知识：任一字段变化 → status=0 → 重新向量化入库"""
    row = KnowledgeDAO(db).get_by_db(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到该知识")
    if row.user_id == KnowledgeModel.GLOBAL_USER_ID:
        raise HTTPException(status_code=403, detail="全局知识不可编辑")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权编辑他人知识")

    if body.title is None and body.content is None and body.category is None:
        raise HTTPException(status_code=400, detail="至少提供一项要修改的内容")

    new_title = body.title.strip() if body.title is not None else None
    new_content = body.content.strip() if body.content is not None else None
    new_category = body.category.strip() if body.category is not None else None
    if (new_title is not None and not new_title) or (
        new_content is not None and not new_content
    ):
        raise HTTPException(status_code=400, detail="标题/正文不能为空")

    dao = KnowledgeDAO(db)
    row = dao.update_content(
        knowledge_id,
        title=new_title,
        content=new_content,
        # 分类传空串视为不改（空分类会破坏聚合视图）
        category=new_category or None,
    )
    if row.status == KnowledgeModel.STATUS_PENDING:
        # 内容有变 → 重建向量（清旧块→写新块在流水线进程锁内）
        try:
            IngestionPipeline(db).ingest_row(row)
        except Exception:  # noqa: BLE001 —— 条目已更新，向量化失败由 status=2 呈现
            pass
        db.refresh(row)
    return KnowledgeItem.model_validate(row)


@router.delete("/my/{knowledge_id}")
def my_delete(
    knowledge_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """删除我的个人知识：先删 Milvus 块，再删 DB 行。

    Milvus 不可用时记日志继续（vectors_removed=-1），
    宁可留孤儿块也不让用户删不掉条目（孤儿块可由 /api/embedding/run 全量重建清掉）。
    """
    dao = KnowledgeDAO(db)
    row = dao.get_by_db(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到该知识")
    if row.user_id == KnowledgeModel.GLOBAL_USER_ID:
        raise HTTPException(status_code=403, detail="全局知识不可删除")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权删除他人知识")

    try:
        vectors_removed = get_vector_store().delete_by_knowledge(knowledge_id)
    except Exception as e:  # noqa: BLE001 —— 向量库不可用不阻塞删除
        logging.getLogger(__name__).warning(
            "[kb-personal] 删除向量失败（继续删 DB）：id=%s %s", knowledge_id, e
        )
        vectors_removed = -1

    dao.delete(knowledge_id)
    return {"deleted": True, "knowledge_id": knowledge_id, "vectors_removed": vectors_removed}


# ---------- 全局知识库（仅 user_id==0；个人条目不可见） ----------

@router.get("/{knowledge_id}", response_model=KnowledgeDetailResponse)
def knowledge_detail(
    knowledge_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
):
    """单条知识详情（含正文，知识库页弹窗展示）。

    读路隐藏存在性：他人个人条目与不存在一律 404。
    """
    row = KnowledgeDAO(db).get_by_db(knowledge_id)
    if not row or row.user_id not in (KnowledgeModel.GLOBAL_USER_ID, user.id):
        raise HTTPException(status_code=404, detail="未找到该知识")
    return KnowledgeDetailResponse.model_validate(row)


@router.get("/", response_model=KnowledgeListResponse)
async def list_knowledge(
    category: str | None = None,
    status: int | None = None,
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """分页查询全局知识列表（仅 user_id==0，安全底线）；total 为真实总数（分页用）"""
    dao = KnowledgeDAO(db)

    rows = dao.list_by_category(
        category=category,
        status=status,
        limit=limit,
        offset=offset,
        user_id=KnowledgeModel.GLOBAL_USER_ID,
    )
    item = [KnowledgeItem.model_validate(r) for r in rows]
    return KnowledgeListResponse(
        total=dao.count_by_category(
            category=category, status=status, user_id=KnowledgeModel.GLOBAL_USER_ID
        ),
        items=item,
    )


@router.post("/", response_model=KnowledgeItem)
async def create_knowledge(
    body: KnowledgeCreateRequest,
    db: Session = Depends(get_db),
):
    """手动创建知识（全局语料，user_id=0，语义不变）"""
    dao = KnowledgeDAO(db)
    row = dao.create(
        title=body.title,
        content=body.content,
        source_url=body.source_url,
        category=body.category,
        source_type=body.source_type,
    )
    if row is None:
        raise HTTPException(status_code=409, detail="知识已存在")
    return KnowledgeItem.model_validate(row)


@router.post("/upload", response_model=KnowledgeItem)
async def upload_knowledge(
    file: UploadFile = File(..., description="要上传的 md/txt 文档"),
    title: str | None = Form(default=None, description="标题，不传则用文件名"),
    category: str = Form(default="general", description="技术分类"),
    db: Session = Depends(get_db),
):
    """文档上传入库：读取文本 → 校验 → upsert 进 knowledge 表（全局，source_type=upload）"""

    # 1) 少数客户端可能不带文件名，先兜底
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    # 2) 扩展名校验：只收 md/txt
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 .md / .txt 文件")

    # 3) 读取并解码；学习阶段文档都不大，直接全量读取，编码统一要求 UTF-8
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件必须是 UTF-8 编码")

    # 4) 与爬虫同一规则：正文太短视为无效，不入库
    content = content.strip()
    if len(content) < 200:
        raise HTTPException(status_code=400, detail="正文少于 200 字，视为无效")

    # 5) 标题缺省用文件名（去掉扩展名）
    final_title = title.strip() if title else Path(filename).stem

    # 6) 构造伪 URL 作为唯一键：source_url 是 NOT NULL + 复合唯一，
    #    多份上传都填空串会撞唯一约束；用文件名做键，同名重传正好走 upsert 更新分支
    source_url = f"upload://{filename}"

    # 7) 原文件落盘到 DATA_DIR/uploads，便于追溯；同名直接覆盖（同名=同一份文档）
    upload_dir = Path(settings.DATA_DIR) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / filename).write_bytes(raw)

    # 8) 复用 DAO 的 upsert：新文档插入；同名文档更新且 status 重置为待向量化
    dao = KnowledgeDAO(db)
    row = dao.upsert(
        title=final_title,
        content=content,
        source_url=source_url,
        category=category,
        source_type="upload",
    )
    return KnowledgeItem.model_validate(row)
