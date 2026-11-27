"""故障知识库多路召回、RRF 融合与确定性重排。

一路词法（BM25F）负责自然语言，实体/短语/SimHash 负责现场叫法和整句复制；
服务端不调用模型，保证无模型时仍可检索和拒答。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .kb_index import KBIndex, SearchHit
from .kb_text import (
    detect_boards, detect_domains, detect_slots, detect_versions, expand_terms,
    freshness_score, make_terms, normalize_key,
)

RRF_K = 60
CHANNEL_WEIGHTS = {"lexical": 1.0, "entity": 1.1, "phrase": 0.9, "fuzzy": 0.6}
FIELD_BOOST = {
    "cause": {"cause": 1.4},
    "solution": {"solution": 1.4},
    "symptom": {"symptom": 1.3},
    "version": {"title": 1.1},
}
INTENTS = {
    "cause": re.compile(r"原因|为什么|根因|定位|为何|怎么回事|什么导致"),
    "solution": re.compile(r"怎么处理|怎么解决|如何解决|如何恢复|恢复|处理方案|处理步骤|解决办法|修复"),
    "symptom": re.compile(r"现象|表现|告警|症状|出现什么|异常"),
    "version": re.compile(r"哪个版本|版本引入|版本.*(?:问题|导致|升级)|升级.*版本"),
}


@dataclass
class QueryAnalysis:
    query: str
    normalized: str
    terms: list[str] = field(default_factory=list)
    expansions: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)
    intent: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    field_boost: dict[str, float] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "query": self.query, "normalized": self.normalized,
            "terms": self.terms[:24], "expansions": self.expansions,
            "boards": self.boards, "domains": self.domains,
            "versions": self.versions, "slots": self.slots,
            "intent": self.intent, "filters": self.filters,
            "field_boost": self.field_boost,
        }


def analyze_query(query: str, filters: dict[str, Any] | None = None) -> QueryAnalysis:
    raw = str(query or "").strip()
    normalized = normalize_key(raw)
    expansions = expand_terms(raw)
    terms = make_terms(raw)
    # 同义词只做有限扩展；实体原样保留，避免把型号扩成别的系列。
    for expansion in expansions:
        for term in make_terms(expansion):
            if term not in terms:
                terms.append(term)
    terms = sorted(terms, key=lambda item: (-len(item), item))[:48]
    boards = detect_boards(raw)
    domains = detect_domains(raw)
    versions = detect_versions(raw)
    slots = detect_slots(raw)
    intent = ""
    for name, pattern in INTENTS.items():
        if pattern.search(normalized):
            intent = name
            break
    # 多意图时以第一个命中的意图为主，但保留所有意图的字段权重。
    boost: dict[str, float] = {}
    for name, pattern in INTENTS.items():
        if pattern.search(normalized):
            for key, value in FIELD_BOOST[name].items():
                boost[key] = max(boost.get(key, 1.0), value)
    clean_filters = {}
    for key in ("board", "domain"):
        value = str((filters or {}).get(key) or "").strip()
        if value:
            clean_filters[key] = value
    return QueryAnalysis(
        query=raw, normalized=normalized, terms=terms, expansions=expansions,
        boards=boards, domains=domains, versions=versions, slots=slots,
        intent=intent, filters=clean_filters, field_boost=boost,
    )


def _allowed(index: KBIndex, query: QueryAnalysis) -> set[int] | None:
    if not query.filters:
        return None
    board = normalize_key(query.filters.get("board", ""))
    domain = normalize_key(query.filters.get("domain", ""))
    allowed: set[int] = set()
    for doc_index, case in enumerate(index.cases):
        if board and not any(normalize_key(item) == board for item in case.boards):
            continue
        if domain and not any(normalize_key(item) == domain for item in case.domains):
            continue
        allowed.add(doc_index)
    return allowed


def _ranked(scores: dict[int, float], limit: int) -> list[tuple[int, float]]:
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def _rrf(channels: dict[str, list[tuple[int, float]]]) -> dict[int, float]:
    fused: dict[int, float] = {}
    for name, ranked in channels.items():
        weight = CHANNEL_WEIGHTS.get(name, 1.0)
        for rank, (doc_index, _score) in enumerate(ranked, start=1):
            fused[doc_index] = fused.get(doc_index, 0.0) + weight / (RRF_K + rank)
    return fused


def _title_coverage(case, terms: list[str]) -> float:
    if not terms:
        return 0.0
    title = set(make_terms(case.title_clean or case.title))
    return len(title & set(terms)) / min(len(terms), 12)


def _entity_coverage(case, query: QueryAnalysis) -> float:
    values = [("board", item) for item in query.boards] + [
        ("domain", item) for item in query.domains] + [
        ("version", item) for item in query.versions]
    if not values:
        return 0.0
    matched = 0
    for kind, value in values:
        pool = case.boards if kind == "board" else (
            case.domains if kind == "domain" else case.versions)
        if any(normalize_key(item) == normalize_key(value) for item in pool):
            matched += 1
    return matched / len(values)


def _section_hit(case, intent: str) -> float:
    if intent == "cause" and case.cause:
        return 1.0
    if intent == "solution" and case.solution:
        return 1.0
    if intent == "symptom" and case.symptom:
        return 1.0
    if intent == "version" and case.versions:
        return 1.0
    return 0.0


def _hit_reasons(case, query: QueryAnalysis, channel_names: list[str]) -> list[str]:
    reasons: list[str] = []
    for board in query.boards:
        if any(normalize_key(item) == normalize_key(board) for item in case.boards):
            reasons.append(f"命中板卡 {board}")
    for domain in query.domains:
        if any(normalize_key(item) == normalize_key(domain) for item in case.domains):
            reasons.append(f"命中领域 {domain}")
    for version in query.versions:
        if any(normalize_key(item) == normalize_key(version) for item in case.versions):
            reasons.append(f"命中版本 {version}")
    if "phrase" in channel_names:
        reasons.append("命中标题/原文整句")
    if "lexical" in channel_names:
        reasons.append("命中现象/原因/方案关键词")
    if "fuzzy" in channel_names:
        reasons.append("近似标题匹配")
    return list(dict.fromkeys(reasons))[:5]


def _dedupe(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    selected: list[SearchHit] = []
    doc_counts: dict[str, int] = {}
    root_counts: dict[str, int] = {}
    for hit in hits:
        if len(selected) >= limit:
            break
        doc_id = hit.case.doc_id
        if doc_counts.get(doc_id, 0) >= 2:
            continue
        root = hit.case.root_cause_key
        # 前 5 条中同一根因只保留一条；后位放宽，保留可解释的相关案例。
        if root and len(selected) < min(5, limit) and root_counts.get(root, 0) >= 1:
            continue
        selected.append(hit)
        doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
        if root:
            root_counts[root] = root_counts.get(root, 0) + 1
    return selected


def search(index: KBIndex, query: str, *, filters: dict[str, Any] | None = None,
           limit: int = 10, threshold: float = 0.18) -> dict[str, Any]:
    started = time.monotonic()
    analysis = analyze_query(query, filters)
    allowed = _allowed(index, analysis)
    limit = max(1, min(12, int(limit or 10)))
    if not analysis.normalized:
        return {"items": [], "no_answer": True, "analysis": analysis.public(),
                "latency_ms": 0, "channels": {}, "total_cases": len(index.cases),
                "reason": "空查询"}

    lexical_scores = index.bm25f(analysis.terms, analysis.field_boost, allowed)
    entity_scores = index.entity_hits({
        "board": analysis.boards, "domain": analysis.domains,
        "version": analysis.versions,
    })
    if allowed is not None:
        entity_scores = {k: v for k, v in entity_scores.items() if k in allowed}
    phrase_scores = index.phrase_hits(analysis.normalized)
    if allowed is not None:
        phrase_scores = {k: v for k, v in phrase_scores.items() if k in allowed}
    fuzzy_scores = index.fuzzy_hits(analysis.normalized)
    if allowed is not None:
        fuzzy_scores = {k: v for k, v in fuzzy_scores.items() if k in allowed}

    channels = {
        "lexical": _ranked(lexical_scores, 50),
        "entity": _ranked(entity_scores, 50),
        "phrase": _ranked(phrase_scores, 20),
        "fuzzy": _ranked(fuzzy_scores, 10),
    }
    fused = _rrf(channels)
    if not fused:
        return {"items": [], "no_answer": True, "analysis": analysis.public(),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "channels": {k: len(v) for k, v in channels.items()},
                "total_cases": len(index.cases), "reason": "没有词项或实体命中"}

    max_lexical = max(lexical_scores.values()) if lexical_scores else 0.0
    top_candidates = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:20]
    hits: list[SearchHit] = []
    for doc_index, fused_score in top_candidates:
        case = index.cases[doc_index]
        lexical = lexical_scores.get(doc_index, 0.0)
        entity = entity_scores.get(doc_index, 0.0)
        phrase = phrase_scores.get(doc_index, 0.0)
        fuzzy = fuzzy_scores.get(doc_index, 0.0)
        lexical_norm = min(1.0, lexical / max(max_lexical, 1.0))
        title_cov = _title_coverage(case, analysis.terms)
        entity_cov = _entity_coverage(case, analysis)
        section = _section_hit(case, analysis.intent)
        quality = max(0.0, min(1.0, case.quality / 100.0))
        fresh = freshness_score(case.found_date or case.created_at)
        complete = 1.0 if (case.cause and case.solution) else 0.0
        final = (
            0.50 * lexical_norm + 0.20 * entity_cov + 0.10 * title_cov
            + 0.10 * section + 0.05 * quality + 0.03 * fresh + 0.02 * complete
        )
        # 融合排名不是噪声：实体/短语命中要把 RRF 信号带进最终分数。
        final += min(0.12, fused_score * 2.0)
        channel_names = [name for name, ranked in channels.items()
                         if any(doc_index == item[0] for item in ranked)]
        hits.append(SearchHit(
            case=case, score=final, lexical=lexical, entity=entity,
            phrase=phrase, fuzzy=fuzzy, channels=channel_names,
            reasons=_hit_reasons(case, analysis, channel_names)))
    hits.sort(key=lambda item: (-item.score, item.case.id))
    selected = _dedupe(hits, limit)

    # 槽位保证：词法/实体/短语各自第 1 名若存在，不能被融合挤掉。
    for name in ("lexical", "entity", "phrase"):
        ranked = channels.get(name) or []
        if not ranked:
            continue
        top_index = ranked[0][0]
        top_hit = next((x for x in hits if x.case is index.cases[top_index]), None)
        if top_hit is None:
            case = index.cases[top_index]
            top_hit = SearchHit(case=case, score=0.01,
                                reasons=[f"{name} 首选"])
        if any(x.case.id == top_hit.case.id for x in selected):
            continue
        selected.insert(0, top_hit)
        selected = _dedupe(selected, limit)

    top_score = selected[0].score if selected else 0.0
    no_answer = (
        not selected
        or (top_score < threshold and not analysis.boards and not analysis.domains
            and not analysis.versions and not channels["phrase"])
    )
    return {
        "items": [hit.public() for hit in selected],
        "no_answer": bool(no_answer),
        "analysis": analysis.public(),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "channels": {k: len(v) for k, v in channels.items()},
        "total_cases": len(index.cases),
        "top_score": round(top_score, 4),
        "reason": "没有找到足够相关的历史案例" if no_answer else "",
    }
