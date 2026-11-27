"""统一证据持久化；与旧技能结果并行存在，可逐消费者切换。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .db import Database
from .evidence_model import EvidenceEvent, content_hash
from .util import iso_now, sha256_file


class EvidenceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, event: EvidenceEvent) -> bool:
        before = self.db.execute(
            "INSERT OR IGNORE INTO evidence_events(evidence_id,session_id,source_json,entity_json,"
            "event_time_json,kind,name,value_json,excerpt,parser_json,trust_json,content_sha256,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.evidence_id, event.session_id,
             json.dumps(event.source, ensure_ascii=False), json.dumps(event.entity, ensure_ascii=False),
             json.dumps(event.event_time, ensure_ascii=False), event.kind, event.name,
             json.dumps(event.value, ensure_ascii=False), event.excerpt,
             json.dumps(event.parser, ensure_ascii=False), json.dumps(event.trust, ensure_ascii=False),
             event.content_sha256, iso_now()))
        return bool(before)

    def extend_legacy(self, session_id: str, skill: str, version: str,
                      items: Iterable[dict[str, Any]], source_root: Path | str | None = None) -> list[str]:
        ids: list[str] = []
        root = Path(source_root) if source_root else None
        for item in items:
            if not isinstance(item, dict):
                continue
            file_hash = ""
            file_path = str(item.get("file") or "")
            if root is not None and file_path:
                candidate = (root / file_path).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError:
                    candidate = Path("/nonexistent")
                if candidate.is_file():
                    file_hash = sha256_file(candidate)
            event = EvidenceEvent.from_legacy(session_id, skill, version, item, file_hash)
            self.append(event)
            ids.append(event.evidence_id)
        return ids

    def get(self, evidence_id: str, session_id: str | None = None) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM evidence_events WHERE evidence_id=?", (evidence_id,))
        if not row or (session_id and row["session_id"] != session_id):
            return None
        return self._public(row)

    def list(self, session_id: str, limit: int = 1000,
             offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM evidence_events WHERE session_id=? "
            "ORDER BY created_at,evidence_id LIMIT ? OFFSET ?",
            (session_id, max(1, min(10000, limit)), max(0, int(offset))))
        return [self._public(row) for row in rows]

    def count(self, session_id: str) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM evidence_events WHERE session_id=?", (session_id,))
        return int((row or {}).get("n") or 0)

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": row["evidence_id"], "session_id": row["session_id"],
            "source": json.loads(row["source_json"]), "entity": json.loads(row["entity_json"]),
            "event_time": json.loads(row["event_time_json"]), "kind": row["kind"],
            "name": row["name"], "value": json.loads(row["value_json"]),
            "excerpt": row["excerpt"], "parser": json.loads(row["parser_json"]),
            "trust": json.loads(row["trust_json"]), "content_sha256": row["content_sha256"],
            "integrity_valid": content_hash(row["excerpt"] or "") == row["content_sha256"],
        }
