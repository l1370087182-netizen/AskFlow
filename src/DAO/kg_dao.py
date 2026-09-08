"""kg_node / kg_edge 的 DAO：建图（节点+共现边+关系边）与查询（扩展/遍历）。

设计要点：
- learn_document：一篇质检合格知识的实体与关系写入图谱——节点归属内归一化判重
  （重复出现只加 mention_count）；同篇节点两两连【共现边】(cooccur，无向)，
  三元组连【关系边】(实际关系词，有向 主体→客体)，两类边 weight 各自累计。
  单篇实体数上限 MAX_NODES_PER_DOC、三元组上限 MAX_TRIPLES_PER_DOC，防撑花。
- expansion_terms：讲解模式消息命中术语卡时，查该术语节点的强【共现】邻居
  （按共现权重降序），作为 BM25 查询扩展词——「卡片里有的知识，图谱帮你把
  相关词一起带进检索」。
- traverse：多跳推理用——从起始实体沿【关系边】遍历 N 跳，返回可达路径
  （如 王五 -上司→ 李四 -上司→ 张三）。关系边稀疏时按任意关系边兜底。
- 全程尽力而为：调用方（reviewer / chain / multihop）各自 try 包裹，图谱故障
  不影响质检与对话。
"""
import logging

from sqlalchemy.orm import Session

from DAO.tech_term_dao import TechTermDAO
from model.KgModel import KgEdgeModel, KgNodeModel

logger = logging.getLogger(__name__)

