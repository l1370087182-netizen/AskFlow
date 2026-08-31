"""切块器：把一篇文档切成若干带重叠的文本块。

策略（对应 CLAUDE.md §7.2）：
- 优先在段落边界（空行）切分，尽量保住语义完整性
- chunk_size ≈ 500 字，overlap ≈ 50 字
- 单个段落超过 chunk_size 时，退化为按字符硬切（步长 = chunk_size - overlap）
- 每块记录所属文档（knowledge_id、标题、source_url、category），供后续溯源
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from model.KnowledgeModel import KnowledgeModel

# 默认块参数：500 字一块，相邻块重叠 50 字
DEFAULT_CHUNK_SIZE = 500
DEFAULT_OVERLAP = 50


@dataclass
class Chunk:
    """切块后的一个文本块，携带所属文档信息"""

    knowledge_id: int
    title: str
    source_url: str
    category: str
    index: int  # 块在文档内的序号（0 起）
    text: str


def split_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """把纯文本切成块列表：段落优先，超长硬切，块间带重叠。

    重叠的作用：上下文跨越切分边界时，相邻两块都保留边界附近的内容，
    避免检索时刚好丢掉关键半句。
    """
    if overlap >= chunk_size:
        raise ValueError("overlap 必须小于 chunk_size")

    # 1) 按空行切段落，去掉空白段落
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    buf = ""
    fresh = False  # buf 里是否有重叠尾之外的新内容（防止把纯重叠残留冲成重复块）

    def flush() -> None:
        """把当前缓冲区推入结果，只保留尾部 overlap 字作为下一块开头"""
        nonlocal buf, fresh
        if fresh and buf.strip():
            chunks.append(buf.strip())
        # 上一块的尾部 overlap 字作为下一块的开头，没有历史块则清空
        buf = buf[-overlap:] if overlap > 0 and chunks else ""
        fresh = False

    for para in paragraphs:
        # 2) 单段超长：先冲出当前缓冲区，再对该段按字符硬切
        if len(para) > chunk_size:
            flush()
            step = chunk_size - overlap
            for i in range(0, len(para), step):
                piece = para[i : i + chunk_size].strip()
                if piece:
                    chunks.append(piece)
                if i + chunk_size >= len(para):
                    break
            # 用最后一块的尾部重建重叠（此时 buf 只有残留，不算新内容）
            buf = chunks[-1][-overlap:] if chunks and overlap > 0 else ""
            fresh = False
            continue

        # 3) 常规段落：拼上后超限就先冲块；注意拼的时候带上换行，保持段落结构
        candidate = para if not buf else buf + "\n" + para
        if len(candidate) > chunk_size and buf:
            flush()
            # flush 后 buf 是上一块的重叠尾部，与当前段重新拼接
            candidate = para if not buf else buf + "\n" + para
        buf = candidate
        fresh = True

    # 4) 收尾：仅当缓冲区确实有新内容时才冲出最后一块
    if fresh:
        flush()
    return chunks


def split_knowledge(
    row: KnowledgeModel,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """把一条 knowledge 记录切成 Chunk 列表（每块携带文档元信息）"""
    texts = split_text(row.content, chunk_size=chunk_size, overlap=overlap)
    return [
        Chunk(
            knowledge_id=row.id,
            title=row.title,
            source_url=row.source_url,
            category=row.category,
            index=i,
            text=t,
        )
        for i, t in enumerate(texts)
    ]
