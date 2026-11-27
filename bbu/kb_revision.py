"""案例级追加式修订台账：字段级 diff、zlib 快照和案例内哈希链。

台账不参与检索；它只记录“知识曾经是什么样、谁在何时为何改了它”。
"""
from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any, Iterable

from .util import iso_now

SNAPSHOT_FIELDS = (
    "title", "title_clean", "symptom", "environment", "expected", "actual", "repro",
    "found_date", "location", "cause", "cause_detail", "solution", "prevention",
    "root_cause_key", "sections", "boards", "domains", "versions", "quality",
    "confidence", "status", "ordinal", "is_derived",
)
LIST_FIELDS = ("sections", "boards", "domains", "versions")
JSON_FIELDS = {
    "sections": "sections_json", "boards": "boards_json",
    "domains": "domains_json", "versions": "versions_json",
}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def case_snapshot(row: dict[str, Any], tags: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """把数据库行转换成可 diff、可恢复的稳定快照。"""
    snap: dict[str, Any] = {}
    for field in SNAPSHOT_FIELDS:
        if field in LIST_FIELDS:
            if tags is not None and field in tags:
                snap[field] = sorted({str(x) for x in (tags.get(field) or [])})
            else:
                snap[field] = _json_list(row.get(JSON_FIELDS[field]))
        elif field == "is_derived":
            snap[field] = bool(row.get(field))
        elif field == "ordinal":
            snap[field] = int(row.get(field) or 1)
        elif field in {"quality", "confidence"}:
            snap[field] = float(row.get(field) or 0)
        else:
            snap[field] = str(row.get(field) or "")
    return snap


def case_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    before = before or {}
    changes: list[dict[str, Any]] = []
    for field in SNAPSHOT_FIELDS:
        old = before.get(field)
        new = after.get(field)
        if field in LIST_FIELDS:
            old_list = old if isinstance(old, list) else _json_list(old)
            new_list = new if isinstance(new, list) else _json_list(new)
            if sorted(str(x) for x in old_list) == sorted(str(x) for x in new_list):
                continue
            changes.append({"field": field, "op": "update", "before": old_list, "after": new_list})
            continue
        old_norm = bool(old) if field == "is_derived" else old
        new_norm = bool(new) if field == "is_derived" else new
        if old_norm == new_norm:
            continue
        if old in (None, "", [], 0, False) and new not in (None, "", [], 0, False):
            op = "add"
        elif new in (None, "", [], 0, False) and old not in (None, "", [], 0, False):
            op = "remove"
        else:
            op = "change"
        changes.append({"field": field, "op": op, "before": old, "after": new})
    return changes


def _actor_key(actor_type: str, actor_id: int | None, actor_name: str) -> str:
    return f"{actor_type}:{int(actor_id or 0)}:{actor_name}"


def _entry_hash(prev_hash: str, snapshot_sha256: str, action: str,
                actor_type: str, actor_id: int | None, actor_name: str,
                created_at: str) -> str:
    payload = (f"{prev_hash}{snapshot_sha256}{action}"
               f"{_actor_key(actor_type, actor_id, actor_name)}{created_at}")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_revision(
    conn,
    *, case_id: str, doc_id: str, revision: int, action: str,
    actor_type: str, actor_id: int | None, actor_name: str,
    source: str, reason: str, before: dict[str, Any] | None,
    after: dict[str, Any], request_id: str = "", review_id: str = "",
    model: str = "", prompt_version: str = "", created_at: str | None = None,
    new_id_fn=None,
) -> dict[str, Any] | None:
    """在调用方事务内追加一条修订；无字段变化且不是建库/恢复动作时跳过。"""
    diff = case_diff(before, after)
    if not diff and action not in {"create", "import", "restore"}:
        return None
    created_at = created_at or iso_now()
    prev = conn.execute(
        "SELECT entry_hash FROM kb_case_revisions WHERE case_id=? ORDER BY revision DESC LIMIT 1",
        (case_id,)).fetchone()
    prev_hash = str(prev["entry_hash"] if prev else "")
    snapshot_json = _canonical(after)
    snapshot_sha = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    entry_hash = _entry_hash(prev_hash, snapshot_sha, action, actor_type,
                             actor_id, actor_name, created_at)
    row_id = (new_id_fn or (lambda: hashlib.sha256(
        f"{case_id}:{revision}:{created_at}".encode()).hexdigest()[:20]))()
    conn.execute(
        "INSERT INTO kb_case_revisions(id,case_id,doc_id,revision,action,actor_type,actor_id,"
        "actor_name,source,reason,diff_json,snapshot_z,request_id,review_id,model,"
        "prompt_version,prev_hash,entry_hash,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_id, case_id, doc_id, int(revision), action, actor_type, actor_id,
         actor_name[:120], source, reason[:500], _canonical(diff),
         zlib.compress(snapshot_json.encode("utf-8")), request_id[:80], review_id[:80],
         model[:120], prompt_version[:80], prev_hash, entry_hash, created_at))
    return {"id": row_id, "revision": int(revision), "diff": diff,
            "entry_hash": entry_hash, "prev_hash": prev_hash}


