"""多跳检索编排（A+B）：图谱导航 + 逐跳检索取证。

针对「答案要串联多个实体关系」的问题（如「王五的上司的上司是谁」）：单跳检索
只能捞到第一跳（王五→李四），后续跳（李四→张三）因查询里没有对应词面而漏召。
本模块把两者结合——图谱给路径（精确制导），检索给原文（可解释证据）：

  1. LLM 拆解问题 → 起始实体 + 跳数 + 关系词 + 每跳子查询
  2. 图谱遍历(B)：KgDAO.traverse 沿有向关系边走 N 跳，拿到答案路径
  3. 逐跳检索(A)：按路径/子查询检索每一跳的原文佐证
  4. 拼装「推理路径 + 各跳证据」上下文，交回 chain 给 LLM 生成

全程可降级，绝不把对话搞挂：
  - 启发式不像多跳 / LLM 判非多跳 → 返回 None（调用方退回单跳检索）
  - 图谱空 / 遍历不通 → 退回纯子查询检索（A 单独也能跑）
  - 一条证据都检不到 → 返回 None（退回单跳）
"""
from __future__ import annotations

import json
import logging
import re

from milvus.retrieval.hybird import chunk_key, relevant_hits

logger = logging.getLogger(__name__)

# 累计证据块上限 / 每跳检索取几块（多跳问题少见，控制延迟与上下文长度）
MAX_EVIDENCE = 8
PER_HOP_TOPK = 3
# 跳数兜底范围（防 LLM 给出离谱跳数把检索拖垮）
HOPS_MIN, HOPS_MAX = 1, 4

# 关系名词触发表 + 英文嵌套所属结构，用于「像不像多跳」的廉价启发式门控
# （只在命中时才花一次 LLM 拆解调用，避免每条消息都拆、拖慢首字延迟）
_RELATIONAL_NOUNS = (
    # 中文关系名词
    "上司", "老板", "上级", "领导", "下属", "父亲", "母亲", "爸爸", "妈妈",
    "儿子", "女儿", "老师", "师傅", "徒弟", "朋友", "同事", "同学", "作者",
    "创始人", "创建者", "发明者", "首都", "省会", "母公司", "子公司", "部门",
    "经理", "总监", "妻子", "丈夫", "哥哥", "姐姐",
    # 英文关系名词（英文多跳靠这些 + 嵌套结构触发）
    "boss", "manager", "father", "mother", "author", "founder", "ceo",
    "capital", "owner", "leader", "wife", "husband", "creator", "parent",
)


def looks_multihop(msg: str) -> bool:
    """廉价启发式：问题是否像「多跳关系推理」。

    触发条件（命中任一）：
      ① 含关系名词（上司/父亲/作者/boss/founder…）——多跳的强信号；
      ② 英文嵌套所属 "of…of" 或双 's（英文关系词不全在表里，靠结构兜底）。

    中文【不】用泛化的「的…的」触发：那会误伤「FastAPI 的依赖注入的用法」这类
    普通技术问题，白白多一次 LLM 拆解调用、拖慢首字延迟。原则：宁可漏判（漏的
    退回单跳，不出错），不要误判（误判加延迟）。关系词表可随时扩充。
    """
    if not msg:
        return False
    m = msg.lower()
    relational = any(w in m for w in _RELATIONAL_NOUNS)
    nested_en = bool(re.search(r"\bof\b[^.?!]{0,40}\bof\b", m)) or m.count("'s") >= 2
    return relational or nested_en


DECOMPOSE_PROMPT = """你是检索规划器。判断下面的问题是否需要【多跳推理】——
答案需要串联多个实体之间的关系（例如「王五的上司的上司是谁」要走 王五→李四→张三 两跳）。

若需要多跳，给出：
- start_entity: 推理起点实体（问题中已明确出现的那个实体名）
- hops: 需要几跳（正整数，通常 2-3）
- relation: 每跳的关系词（如「上司」；不确定就填空字符串）
- sub_queries: 每一跳用来检索原文佐证的查询，条数=hops（如 ["王五的上司是谁", "李四的上司是谁"]）

若不需要多跳（普通问题，一次检索即可回答），只输出 {{"multi_hop": false}}。

只输出 JSON：{{"multi_hop": true, "start_entity": "...", "hops": 2, "relation": "...", "sub_queries": ["...", "..."]}}

问题：{question}"""


def _parse_plan(raw: str) -> dict | None:
    """宽容解析拆解结果；非多跳/解析失败返回 None"""
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    cands = [t]
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        cands.append(t[s : e + 1])
    for c in cands:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or not data.get("multi_hop"):
            return None
        start = str(data.get("start_entity", "")).strip()
        try:
            hops = max(HOPS_MIN, min(int(data.get("hops", 2)), HOPS_MAX))
        except (TypeError, ValueError):
            hops = 2
        relation = str(data.get("relation", "")).strip()
        sq = data.get("sub_queries") or []
        sub_queries = [str(x).strip() for x in sq if str(x).strip()][:hops] if isinstance(sq, list) else []
        if not start and not sub_queries:
            return None
        return {"start_entity": start, "hops": hops, "relation": relation, "sub_queries": sub_queries}
    return None


