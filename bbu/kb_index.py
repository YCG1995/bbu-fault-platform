"""故障知识库检索索引：紧凑倒排 + BM25F + 实体/短语/SimHash 辅助索引。

索引完全驻留内存，从 SQLite 的 active 案例重建；不依赖 SQLite FTS5、
numpy、jieba 或向量库。每次重建约 1–3 秒（2268 案例规模），服务层负责
防抖与原子替换。
"""
from __future__ import annotations

import math
import re
import time
from array import array
from dataclasses import dataclass, field
from typing import Any, Iterable

from .kb_text import make_terms, normalize_key, simhash, hamming

FIELD_NAMES = ("title", "symptom", "cause", "solution", "body")
FIELD_WEIGHTS = {"title": 4.0, "symptom": 2.2, "cause": 1.8, "solution": 1.2, "body": 1.0}
BM25_K1 = 1.2
BM25_B = 0.75
MAX_TERM_TF = 65535


@dataclass
class IndexCase:
    id: str
    doc_id: str
    title: str
    title_clean: str
    symptom: str = ""
    cause: str = ""
    solution: str = ""
    body: str = ""
    quality: float = 0.0
    boards: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    root_cause_key: str = ""
    status: str = "active"
    created_at: str = ""
    found_date: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)

    def fields(self) -> dict[str, str]:
        return {
            "title": self.title_clean or self.title,
            "symptom": self.symptom,
            "cause": self.cause,
            "solution": self.solution,
            "body": self.body or self.symptom,
        }


@dataclass
class SearchHit:
    case: IndexCase
    score: float
    lexical: float = 0.0
    entity: float = 0.0
    phrase: float = 0.0
    fuzzy: float = 0.0
    channels: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        case = self.case
        return {
            "case_id": case.id, "doc_id": case.doc_id,
            "title": case.title, "title_clean": case.title_clean,
            "board": case.boards[0] if case.boards else "",
            "boards": case.boards, "domains": case.domains, "versions": case.versions,
            "symptom": case.symptom, "cause": case.cause, "solution": case.solution,
            "sections": case.sections,
            "environment": next((s.get("text") for s in case.sections
                                 if s.get("kind") == "environment"), ""),
            "prevention": next((s.get("text") for s in case.sections
                                if s.get("kind") == "prevention"), ""),
            "quality": case.quality, "score": round(self.score, 4),
            "channels": self.channels, "reasons": self.reasons,
            "uri": f"faultkb://case/{case.id}",
            "link": f"/#/kb/docs/{case.doc_id}",
        }


