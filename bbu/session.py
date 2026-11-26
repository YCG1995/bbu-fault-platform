"""事件溯源诊断会话；只保存公开决策摘要，不保存自由思维链。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import Database
from .util import iso_now


class RevisionConflict(ValueError):
    pass


class DiagnosticSession:
    TERMINAL = {"resolved", "closed", "unknown_fault", "cancelled", "failed"}

    def __init__(self, db: Database, task_id: str) -> None:
        self.db = db
        self.task_id = task_id

    def ensure(self) -> dict[str, Any]:
        if not self.db.one("SELECT 1 FROM session_events WHERE task_id=? LIMIT 1", (self.task_id,)):
            self.append("session_created", {"state": "intake"}, event_key="session-created")
        return self.snapshot()

    @property
    def revision(self) -> int:
        row = self.db.one(
            "SELECT COALESCE(MAX(revision),0) AS revision FROM session_events WHERE task_id=?",
            (self.task_id,))
        return int((row or {}).get("revision") or 0)

    def append(self, event_type: str, payload: dict[str, Any], *,
               expected_revision: int | None = None,
               event_key: str | None = None) -> dict[str, Any]:
        with self.db.tx() as conn:
            if event_key:
                existing = conn.execute(
                    "SELECT * FROM session_events WHERE task_id=? AND event_key=?",
                    (self.task_id, event_key)).fetchone()
                if existing:
                    return self._row(dict(existing))
            current = int(conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM session_events WHERE task_id=?",
                (self.task_id,)).fetchone()[0])
            if expected_revision is not None and current != expected_revision:
                raise RevisionConflict(f"会话版本冲突：当前 {current}，提交基于 {expected_revision}")
            revision = current + 1
            now = iso_now()
            try:
                conn.execute(
                    "INSERT INTO session_events(task_id,revision,event_type,payload_json,event_key,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (self.task_id, revision, event_type,
                     json.dumps(payload, ensure_ascii=False, default=str), event_key, now))
            except sqlite3.IntegrityError as exc:
                if event_key:
                    row = conn.execute(
                        "SELECT * FROM session_events WHERE task_id=? AND event_key=?",
                        (self.task_id, event_key)).fetchone()
                    if row:
                        return self._row(dict(row))
                raise RevisionConflict(f"会话版本冲突：当前已变为 {revision - 1}") from exc
        return {"task_id": self.task_id, "revision": revision, "event_type": event_type,
                "payload": payload, "created_at": now}

    def snapshot(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "task_id": self.task_id, "revision": 0, "state": "intake",
            "hypotheses": {}, "gaps": [], "supplement_round": 0,
            "decision_summary": [], "input_versions": [], "last_input": {}, "budget": {},
        }
        for event in self.events():
            payload = event["payload"]
            state["revision"] = event["revision"]
            if "state" in payload:
                state["state"] = payload["state"]
            kind = event["event_type"]
            if kind == "run_started" and payload.get("budget"):
                state["budget"] = payload["budget"]
            if kind == "hypothesis_updated":
                hypothesis = dict(payload.get("hypothesis") or {})
                hypothesis["state"] = str(payload.get("hypothesis_state")
                                         or hypothesis.get("state") or "")
                hid = str(hypothesis.get("hypothesis_id") or "")
                if hid:
                    state["hypotheses"][hid] = hypothesis
            elif kind == "gaps_updated":
                state["gaps"] = list(payload.get("gaps") or [])[:3]
            elif kind == "input_added":
                state["supplement_round"] += 1
                item = {k: payload.get(k) for k in (
                    "revision", "input_hash", "files", "answers", "invalidated_skills")}
                state["input_versions"].append(item)
                state["last_input"] = item
            elif kind == "decision_summary":
                state["decision_summary"].append(payload)
                state["decision_summary"] = state["decision_summary"][-100:]
                if payload.get("budget"):
                    state["budget"] = payload["budget"]
        return state

    def update_hypothesis(self, hypothesis: dict[str, Any], *, reason: str,
                          event_key: str | None = None,
                          state: str = "evaluating") -> None:
        item = dict(hypothesis)
        item["last_transition_reason"] = reason
        self.append("hypothesis_updated", {"hypothesis": item,
                                           "hypothesis_state": state},
                    event_key=event_key)

    def questions(self) -> list[dict[str, Any]]:
        snapshot = self.snapshot()
        if snapshot.get("state") in self.TERMINAL:
            return []
        gaps = snapshot.get("gaps") or []
        if gaps:
            return gaps
        task = self.db.one("SELECT result_json FROM tasks WHERE id=?", (self.task_id,))
        try:
            result = json.loads((task or {}).get("result_json") or "{}")
        except json.JSONDecodeError:
            result = {}
        missing = ((result.get("arbitration") or {}).get("missing_info") or [])[:3]
        return [{"question_id": f"Q{i + 1}", "question": text, "blocking": True,
                 "distinguishes": list((snapshot.get("hypotheses") or {}).keys())[:3]}
                for i, text in enumerate(missing)]

    def event(self, event_key: str) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM session_events WHERE task_id=? AND event_key=?",
            (self.task_id, event_key))
        return self._row(row) if row else None

    def events(self) -> list[dict[str, Any]]:
        return [self._row(row) for row in self.db.all(
            "SELECT * FROM session_events WHERE task_id=? ORDER BY revision", (self.task_id,))]

    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": row["task_id"], "revision": row["revision"],
                "event_type": row["event_type"], "payload": json.loads(row["payload_json"] or "{}"),
                "event_key": row.get("event_key"), "created_at": row["created_at"]}
