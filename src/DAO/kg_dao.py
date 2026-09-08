"""kg_node / kg_edge 的 DAO：建图（节点+共现边）与邻居查询（检索查询扩展）。

设计要点：
- learn_document：一篇质检合格知识的实体写入图谱——节点归属内归一化判重
  （重复出现只加 mention_count），同篇实体两两连边 weight+1。
  单篇实体数上限 MAX_NODES_PER_DOC，防止模型抽一堆杂词把图撑花。
- expansion_terms：讲解模式消息命中术语卡时，查该术语节点在图谱里的
  强邻居（按共现权重降序），作为 BM25 查询扩展词——「卡片里有的知识，
  图谱帮你把相关词一起带进检索」。
- 全程尽力而为：调用方（reviewer / chain）各自 try 包裹，图谱故障不影响
  质检与对话。
"""
import logging

from sqlalchemy.orm import Session

from DAO.tech_term_dao import TechTermDAO
from model.KgModel import KgEdgeModel, KgNodeModel

logger = logging.getLogger(__name__)

MAX_NODES_PER_DOC = 8   # 单篇入库实体上限（提示词也要求 ≤8，这里代码兜底）
MAX_EXPANSION = 3       # 查询扩展词上限
MIN_NAME_LEN = 2        # 实体名最短长度（单字符多是截断/噪声）


class KgDAO:
    """知识图谱 DAO"""

    def __init__(self, db: Session):
        self.db = db

    # ---------- 建图 ----------

    @staticmethod
    def _normalize(name: str) -> str:
        """实体名归一化：与 TechTermDAO._normalize 同口径（忽略大小写/空格/连字符）"""
        return TechTermDAO._normalize(name)

    def _match_term_id(self, uid: int, name: str) -> int | None:
        """实体名归一化比对用户可见术语（全局+本人），命中挂 term_id"""
        target = self._normalize(name)
        for t in TechTermDAO(self.db).list_visible(uid):
            if self._normalize(t.term) == target:
                return t.id
        return None

    def learn_document(
        self, uid: int, names: list[str], category: str = "general"
    ) -> int:
        """一篇知识的实体写入图谱：建/更新节点 + 同篇两两连边。

        :return: 本次涉及的节点数（去重后）
        """
        # 1) 归一化去重、长度过滤，保序截断
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in names or []:
            name = " ".join(str(raw).split()).strip()
            if len(name) < MIN_NAME_LEN or len(name) > 40:
                continue
            key = self._normalize(name)
            if not key or key in seen:
                continue
            seen.add(key)
            cleaned.append(name)
            if len(cleaned) >= MAX_NODES_PER_DOC:
                break
        if not cleaned:
            return 0

        # 2) 节点 upsert：全量取归属下已有节点做归一化判重
        #    （名字精确匹配会漏 'DI 容器' / 'DI容器' 这种跨写法重复），
        #    已存在的 mention_count+1，缺失的新建
        nodes: list[KgNodeModel] = []
        by_norm = {
            self._normalize(r.name): r
            for r in self.db.query(KgNodeModel).filter(KgNodeModel.user_id == uid).all()
        }
        for name in cleaned:
            row = by_norm.get(self._normalize(name))
            if row is not None:
                row.mention_count += 1
                if row.term_id is None:
                    row.term_id = self._match_term_id(uid, name)
                nodes.append(row)
            else:
                node = KgNodeModel(
                    user_id=uid,
                    name=name,
                    category=category or "general",
                    term_id=self._match_term_id(uid, name),
                )
                self.db.add(node)
                nodes.append(node)
        self.db.flush()

        # 3) 同篇共现边：两两连边（src<dst 存一份），已有 weight+1
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                src_id, dst_id = sorted((nodes[i].id, nodes[j].id))
                edge = (
                    self.db.query(KgEdgeModel)
                    .filter(
                        KgEdgeModel.user_id == uid,
                        KgEdgeModel.src_id == src_id,
                        KgEdgeModel.dst_id == dst_id,
                        KgEdgeModel.relation == "cooccur",
                    )
                    .first()
                )
                if edge is not None:
                    edge.weight += 1
                else:
                    self.db.add(
                        KgEdgeModel(user_id=uid, src_id=src_id, dst_id=dst_id)
                    )
        self.db.commit()
        return len(nodes)

    # ---------- 查询扩展 ----------

    def expansion_terms(self, uid: int, term) -> list[str]:
        """术语（tech_term 行）→ 图谱强邻居实体名，按共现权重降序。

        锚点取全部同名/同 term_id 节点（全局 + 本人个人图谱都算），
        邻居只取用户可见范围（全局+本人）。异常/查不到返回空。
        """
        try:
            anchors = (
                self.db.query(KgNodeModel)
                .filter(
                    KgNodeModel.user_id.in_([0, uid]),
                    KgNodeModel.term_id == term.id,
                )
                .all()
            )
            if not anchors:
                # term_id 没挂上（术语晚于实体入库等）：退化按名字匹配
                target = self._normalize(term.term)
                anchors = [
                    n for n in self.db.query(KgNodeModel)
                    .filter(KgNodeModel.user_id.in_([0, uid]))
                    .all()
                    if self._normalize(n.name) == target
                ]
            if not anchors:
                return []
            anchor_ids = [n.id for n in anchors]
            anchor_keys = {self._normalize(n.name) for n in anchors}

            edges = (
                self.db.query(KgEdgeModel)
                .filter(
                    KgEdgeModel.user_id.in_([0, uid]),
                    (KgEdgeModel.src_id.in_(anchor_ids))
                    | (KgEdgeModel.dst_id.in_(anchor_ids)),
                )
                .order_by(KgEdgeModel.weight.desc())
                .limit(MAX_EXPANSION * 5)
                .all()
            )
            if not edges:
                return []

            # 邻居候选按最弱权重排（一条边挂多个锚点时取最大权重），去锚点自身
            best_weight: dict[int, int] = {}
            for e in edges:
                for nid in (e.src_id, e.dst_id):
                    if nid in anchor_ids:
                        continue
                    best_weight[nid] = max(best_weight.get(nid, 0), e.weight)
            if not best_weight:
                return []
            neighbors = (
                self.db.query(KgNodeModel)
                .filter(
                    KgNodeModel.user_id.in_([0, uid]),
                    KgNodeModel.id.in_(best_weight),
                )
                .all()
            )
            ranked = sorted(
                neighbors,
                key=lambda n: (best_weight.get(n.id, 0), n.mention_count),
                reverse=True,
            )
            out: list[str] = []
            for n in ranked:
                # 锚点自己、以及与锚点同名的邻居没信息量
                if self._normalize(n.name) in anchor_keys:
                    continue
                out.append(n.name)
                if len(out) >= MAX_EXPANSION:
                    break
            return out
        except Exception as e:  # noqa: BLE001 —— 扩展是增强，出错返回空
            logger.warning("[kg] 查询扩展失败（返回空）：%s", e)
            return []
