"""每日学习卡片接口：GET /api/card/today（阶段 7.5）

流程（对应 CLAUDE.md §7.5）：
    Redis key daily:card:YYYY-MM-DD
      命中 → 直接返回
      未命中 → 用日期做随机种子从 tech_term 抽一条（同一天结果稳定）
             → 写回 Redis，过期时间设到当天 24:00
"""
import json
import random
from datetime import date, datetime, timedelta

import redis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from core.config import settings
from DAO.tech_term_dao import TechTermDAO
from database.session import get_db
from model.TechTermModel import TechTermModel

router = APIRouter(prefix="/api/card", tags=["每日卡片"])


def _redis() -> redis.Redis:
    """Redis 客户端（与爬虫模块同一套连接配置）"""
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )


def pick_card(terms: list[TechTermModel], date_str: str) -> TechTermModel:
    """用日期做随机种子抽一条——同一天多次调用结果必然一致"""
    rng = random.Random(date_str)
    return rng.choice(terms)


def _seconds_until_midnight() -> int:
    """距离当天 24:00 的秒数（卡片缓存的 TTL）"""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(int((midnight - now).total_seconds()), 60)


def _to_card(term: TechTermModel, date_str: str) -> dict:
    """卡片响应结构：术语 + 别名 + 简介 + 详细讲解 + 示例 + 来源"""
    return {
        "date": date_str,
        "term": term.term,
        "alias": term.alias,
        "category": term.category,
        "brief": term.brief,
        "detail": term.detail,
        "example": term.example,
        "source_url": term.source_url,
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """系统概览统计：知识条目 / 术语卡片（全局）+ 讲解评估（当前用户）"""
    from sqlalchemy import func as sa_func

    from model.KnowledgeModel import KnowledgeModel
    from DAO.tech_term_dao import TechTermDAO
    from DAO.evaluate_dao import EvaluateDAO

    knowledge = (
        db.query(sa_func.count(KnowledgeModel.id))
        .filter(KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED)
        .scalar()
        or 0
    )
    eval_stats = EvaluateDAO(db).stats(user.id)
    return {
        "knowledge": knowledge,
        "terms": TechTermDAO(db).count(),
        "evals": eval_stats["total"],
        "eval_avg_score": eval_stats["avg_score"],
    }


@router.get("/today")
def today_card(db: Session = Depends(get_db)):
    """今日学习卡片：同一天多次刷新内容不变，换天自动换新"""
    today = date.today().isoformat()
    # v2：卡片新增详细讲解/示例字段后升版，避免命中旧结构缓存
    key = f"daily:card:v2:{today}"
    r = _redis()

    # 1) 缓存命中直接返回
    cached = r.get(key)
    if cached:
        return json.loads(cached)

    # 2) 未命中：日期种子抽卡
    dao = TechTermDAO(db)
    terms = dao.list_all()
    if not terms:
        raise HTTPException(
            status_code=404,
            detail="还没有术语数据，请先运行 scripts/seed_terms.py 灌种子",
        )

    card = _to_card(pick_card(terms, today), today)

    # 3) 写回 Redis，过期时间到当天 24:00
    r.set(key, json.dumps(card, ensure_ascii=False), ex=_seconds_until_midnight())
    return card
