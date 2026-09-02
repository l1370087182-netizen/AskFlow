"""学习卡片接口：GET /api/card/today + POST /api/card/refresh（手动刷新）

卡片不再按天自动更换（原「每日卡片」）：Redis 只存「当前卡片」
（key 无 TTL），重进主页看到的还是上一次那张；用户点「换一个」
（POST /refresh）才排除当前这张随机换新。抽取范围 = 全局术语 +
本人个人术语（个人术语只进本人卡片）。
"""
import json
import random

import redis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.deps import get_current_user
from DAO.tech_term_dao import TechTermDAO
from database.session import get_db
from model.TechTermModel import TechTermModel
from util.redis_util import make_redis

router = APIRouter(prefix="/api/card", tags=["学习卡片"])


def _redis() -> redis.Redis:
    """Redis 客户端（统一工厂：短超时+失败即抛，见 util/redis_util）"""
    return make_redis()


def _card_key(uid: int) -> str:
    """当前卡片缓存键（无 TTL：卡片常驻，换卡只由用户手动触发）"""
    return f"card:current:v1:{uid}"


def _to_card(term: TechTermModel) -> dict:
    """卡片响应结构：术语 + 别名 + 简介 + 详细讲解 + 示例 + 来源"""
    return {
        "term": term.term,
        "alias": term.alias,
        "category": term.category,
        "brief": term.brief,
        "detail": term.detail,
        "example": term.example,
        "source_url": term.source_url,
    }


def _pick_term(db: Session, uid: int, exclude: str | None = None) -> TechTermModel:
    """从用户可见术语里随机抽一条；exclude 排除当前这张（只剩一张时原样返回）"""
    terms = TechTermDAO(db).list_visible(uid)
    if not terms:
        raise HTTPException(
            status_code=404,
            detail="还没有术语数据，请先运行 scripts/seed_terms.py 灌种子",
        )
    pool = [t for t in terms if t.term != exclude]
    return random.choice(pool or terms)


def _load_cached(r: redis.Redis, uid: int) -> dict | None:
    """读当前卡片缓存；缓存内容损坏时视为未命中（换一张覆盖掉）"""
    cached = r.get(_card_key(uid))
    if not cached:
        return None
    try:
        return json.loads(cached)
    except json.JSONDecodeError:
        return None


@router.get("/overview")
def overview(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """系统概览统计：知识条目 / 术语卡片（全局）+ 讲解评估（当前用户）

    个人知识库：全局计数只算 user_id=0，另返回本人已向量化个人条目数。
    """
    from sqlalchemy import func as sa_func

    from model.KnowledgeModel import KnowledgeModel
    from DAO.tech_term_dao import TechTermDAO
    from DAO.evaluate_dao import EvaluateDAO

    knowledge = (
        db.query(sa_func.count(KnowledgeModel.id))
        .filter(
            KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED,
            KnowledgeModel.user_id == 0,
        )
        .scalar()
        or 0
    )
    my_knowledge = (
        db.query(sa_func.count(KnowledgeModel.id))
        .filter(
            KnowledgeModel.status == KnowledgeModel.STATUS_EMBEDDED,
            KnowledgeModel.user_id == user.id,
        )
        .scalar()
        or 0
    )
    eval_stats = EvaluateDAO(db).stats(user.id)
    term_dao = TechTermDAO(db)
    return {
        "knowledge": knowledge,
        "my_knowledge": my_knowledge,
        "terms": term_dao.count(user_id=0),
        "my_terms": term_dao.count(user_id=user.id),
        "evals": eval_stats["total"],
        "eval_avg_score": eval_stats["avg_score"],
    }


@router.get("/today")
def today_card(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """当前学习卡片：手动刷新制——重进页面不换卡，点「换一个」才换。

    抽取范围 = 全局术语 + 本人个人术语（个人术语由知识爬取的
    curator Agent 提炼，仅本人可见）。
    """
    r = _redis()
    cached = _load_cached(r, user.id)
    if cached:
        return cached

    card = _to_card(_pick_term(db, user.id))
    r.set(_card_key(user.id), json.dumps(card, ensure_ascii=False))
    return card


@router.post("/refresh")
def refresh_card(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """换一张卡片：排除当前这张随机换新（只剩一张时原样返回）"""
    r = _redis()
    current = _load_cached(r, user.id)
    exclude = current.get("term") if current else None

    card = _to_card(_pick_term(db, user.id, exclude))
    r.set(_card_key(user.id), json.dumps(card, ensure_ascii=False))
    return card