class MultiHopRetriever:
    """多跳检索编排器：拆解 → 图谱遍历 → 逐跳检索 → 拼装证据上下文"""

    def __init__(self, db, retriever, llm):
        self.db = db
        self.retriever = retriever
        self.llm = llm

    def build(self, question: str, uid: int = 0, top_k: int = 5) -> dict | None:
        """返回 {"context", "results", "path", "answer"}；非多跳/无证据返回 None"""
        if not looks_multihop(question):
            return None  # 启发式门控：不像多跳，省一次 LLM 调用

        # 1) LLM 拆解
        try:
            raw = self.llm.chat(
                [{"role": "user", "content": DECOMPOSE_PROMPT.format(question=question)}],
                0.1,
            )
        except Exception as e:  # noqa: BLE001 —— 拆解失败退回单跳
            logger.warning("[multihop] 拆解调用失败，退回单跳：%s", e)
            return None
        plan = _parse_plan(raw)
        if plan is None:
            return None  # LLM 判定非多跳

        # 2) 图谱遍历(B)：拿答案路径（图谱空/不通则为空，下一步用子查询兜底）
        path = self._traverse(plan, uid)

        # 3) 逐跳检索取证(A)
        evidence = self._gather_evidence(question, plan, path, uid)
        if not evidence:
            return None  # 一条证据都没有 → 退回单跳，让单跳自己判「有无资料」

        # 4) 拼装上下文
        context = self._format_context(plan, path, evidence)
        return {
            "context": context,
            "results": evidence,
            "path": path,
            "answer": path[-1] if len(path) >= 2 else None,
        }

    def _traverse(self, plan: dict, uid: int) -> list[str]:
        """图谱遍历拿路径；任何异常/空图返回 []（纯 A 兜底）"""
        if not plan["start_entity"]:
            return []
        try:
            from DAO.kg_dao import KgDAO  # 延迟导入，与 chain 同口径

            paths = KgDAO(self.db).traverse(
                uid, plan["start_entity"], plan["hops"], plan["relation"] or None
            )
            if not paths:
                return []
            # 优先取恰好 hops 跳的路径；没有则取能走到的最深路径
            deep = [p for p in paths if p["depth"] == plan["hops"]] or paths
            return deep[0]["path"]
        except Exception as e:  # noqa: BLE001 —— 图谱是增强，故障退回子查询检索
            logger.warning("[multihop] 图遍历失败（退回子查询检索）：%s", e)
            return []

    def _gather_evidence(self, question: str, plan: dict, path: list[str], uid: int) -> list[dict]:
        """按「图谱路径 + LLM 子查询 + 原问题」逐条检索，去重累计证据块"""
        queries: list[str] = []
        rel = plan["relation"] or ""
        # 图谱路径 → 每相邻两实体 + 关系词拼成精准查询
        if len(path) >= 2:
            for i in range(len(path) - 1):
                queries.append(f"{path[i]} {path[i + 1]} {rel}".strip())
        # LLM 子查询（图谱空时的主力，图谱有时作补充）
        for q in plan["sub_queries"]:
            if q not in queries:
                queries.append(q)
        # 原问题兜底
        if question not in queries:
            queries.append(question)
        queries = queries[: plan["hops"] + 2]

        evidence: list[dict] = []
        seen: set[tuple] = set()
        for q in queries:
            try:
                hits = relevant_hits(self.retriever.search(q, top_k=PER_HOP_TOPK, uid=uid))
            except Exception as e:  # noqa: BLE001 —— 单条检索失败不影响其他跳
                logger.warning("[multihop] 逐跳检索失败 q=%r：%s", q, e)
                continue
            for h in hits:
                k = chunk_key(h)
                if k in seen:
                    continue
                seen.add(k)
                evidence.append(h)
            if len(evidence) >= MAX_EVIDENCE:
                break
        return evidence[:MAX_EVIDENCE]

    @staticmethod
    def _format_context(plan: dict, path: list[str], evidence: list[dict]) -> str:
        """拼装「推理路径（若有）+ 各跳原文佐证」上下文块"""
        head: list[str] = []
        if len(path) >= 2:
            rel = plan["relation"] or "关联"
            head.append(f"【多跳推理路径】{' →(' + rel + ') '.join(path)}")
            head.append("（路径由知识图谱给出，仅供参考；请以下方原文佐证为准，逐跳核对后回答）")
        else:
            head.append("【多跳检索】下面是分步检索到的原文佐证，请串联它们回答：")
        parts = ["\n".join(head)]
        for i, r in enumerate(evidence, 1):
            parts.append(
                f"[片段 {i}]（分类：{r.get('category', '')}，相关度：{r.get('score', 0):.2f}）\n"
                f"{r['content']}"
            )
        return "\n\n".join(parts)
