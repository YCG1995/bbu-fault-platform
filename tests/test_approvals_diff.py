"""O11 版本 diff、双人审批与紧急发布。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.approvals import ApprovalStore, diff_profiles, publish_gate  # noqa: E402
from bbu.db import Database  # noqa: E402


def _profile(pattern: str = "PLL unlocked", negative: bool = True) -> dict:
    return {
        "analysis": {"steps": [{"rules": [
            {"id": "E1", "globs": ["clock.log"], "any_of": [pattern], "min_matches": 1},
            {"id": "E2", "globs": ["alarm.log"], "any_of": ["ALARM active"], "critical": False},
        ]}]},
        "root_causes": [
            {"id": "R1", "condition": "E1 and not E2" if negative else "E1",
             "scope": "site", "confidence": "high"},
        ],
    }


def test_diff_reports_changed_pattern_and_removed_negative():
    old = _profile()
    new = _profile("CLK032 PLL unlock", negative=False)
    new["analysis"]["steps"][0]["rules"] = [
        x for x in new["analysis"]["steps"][0]["rules"] if x["id"] != "E2"]
    diff = diff_profiles(old, new)
    assert diff["rules"]["changed"][0]["rule_id"] == "E1"
    assert diff["rules"]["changed"][0]["fields"]["any_of"]["old"] == ["PLL unlocked"]
    assert diff["rules"]["changed"][0]["fields"]["any_of"]["new"] == ["CLK032 PLL unlock"]
    assert any(x["code"] == "REMOVED_NEGATIVE_RULE" for x in diff["risks"])
    assert diff["level"] == "red"


def test_scope_widened_is_red():
    old = _profile()
    new = _profile()
    new["root_causes"][0]["scope"] = "all"
    diff = diff_profiles(old, new)
    assert any(x["code"] == "SCOPE_WIDENED" for x in diff["risks"])


def test_stable_requires_two_distinct_approvals():
    gate = publish_gate(channel="stable", approvals=[{"approver_id": 1}])
    assert gate["ok"] is False and gate["status"] == 409
    gate = publish_gate(channel="stable",
                        approvals=[{"approver_id": 1}, {"approver_id": 2}])
    assert gate["ok"] is True
    assert publish_gate(channel="canary", approvals=[{"approver_id": 1}])["ok"] is True


def test_emergency_requires_twenty_char_reason():
    gate = publish_gate(channel="stable", emergency_reason="现场紧急")
    assert gate["ok"] is False and gate["status"] == 400
    gate = publish_gate(channel="stable", emergency_reason="现场核心网中断，需立即回滚旧规则恢复业务")
    assert gate["ok"] is True and gate["emergency"] is True
    assert gate["follow_up_within_hours"] == 24


def test_approval_store_upsert_and_reject(tmp_path: Path):
    db = Database(tmp_path / "bbu.db")
    store = ApprovalStore(db)
    store.record("demo", "1.0", 1, "admin1")
    store.record("demo", "1.0", 2, "admin2", reason="核对通过")
    status = store.status("demo", "1.0")
    assert status["ok"] and status["distinct"] == 2
    store.record("demo", "1.0", 2, "admin2", decision="reject", reason="发现误命中")
    status = store.status("demo", "1.0")
    assert status["ok"] is False and status["rejects"] == 1
