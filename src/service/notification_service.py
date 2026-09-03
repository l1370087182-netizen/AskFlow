"""通知服务：事件 → 站内通知（尽力而为，绝不阻塞主流程）。

挂钩点：
- 任务引擎终态：agent_task_dao.write_back / fail_task 尾部调
  notify_task_terminal（纯 dict 快照，独立 Session 写入——避免通知写入
  异常污染 agent 会话，兑现「绝不影响任务状态流转」）
- 费曼评分：chat_controller._eval_stream save_evaluation 成功后
- 面试总评：interview_controller 记录 commit 成功后
"""
import logging

from database.session import SessionLocal

logger = logging.getLogger(__name__)

# kind → 中文名（失败通知标题用）
KIND_ZH = {
    "crawl": "爬取",
    "web_search": "联网检索",
    "quality_review": "质检",
    "term_curate": "术语整理",
    "study_plan": "学习计划",
    "learning_goal": "目标拆解",
    "learning_item": "学习材料",
}


# ---------- 写入 ----------

def _insert(user_id: int, type_: str, title: str, body: str, link: str, ref_id: str) -> None:
    """独立会话写一条通知（调用方已 try/except；这里兜底 close）"""
    from DAO.notification_dao import NotificationDAO

    db = SessionLocal()
    try:
        NotificationDAO(db).create(
            user_id=user_id, type=type_, title=title,
            body=body, link=link, ref_id=ref_id,
        )
    finally:
        db.close()


# ---------- 任务终态 ----------

def notify_task_terminal(snap: dict) -> None:
    """任务引擎终态 → 通知。入参是纯数据快照（不传 ORM 对象）。

    防御：仅 completed/failed 通知；user_id==0 系统任务跳过。
    """
    try:
        status = snap.get("status")
        if status not in ("completed", "failed"):
            return
        uid = int(snap.get("user_id") or 0)
        if uid == 0:
            return
        kind = snap.get("kind", "")
        payload = snap.get("payload") or {}
        output = snap.get("output") or {}
        task_id = str(snap.get("id", ""))

        if status == "failed":
            title, body, link = _failed_text(kind, snap.get("assignee", ""), output)
            _insert(uid, "task_failed", title, body, link, task_id)
            return

        built = _done_text(kind, task_id, snap.get("parent_id", ""), payload, output)
        if built is None:
            return
        title, body, link = built
        _insert(uid, "task_done", title, body, link, task_id)
    except Exception:  # noqa: BLE001 —— 通知失败只记日志，不影响状态流转
        logger.exception("[notify] 任务终态通知写入失败（已忽略）")


def _done_text(kind: str, task_id: str, parent_id: str, payload: dict, output: dict):
    """成功终态文案映射；返回 (title, body, link)；None = 不发"""
    if kind == "learning_goal":
        goal = str(payload.get("goal") or output.get("goal") or "").strip()[:40]
        items = output.get("items") or []
        return (
            "学习目标已拆解",
            f"「{goal or '未命名目标'}」已拆解为 {len(items)} 个学习子题，去任务板查看",
            f"learning.html?goal={task_id}",
        )
    if kind == "learning_item":
        topic = str(payload.get("topic", "")).strip()[:40]
        return (
            "学习材料就绪",
            f"「{topic}」的学习材料已生成，点击查看",
            f"learning.html?goal={parent_id}" if parent_id else "learning.html",
        )
    if kind == "crawl":
        done = int(output.get("done_pages") or 0)
        failed = int(output.get("failed_pages") or 0)
        skipped = int(output.get("skipped_pages") or 0)
        topic = str(payload.get("topic", "")).strip()[:40]
        prefix = f"「{topic}」" if topic else ""
        if failed > 0:
            return (
                "爬取部分完成",
                f"{prefix}成功 {done} 页 / 失败 {failed} 页 / 跳过 {skipped} 页，已入库的可在「我的知识」查看",
                "kb.html?tab=mine",
            )
        return (
            "爬取完成",
            f"{prefix}{done} 页已入知识库，去「我的知识」查看",
            "kb.html?tab=mine",
        )
    if kind == "web_search":
        selected = output.get("selected") or []
        source = str(payload.get("source", ""))
        link = "learning.html" if source == "board" else "kb.html?tab=mine"
        if selected:
            return (
                "联网检索完成",
                f"「{str(payload.get('topic', ''))[:40]}」选中 {len(selected)} 个页面，已转入爬取",
                link,
            )
        return (
            "联网检索完成",
            f"「{str(payload.get('topic', ''))[:40]}」筛选后没有值得入库的页面",
            link,
        )
    if kind == "quality_review":
        kept = int(output.get("kept") or 0)
        discarded = int(output.get("discarded") or 0)
        return (
            "知识质检完成",
            f"保留 {kept} 条 / 丢弃 {discarded} 条（低质量内容已清理）",
            "kb.html?tab=mine",
        )
    if kind == "term_curate":
        registered = int(output.get("registered") or 0)
        return (
            "术语整理完成",
            f"提炼并注册 {registered} 个术语，可能出现在学习卡片中",
            "index.html",
        )
    if kind == "study_plan":
        items = output.get("items") or []
        record_id = payload.get("interview_record_id", "")
        link = f"interview.html?record={record_id}" if record_id else "interview.html"
        if not items:
            return ("学习计划", "本次面试没有识别出明确弱项与缺口，无需补课计划", link)
        return ("学习计划已生成", f"共 {len(items)} 个补课条目，点击查看", link)
    return None


def _failed_text(kind: str, assignee: str, output: dict) -> tuple[str, str, str]:
    """失败终态文案：内部话术转人话 + 高频原因给行动指引"""
    error = str(output.get("error", "") or "")
    zh = KIND_ZH.get(kind, "任务")
    if assignee == "reaper-01" or "心跳超时" in error or "重试耗尽" in error:
        return (
            f"{zh}任务超时",
            "任务在规定时间内没有跑完，已被放弃。可以重新发起。",
            "learning.html",
        )
    if "未配置个人大模型" in error or "个人大模型" in error:
        return (
            "请先配置个人模型",
            f"{zh}任务需要用到你配置的模型：到「对话学习」页右上角 ⚙️ 保存模型后重试",
            "chat.html",
        )
    return (
        f"{zh}任务失败",
        (error[:200] if error else "执行过程中出现异常，可重新发起"),
        "learning.html",
    )


# ---------- 对话/面试事件 ----------

def notify_evaluation(user_id: int, session_id: str, topic: str, rounds: int, score) -> None:
    """费曼评分完成通知（score 可能为 None——解析失败也有通知，不拼分数）"""
    try:
        t = str(topic or "").strip()[:40]
        if score is not None:
            body = f"「{t}」讲解评分 {score}/10（追问 {rounds} 轮），点击查看详细点评"
        else:
            body = f"「{t}」讲解评分已生成（追问 {rounds} 轮），点击查看详细点评"
        _insert(
            user_id, "evaluation", "费曼讲解评分完成", body,
            f"chat.html?session={session_id}&mode=teach", str(session_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[notify] 费曼评分通知写入失败（已忽略）")


def notify_interview(
    user_id: int, record_id: int, jd_title: str, n_weakness: int, n_gap: int
) -> None:
    """面试总评完成通知"""
    try:
        title_part = str(jd_title or "").strip()[:40] or "模拟面试"
        _insert(
            user_id, "interview", "面试总评已生成",
            f"「{title_part}」总评完成：{n_weakness} 个薄弱点 / {n_gap} 个知识缺口，点击查看",
            f"interview.html?record={record_id}", str(record_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[notify] 面试总评通知写入失败（已忽略）")
