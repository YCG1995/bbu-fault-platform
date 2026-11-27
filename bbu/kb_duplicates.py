"""近重复文档发现与人工合并向导。

纯标准库实现：字符 3-gram + Jaccard 精确相似度；候选初筛复用索引已有的
64 位 SimHash 4 段桶，避免 O(n²) 全量比较。合并只做“并入”，不物理删除：
源文档改为 merged 并保留 merged_into/merge_token，案例逐条追加修订后迁移。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .kb_revision import case_snapshot, write_revision
from .kb_service import KBError
from .kb_text import hamming, normalize_key, simhash
from .util import iso_now, new_id

MAX_DOCUMENTS = 20000
MAX_CANDIDATES = 200
DEFAULT_THRESHOLD = 0.82


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _fingerprint(doc: dict[str, Any], cases: list[dict[str, Any]]) -> str:
    parts = [str(doc.get("title_clean") or doc.get("title") or "")]
    for case in cases[:4]:
        parts.append(str(case.get("symptom") or ""))
        parts.append(str(case.get("cause") or case.get("cause_detail") or "")[:500])
    return "\n".join(parts)


def _shingles(text: str) -> set[str]:
    clean = re.sub(r"[\s\W_]+", "", normalize_key(text), flags=re.UNICODE)
    if len(clean) < 3:
        return set()
    return {clean[i:i + 3] for i in range(max(0, len(clean) - 2))}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _canonical_score(doc: dict[str, Any]) -> tuple:
    cases = int(doc.get("case_count") or 0)
    return (float(doc.get("quality") or 0), int(doc.get("citation_count") or 0),
            cases, str(doc.get("updated_at") or ""))


class KBDuplicates:
    def __init__(self, kb) -> None:  # noqa: ANN001
        self.kb = kb
        self.db = kb.db

    def _admin(self, user: dict[str, Any]) -> None:
        if (user or {}).get("role") != "admin":
            raise KBError("仅管理员可查看或执行近重复合并", 403, "forbidden")

    def _documents(self, kb_id: int) -> list[dict[str, Any]]:
        docs = self.db.all(
            "SELECT id,kb_id,title,title_clean,status,quality,boards_json,domains_json,"
            "versions_json,updated_at,merged_into FROM kb_documents "
            "WHERE kb_id=? AND status='active' AND deleted_at IS NULL "
            "ORDER BY created_at ASC LIMIT ?", (int(kb_id), MAX_DOCUMENTS))
        cases_by_doc: dict[str, list[dict[str, Any]]] = {}
        for case in self.db.all(
                "SELECT doc_id,id,title,symptom,cause,cause_detail,solution,status,"
                "citation_count FROM kb_cases WHERE kb_id=? AND status='active' "
                "ORDER BY doc_id,ordinal", (int(kb_id),)):
            cases_by_doc.setdefault(str(case.get("doc_id")), []).append(dict(case))
        result = []
        for doc in docs:
            doc_cases = cases_by_doc.get(str(doc.get("id")), [])
            if not doc_cases:
                continue
            fingerprint = _fingerprint(doc, doc_cases)
            sh = _shingles(fingerprint)
            doc["case_count"] = len(doc_cases)
            doc["citation_count"] = sum(int(c.get("citation_count") or 0) for c in doc_cases)
            doc["fingerprint"] = fingerprint
            doc["shingles"] = sh
            doc["simhash"] = simhash(fingerprint)
            result.append(doc)
        return result

    def candidates(self, user: dict[str, Any], *, kb_id: int = 1,
                   threshold: float = DEFAULT_THRESHOLD,
                   limit: int = 60) -> dict[str, Any]:
        self._admin(user)
        threshold = max(0.5, min(1.0, float(threshold or DEFAULT_THRESHOLD)))
        docs = self._documents(kb_id)
        buckets: dict[tuple[int, int], list[int]] = {}
        title_buckets: dict[str, list[int]] = {}
        pair_hits: dict[tuple[int, int], int] = {}
        for index, doc in enumerate(docs):
            sh = int(doc["simhash"])
            for part in range(4):
                key = (part, (sh >> (part * 16)) & 0xFFFF)
                bucket = buckets.setdefault(key, [])
                for other in bucket:
                    pair = (min(index, other), max(index, other))
                    pair_hits[pair] = pair_hits.get(pair, 0) + 1
                bucket.append(index)
            # 标题完全相同的文档没有 SimHash 桶命中时也纳入候选。
            title_key = normalize_key(doc.get("title_clean") or doc.get("title"))
            if title_key:
                for other in title_buckets.get(title_key, []):
                    pair = (other, index)
                    pair_hits[pair] = pair_hits.get(pair, 0) + 1
                title_buckets.setdefault(title_key, []).append(index)
        items = []
        for (left, right), hits in pair_hits.items():
            a, b = docs[left], docs[right]
            # 初筛：至少一个 16bit 段相同，或标题相同；再做精确 Jaccard。
            similar = _jaccard(a["shingles"], b["shingles"])
            if similar < threshold:
                continue
            if not similar:
                continue
            # 同一根因、同板卡但不同原因的负样本：Jaccard 低于阈值自然排除。
            preferred, other = (a, b) if _canonical_score(a) >= _canonical_score(b) else (b, a)
            items.append({
                "left_doc_id": a["id"], "right_doc_id": b["id"],
                "similarity": round(similar, 4),
                "hamming": hamming(int(a["simhash"]), int(b["simhash"])),
                "suggested_source": other["id"], "suggested_target": preferred["id"],
                "source": self._doc_public(other),
                "target": self._doc_public(preferred),
                "fields": self._diff(other, preferred),
                "case_count_delta": int(preferred["case_count"]) - int(other["case_count"]),
            })
        items.sort(key=lambda x: (-x["similarity"], x["left_doc_id"], x["right_doc_id"]))
        return {"items": items[:max(1, min(MAX_CANDIDATES, int(limit or 60)))],
                "total": len(items), "threshold": threshold,
                "documents": len(docs), "kb_id": int(kb_id)}

    def preview(self, source_doc_id: str, target_doc_id: str,
                user: dict[str, Any], *, kb_id: int = 1) -> dict[str, Any]:
        self._admin(user)
        source = self._one(source_doc_id, kb_id)
        target = self._one(target_doc_id, kb_id)
        if source_doc_id == target_doc_id:
            raise KBError("源文档和目标文档不能相同", 400, "same_document")
        source_cases = self._cases(source_doc_id)
        target_cases = self._cases(target_doc_id)
        source_set = _shingles(_fingerprint(source, source_cases))
        target_set = _shingles(_fingerprint(target, target_cases))
        similarity = _jaccard(source_set, target_set)
        return {
            "source": self._doc_public(source), "target": self._doc_public(target),
            "similarity": round(similarity, 4),
            "fields": self._diff(source, target),
            "source_cases": len(source_cases), "target_cases": len(target_cases),
            "suggested_canonical": self._suggest(source, target),
        }

    def merge(self, source_doc_id: str, target_doc_id: str, user: dict[str, Any], *,
              merge_token: str, reason: str = "", kb_id: int = 1) -> dict[str, Any]:
        self._admin(user)
        merge_token = str(merge_token or "").strip()
        if not merge_token or len(merge_token) > 80:
            raise KBError("merge_token 必填且不超过 80 字", 400, "bad_merge_token")
        source = self.db.one(
            "SELECT * FROM kb_documents WHERE id=? AND kb_id=? AND deleted_at IS NULL",
            (source_doc_id, int(kb_id)))
        if not source:
            raise KBError("文档不存在", 404, "not_found")
        if source_doc_id == target_doc_id:
            raise KBError("源文档和目标文档不能相同", 400, "same_document")
        if (source.get("merged_into") == target_doc_id
                and source.get("merge_token") == merge_token):
            return {"ok": True, "noop": True, "source_doc_id": source_doc_id,
                    "target_doc_id": target_doc_id, "moved_cases": 0}
        source = self._one(source_doc_id, kb_id)
        target = self._one(target_doc_id, kb_id)
        # 目标必须仍是活跃 canonical，不能是已被合并的源。
        if target.get("merged_into"):
            raise KBError("目标文档已是合并源，请选择 canonical 文档", 409, "target_merged")
        source_cases = self._cases(source_doc_id)
        target_cases = self._cases(target_doc_id)
        if not source_cases:
            raise KBError("源文档没有可迁移案例", 409, "empty_source")
        now = iso_now()
        moved = 0
        with self.db.tx() as conn:
            max_ordinal = max([int(c.get("ordinal") or 0) for c in target_cases] or [0])
            for case in source_cases:
                snapshot = case_snapshot(case)
                next_revision = int(conn.execute(
                    "SELECT MAX(revision) AS r FROM kb_case_revisions WHERE case_id=?",
                    (str(case["id"]),)).fetchone()["r"] or 0) + 1
                write_revision(
                    conn, case_id=str(case["id"]), doc_id=str(case.get("doc_id") or ""),
                    revision=next_revision, action="merge",
                    actor_type="human", actor_id=int(user.get("id") or 0),
                    actor_name=str(user.get("username") or user.get("display_name") or ""),
                    source="web", reason=reason or f"近重复合并 {source_doc_id} → {target_doc_id}",
                    before=snapshot, after=snapshot,
                    new_id_fn=lambda: new_id("KV", with_time=False, entropy=8))
                max_ordinal += 1
                conn.execute(
                    "UPDATE kb_cases SET doc_id=?,kb_id=?,ordinal=?,revision=? WHERE id=?",
                    (target_doc_id, int(kb_id), max_ordinal, next_revision,
                     str(case["id"])))
                moved += 1
            conn.execute(
                "UPDATE kb_documents SET status='merged',merged_into=?,merged_at=?,"
                "merge_token=?,updated_at=? WHERE id=?",
                (target_doc_id, now, merge_token, now, source_doc_id))
            # 合并标签：保留目标已有值，并集补齐来源标签。
            merged_tags = {}
            for kind, column in (("boards", "boards_json"), ("domains", "domains_json"),
                                 ("versions", "versions_json")):
                values = sorted({str(x) for x in _json_list(target.get(column))}
                                | {str(x) for x in _json_list(source.get(column))})
                merged_tags[column] = values
            conn.execute(
                "UPDATE kb_documents SET boards_json=?,domains_json=?,versions_json=?,"
                "quality=MAX(quality,?),updated_at=? WHERE id=?",
                (json.dumps(merged_tags["boards_json"], ensure_ascii=False),
                 json.dumps(merged_tags["domains_json"], ensure_ascii=False),
                 json.dumps(merged_tags["versions_json"], ensure_ascii=False),
                 float(source.get("quality") or 0), now, target_doc_id))
        self.kb._mark_dirty()
        return {
            "ok": True, "noop": False, "source_doc_id": source_doc_id,
            "target_doc_id": target_doc_id, "moved_cases": moved,
            "merged_at": now, "merge_token": merge_token,
            "target_doc_url": f"/#/kb/docs/{target_doc_id}",
        }

    def _one(self, doc_id: str, kb_id: int) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM kb_documents WHERE id=? AND kb_id=? AND deleted_at IS NULL",
            (doc_id, int(kb_id)))
        if not row:
            raise KBError("文档不存在", 404, "not_found")
        if row.get("status") != "active":
            raise KBError("文档不是可合并的活跃状态", 409, "not_active")
        return row

    def _cases(self, doc_id: str) -> list[dict[str, Any]]:
        return [dict(x) for x in self.db.all(
            "SELECT * FROM kb_cases WHERE doc_id=? AND status='active' "
            "ORDER BY ordinal,id", (doc_id,))]

    def _doc_public(self, doc: dict[str, Any]) -> dict[str, Any]:
        cases = self._cases(str(doc.get("id"))) if "case_count" not in doc else []
        count = int(doc.get("case_count") if "case_count" in doc else len(cases))
        return {
            "doc_id": doc.get("id"), "title": doc.get("title") or "",
            "case_count": count, "quality": float(doc.get("quality") or 0),
            "status": doc.get("status") or "", "updated_at": doc.get("updated_at") or "",
            "boards": _json_list(doc.get("boards_json")),
            "domains": _json_list(doc.get("domains_json")),
            "versions": _json_list(doc.get("versions_json")),
        }

    def _diff(self, source: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
        cases_a = self._cases(str(source.get("id"))) if "case_count" not in source else []
        cases_b = self._cases(str(target.get("id"))) if "case_count" not in target else []
        source_count = int(source.get("case_count") if "case_count" in source else len(cases_a))
        target_count = int(target.get("case_count") if "case_count" in target else len(cases_b))
        pairs = (
            ("标题", source.get("title"), target.get("title")),
            ("案例数", source_count, target_count),
            ("板卡", _json_list(source.get("boards_json")), _json_list(target.get("boards_json"))),
            ("领域", _json_list(source.get("domains_json")), _json_list(target.get("domains_json")),
             ),
            ("版本", _json_list(source.get("versions_json")), _json_list(target.get("versions_json"))),
            ("质量", round(float(source.get("quality") or 0), 1), round(float(target.get("quality") or 0), 1)),
        )
        return [{"field": name, "source": left, "target": right}
                for name, left, right in pairs if left != right]

    def _suggest(self, source: dict[str, Any], target: dict[str, Any]) -> str:
        return str(source["id"] if _canonical_score(source) >= _canonical_score(target)
                   else target["id"])