MAX_NODES_PER_DOC = 8       # 单篇入库实体上限（提示词也要求 ≤8，这里代码兜底）
MAX_TRIPLES_PER_DOC = 6     # 单篇关系三元组上限（与提示词一致）
MAX_RELATION_LEN = 32       # 关系名落库长度上限（kg_edge.relation 是 String(32)）
MAX_EXPANSION = 3           # 查询扩展词上限
MIN_NAME_LEN = 2            # 实体名最短长度（单字符多是截断/噪声）
COOCCUR = "cooccur"         # 共现边的关系名（与有向关系边区分）


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
        self,
        uid: int,
        names: list[str],
        category: str = "general",
        triples: list[tuple[str, str, str]] | None = None,
    ) -> int:
        """一篇知识的实体与关系写入图谱：节点 + 共现边 + 有向关系边。

        - 节点：names（实体）∪ triples 的主体/客体，归属内归一化判重，
          已存在的 mention_count+1，缺失的新建。
        - 共现边（relation=cooccur，无向 src<dst）：同篇节点两两连边，weight 累计
          ——供 BM25 查询扩展（expansion_terms），行为与升级前一致。
        - 关系边（relation=实际关系词，有向 src=主体 → dst=客体）：来自 triples，
          weight 累计同关系出现次数——供多跳图遍历（traverse）。

        :return: 本次涉及的节点数（去重后）
        """
        # 1) 归一化去重实体名（names 优先，triple 端点补足），保序截断
        seen: set[str] = set()
        cleaned: list[str] = []

        def _add(raw: str) -> None:
            name = " ".join(str(raw).split()).strip()
            if len(name) < MIN_NAME_LEN or len(name) > 40:
                return
            key = self._normalize(name)
            if not key or key in seen:
                return
            seen.add(key)
            cleaned.append(name)

        for raw in names or []:
            if len(cleaned) >= MAX_NODES_PER_DOC:
                break
            _add(raw)

        # 三元组清洗（端点稍后补进节点集；自环/超长/空关系丢弃）
        triple_list: list[tuple[str, str, str]] = []
        for t in triples or []:
            if not isinstance(t, (list, tuple)) or len(t) != 3:
                continue
            s, r, o = (str(x).strip() for x in t)
            if len(s) < MIN_NAME_LEN or len(s) > 40:
                continue
            if len(o) < MIN_NAME_LEN or len(o) > 40:
                continue
            if not r or len(r) > MAX_RELATION_LEN:
                continue
            if self._normalize(s) == self._normalize(o):
                continue
            triple_list.append((s, r[:MAX_RELATION_LEN], o))
            if len(triple_list) >= MAX_TRIPLES_PER_DOC:
                break

        # 关系端点补进节点集（关系通常就在实体里，兜底补全；给端点留名额）
        node_cap = MAX_NODES_PER_DOC + MAX_TRIPLES_PER_DOC
        for s, _r, o in triple_list:
            if len(cleaned) >= node_cap:
                break
            _add(s)
            if len(cleaned) < node_cap:
                _add(o)

        if not cleaned:
            return 0

        # 2) 节点 upsert：全量取归属下已有节点做归一化判重
        #    （名字精确匹配会漏 'DI 容器' / 'DI容器' 这种跨写法重复），
        #    已存在的 mention_count+1，缺失的新建
        nodes: list[KgNodeModel] = []
        node_by_key: dict[str, KgNodeModel] = {}
        by_norm = {
            self._normalize(r.name): r
            for r in self.db.query(KgNodeModel).filter(KgNodeModel.user_id == uid).all()
        }
        for name in cleaned:
            key = self._normalize(name)
            row = by_norm.get(key)
            if row is not None:
                row.mention_count += 1
                if row.term_id is None:
                    row.term_id = self._match_term_id(uid, name)
                nodes.append(row)
                node_by_key[key] = row
            else:
                node = KgNodeModel(
                    user_id=uid,
                    name=name,
                    category=category or "general",
                    term_id=self._match_term_id(uid, name),
                )
                self.db.add(node)
                nodes.append(node)
                node_by_key[key] = node
        self.db.flush()

        # 3) 共现边（无向，src<dst）：同篇节点两两连边
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                src_id, dst_id = sorted((nodes[i].id, nodes[j].id))
                self._bump_edge(uid, src_id, dst_id, COOCCUR)

        # 4) 关系边（有向，src=主体 → dst=客体）：来自三元组
        for s, r, o in triple_list:
            s_node = node_by_key.get(self._normalize(s))
            o_node = node_by_key.get(self._normalize(o))
            if s_node is None or o_node is None or s_node.id == o_node.id:
                continue
            self._bump_edge(uid, s_node.id, o_node.id, r)

        self.db.commit()
        return len(nodes)

    def _bump_edge(self, uid: int, src_id: int, dst_id: int, relation: str) -> None:
        """建边或给已有边 weight+1（按 (uid, src, dst, relation) 唯一）。

        共现边与关系边共用此逻辑，靠 relation 字段区分；同一对节点可同时有
        一条 cooccur（无向）和若干条有向关系边，互不冲突。
        """
        edge = (
            self.db.query(KgEdgeModel)
            .filter(
                KgEdgeModel.user_id == uid,
                KgEdgeModel.src_id == src_id,
                KgEdgeModel.dst_id == dst_id,
                KgEdgeModel.relation == relation,
            )
            .first()
        )
        if edge is not None:
            edge.weight += 1
        else:
            self.db.add(
                KgEdgeModel(
                    user_id=uid, src_id=src_id, dst_id=dst_id, relation=relation
                )
            )

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

    # ---------- 多跳图遍历 ----------

    def traverse(
        self, uid: int, start_name: str, max_hops: int, relation: str | None = None
    ) -> list[dict]:
        """从起始实体沿【有向关系边】遍历，返回 max_hops 跳内的可达路径。

        多跳推理用：王五 -上司→ 李四 -上司→ 张三，问「王五的上司的上司」
        即 traverse(王五, max_hops=2) → 路径 ['王五','李四','张三']，终点是答案。

        - 只走关系边（relation != cooccur）；relation 给定则只走关系名互相包含的边
          （容忍「上司/上级」近义命名），否则走任意关系边（稀疏图下先保连通）。
        - 方向无关：从当前节点出发，边的 src/dst 任一端命中都走到另一端——关系
          主客体方向不保证与问法一致，精确方向交给检索证据与 LLM 判定。
        - 防环：路径内不重复访问节点；跳数兜底 1..5。
        - 锚点/邻居均取用户可见范围（全局 user_id=0 + 本人 uid）；异常返回空。

        :return: [{"path": [节点名...], "relations": [关系词...],
                   "depth": int, "weight": int}, ...]
                 按 depth 升序、同 depth 按累计 weight 降序；空表示图里走不通。
        """
        try:
            hops = max(1, min(int(max_hops), 5))
            target = self._normalize(start_name)
            start_nodes = [
                n
                for n in self.db.query(KgNodeModel)
                .filter(KgNodeModel.user_id.in_([0, uid]))
                .all()
                if self._normalize(n.name) == target
            ]
            if not start_nodes:
                return []

            def _rel_match(edge_rel: str) -> bool:
                if not relation:
                    return True
                a, b = self._normalize(edge_rel), self._normalize(relation)
                return bool(a) and bool(b) and (a in b or b in a)

            results: list[dict] = []
            # frontier 元素：(node_id, path_names, path_rels, visited_ids, accum_weight)
            frontier = [
                (n.id, [n.name], [], {n.id}, 0) for n in start_nodes
            ]
            for depth in range(1, hops + 1):
                next_frontier = []
                for nid, path, rels, visited, acc in frontier:
                    edges = (
                        self.db.query(KgEdgeModel)
                        .filter(
                            KgEdgeModel.user_id.in_([0, uid]),
                            KgEdgeModel.relation != COOCCUR,
                            (KgEdgeModel.src_id == nid) | (KgEdgeModel.dst_id == nid),
                        )
                        .all()
                    )
                    for e in edges:
                        if not _rel_match(e.relation):
                            continue
                        other_id = e.dst_id if e.src_id == nid else e.src_id
                        if other_id in visited:
                            continue
                        other = self.db.get(KgNodeModel, other_id)
                        if other is None or other.user_id not in (0, uid):
                            continue
                        new_path = path + [other.name]
                        new_acc = acc + (e.weight or 1)
                        results.append(
                            {
                                "path": new_path,
                                "relations": rels + [e.relation],
                                "depth": depth,
                                "weight": new_acc,
                            }
                        )
                        next_frontier.append(
                            (other_id, new_path, rels + [e.relation],
                             visited | {other_id}, new_acc)
                        )
                frontier = next_frontier
                if not frontier:
                    break

            # 去重（同路径可能经不同锚点/边序到达，留 weight 最大的）+ 排序
            uniq: dict[tuple, dict] = {}
            for r in results:
                key = tuple(r["path"])
                if key not in uniq or r["weight"] > uniq[key]["weight"]:
                    uniq[key] = r
            return sorted(uniq.values(), key=lambda r: (r["depth"], -r["weight"]))
        except Exception as e:  # noqa: BLE001 —— 遍历是增强，出错返回空
            logger.warning("[kg] 图遍历失败（返回空）：%s", e)
            return []