class KBIndex:
    """不可变快照式索引；构建完成后只读，重建时整对象替换。"""

    def __init__(self, cases: Iterable[IndexCase]) -> None:
        started = time.monotonic()
        self.cases: list[IndexCase] = list(cases)
        self.terms: dict[str, int] = {}
        self.postings: list[array] = []
        self.tfs: list[array] = []
        self.field_len: list[array] = [array("I") for _ in FIELD_NAMES]
        self.idf: list[float] = []
        self.entity_index: dict[tuple[str, str], array] = {}
        self.phrase_index: dict[str, array] = {}
        self.simhashes: array = array("Q")
        self.sim_buckets: dict[tuple[int, int], array] = {}
        self._build()
        self.build_ms = int((time.monotonic() - started) * 1000)

    def _term_id(self, term: str) -> int:
        existing = self.terms.get(term)
        if existing is not None:
            return existing
        term_id = len(self.terms)
        self.terms[term] = term_id
        self.postings.append(array("I"))
        self.tfs.append(array("H"))
        return term_id

    def _build(self) -> None:
        counts_by_field: list[list[int]] = []
        for doc_index, case in enumerate(self.cases):
            fields = case.fields()
            counts: list[int] = []
            # 先收齐本文档在五个字段的词频，保证同一词项每文档只写一条 posting。
            doc_term_tf: dict[str, list[int]] = {}
            for field_pos, name in enumerate(FIELD_NAMES):
                tokens = make_terms(fields.get(name, ""))
                self.field_len[field_pos].append(len(tokens))
                counts.append(len(tokens))
                term_counts: dict[str, int] = {}
                for term in tokens:
                    term_counts[term] = term_counts.get(term, 0) + 1
                for term, tf in term_counts.items():
                    slot = doc_term_tf.setdefault(term, [0, 0, 0, 0, 0])
                    slot[field_pos] = min(MAX_TERM_TF, slot[field_pos] + tf)
            for term, tf_vec in doc_term_tf.items():
                term_id = self._term_id(term)
                self.postings[term_id].append(doc_index)
                self.tfs[term_id].extend(tf_vec)
            counts_by_field.append(counts)
        total = len(self.cases)
        self.avg_len = [
            (sum(row[pos] for row in counts_by_field) / total) if total else 1.0
            for pos in range(len(FIELD_NAMES))
        ]
        for pos, avg in enumerate(self.avg_len):
            if avg <= 0:
                self.avg_len[pos] = 1.0
        self.idf = [
            math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            if postings else 0.0
            for postings in self.postings
        ]
        self._build_aux_indexes()

    def _rebuild_term_arrays(self) -> None:
        # 防御性校验：确保每个 term 的 tfs 长度是 5 的整数倍。
        for term_id, postings in enumerate(self.postings):
            if len(self.tfs[term_id]) != len(postings) * 5:
                # 正常情况下不会发生；保留截断而不是静默错位。
                self.tfs[term_id] = self.tfs[term_id][:len(postings) * 5]

    def _build_aux_indexes(self) -> None:
        for doc_index, case in enumerate(self.cases):
            for kind, values in (("board", case.boards), ("domain", case.domains),
                                 ("version", case.versions)):
                for value in values:
                    key = (kind, normalize_key(value))
                    self.entity_index.setdefault(key, array("I")).append(doc_index)
            for key in self._phrase_keys(case):
                self.phrase_index.setdefault(key, array("I")).append(doc_index)
            sh = simhash(case.title_clean or case.title)
            self.simhashes.append(sh)
            for part in range(4):
                bucket = (sh >> (part * 16)) & 0xFFFF
                self.sim_buckets.setdefault((part, bucket), array("I")).append(doc_index)

    @staticmethod
    def _phrase_keys(case: IndexCase) -> list[str]:
        keys: list[str] = []
        title = normalize_key(case.title_clean or case.title)
        if len(title) >= 4:
            keys.append(title)
        # 原因/方案首句用于整句复制查询；限制长度，避免超长键。
        for value in (case.cause, case.solution):
            for sentence in _sentences(value):
                norm = normalize_key(sentence)
                if 6 <= len(norm) <= 240:
                    keys.append(norm)
        return keys

    # ---------------------------------------------------------------- 查询
    def bm25f(self, query_terms: list[str], field_boost: dict[str, float] | None = None,
              allowed: set[int] | None = None) -> dict[int, float]:
        if not query_terms or not self.cases:
            return {}
        boost = field_boost or {}
        scores: dict[int, float] = {}
        for term in query_terms:
            term_id = self.terms.get(term)
            if term_id is None:
                continue
            postings = self.postings[term_id]
            tfs = self.tfs[term_id]
            idf = self.idf[term_id]
            for pos, doc_index in enumerate(postings):
                if allowed is not None and doc_index not in allowed:
                    continue
                total = 0.0
                base = pos * 5
                for field_pos, field_name in enumerate(FIELD_NAMES):
                    tf = tfs[base + field_pos]
                    if not tf:
                        continue
                    length = self.field_len[field_pos][doc_index]
                    avg = self.avg_len[field_pos] or 1.0
                    weight = FIELD_WEIGHTS[field_name] * float(boost.get(field_name, 1.0))
                    norm = BM25_K1 * (1 - BM25_B + BM25_B * length / avg)
                    total += weight * tf * (BM25_K1 + 1) / (tf + norm)
                scores[doc_index] = scores.get(doc_index, 0.0) + idf * total
        return scores

    def entity_hits(self, entities: dict[str, list[str]]) -> dict[int, float]:
        scores: dict[int, float] = {}
        for kind, values in entities.items():
            for value in values:
                for doc_index in self.entity_index.get((kind, normalize_key(value)), ()):
                    scores[doc_index] = scores.get(doc_index, 0.0) + 1.0
        return scores

    def phrase_hits(self, query: str, limit_chars: int = 4) -> dict[int, float]:
        q = normalize_key(query)
        if len(q) < limit_chars:
            return {}
        hits: dict[int, float] = {}
        # 整句/标题前缀精确命中。
        for key, docs in self.phrase_index.items():
            if q == key or (len(q) >= 8 and (q in key or key in q)):
                for doc_index in docs:
                    hits[doc_index] = hits.get(doc_index, 0.0) + 2.0
        # 长查询清理后可能被标签/标点打断，回退到标题/原因首句包含。
        for doc_index, case in enumerate(self.cases):
            if doc_index in hits:
                continue
            hay = normalize_key(" ".join((case.title_clean, case.cause[:500], case.symptom[:300])))
            if q in hay:
                hits[doc_index] = 1.5
        return hits

    def fuzzy_hits(self, query: str) -> dict[int, float]:
        if not self.cases:
            return {}
        sh = simhash(query)
        candidates: set[int] = set()
        for part in range(4):
            bucket = (sh >> (part * 16)) & 0xFFFF
            candidates.update(self.sim_buckets.get((part, bucket), ()))
        hits: dict[int, float] = {}
        for doc_index in candidates:
            distance = hamming(sh, self.simhashes[doc_index])
            if distance <= 3:
                hits[doc_index] = 1.0 - distance / 4.0
        return hits

    def public_stats(self) -> dict[str, Any]:
        return {
            "cases": len(self.cases), "terms": len(self.terms),
            "postings": sum(len(x) for x in self.postings),
            "entities": len(self.entity_index), "phrases": len(self.phrase_index),
            "build_ms": self.build_ms, "bytes": self.approx_bytes(),
        }

    def approx_bytes(self) -> int:
        postings = sum(len(x) for x in self.postings) * 4
        tfs = sum(len(x) for x in self.tfs) * 2
        field_len = sum(len(x) for x in self.field_len) * 4
        text = sum(len(c.title) + len(c.symptom) + len(c.cause) + len(c.solution) + len(c.body)
                   for c in self.cases)
        return postings + tfs + field_len + text * 2


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?；;\n]", text or "")
            if item.strip()]