def write_revision_snapshot(
    conn,
    *, case_id: str, doc_id: str, revision: int, action: str,
    actor_type: str, actor_id: int | None, actor_name: str,
    source: str, reason: str, diff: list[dict[str, Any]] | None,
    snapshot: dict[str, Any], request_id: str = "", review_id: str = "",
    model: str = "", prompt_version: str = "", created_at: str | None = None,
    prev_hash: str = "", new_id_fn=None,
) -> dict[str, Any]:
    """按原始快照重建一条修订；用于备份恢复，保留修订链语义但不复用源 ID。"""
    created_at = created_at or iso_now()
    snapshot_json = _canonical(snapshot)
    snapshot_sha = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    entry_hash = _entry_hash(prev_hash, snapshot_sha, action, actor_type,
                             actor_id, actor_name, created_at)
    row_id = (new_id_fn or (lambda: hashlib.sha256(
        f"{case_id}:{revision}:{created_at}:restore".encode()).hexdigest()[:20]))()
    conn.execute(
        "INSERT INTO kb_case_revisions(id,case_id,doc_id,revision,action,actor_type,actor_id,"
        "actor_name,source,reason,diff_json,snapshot_z,request_id,review_id,model,"
        "prompt_version,prev_hash,entry_hash,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_id, case_id, doc_id, int(revision), action, actor_type, actor_id or 0,
         actor_name[:120], source, reason[:500], _canonical(diff or []),
         zlib.compress(snapshot_json.encode("utf-8")), request_id[:80], review_id[:80],
         model[:120], prompt_version[:80], prev_hash, entry_hash, created_at))
    return {"id": row_id, "revision": int(revision), "entry_hash": entry_hash,
            "prev_hash": prev_hash}


def decode_snapshot(value: Any) -> dict[str, Any]:
    if value in (None, b"", ""):
        return {}
    try:
        raw = zlib.decompress(value if isinstance(value, bytes) else bytes(value))
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, zlib.error):
        return {}


def verify_chain(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    checked = 0
    prev_hash = ""
    broken: dict[str, Any] | None = None
    for row in rows:
        checked += 1
        snap = decode_snapshot(row.get("snapshot_z"))
        snapshot_json = _canonical(snap)
        snapshot_sha = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        expected = _entry_hash(
            str(row.get("prev_hash") or ""), snapshot_sha, str(row.get("action") or ""),
            str(row.get("actor_type") or ""), row.get("actor_id"),
            str(row.get("actor_name") or ""), str(row.get("created_at") or ""))
        if (str(row.get("prev_hash") or "") != prev_hash
                or str(row.get("entry_hash") or "") != expected
                or not snap):
            broken = {"revision": row.get("revision"), "expected": expected,
                      "stored": row.get("entry_hash") or "",
                      "reason": "snapshot" if not snap else
                      ("prev_hash" if str(row.get("prev_hash") or "") != prev_hash else "entry_hash")}
            break
        prev_hash = str(row.get("entry_hash") or "")
    return {"ok": broken is None, "checked": checked, "broken": broken}
