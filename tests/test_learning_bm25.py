"""R3 O8 检索升级：BM25 + 别名 + 结构重排。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.db import Database  # noqa: E402
from bbu.learning import ExperienceStore, _alias_score  # noqa: E402


def _insert(db: Database, task_id: str, desc: str, root: str, hints: list[str],
            *, skill: str = "bbu-fault-clock", device: str = "") -> None:
    db.execute(
        "INSERT INTO plan_experiences(task_id,skill_name,root_cause_name,status,confidence,"
        "description,hints_json,plan_json,outcome_json,created_at,device_type,trust_level) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, skill, root, "done", "high", desc, str(hints).replace("'", '"'),
         "{}", "{}", "2026-01-01T00:00:00Z", device, "verified"))


def test_alias_score_is_bidirectional_and_phrase_based():
    assert _alias_score("PLL 失锁", "CLK032 时钟失锁 unlocked") > 0
    assert _alias_score("光模块收光异常", "optical rx power low") > 0
    assert _alias_score("SSD 寿命", "硬盘健康度 smart") > 0
    assert _alias_score("完全无关", "nothing here") == 0


def test_bm25_alias_search_prefers_clock_over_unrelated(tmp_path: Path):
    db = Database(tmp_path / "bbu.db")
    _insert(db, "T1", "NE_A 时钟失锁，PLL unlocked，CLK032", "参考时钟源失锁",
            ["clock", "pll"])
    _insert(db, "T2", "光模块收光功率低，optical rx alarm", "光模块故障",
            ["optical"], skill="bbu-fault-optical")
    _insert(db, "T3", "硬盘 SMART 寿命不足，需要更换 SSD", "硬盘寿命",
            ["ssd"], skill="bbu-fault-ssd")
    hits = ExperienceStore(db).search("PLL 失锁 clock", limit=3)
    assert hits and hits[0]["task_id"] == "T1"
    assert hits[0]["match"]["bm25"] > 0
    assert hits[0]["match"]["alias"] > 0


def test_structured_device_and_skill_bonus_reranks(tmp_path: Path):
    db = Database(tmp_path / "bbu.db")
    _insert(db, "T1", "时钟失锁 PLL", "参考时钟源失锁", ["clock"], device="VSW")
    _insert(db, "T2", "时钟失锁 PLL", "参考时钟源失锁", ["clock"], device="OTHER")
    hits = ExperienceStore(db).search("时钟失锁", device_type="VSW",
                                     skill_name="bbu-fault-clock", limit=2)
    assert hits[0]["task_id"] == "T1"
    assert hits[0]["match"]["structured"] is True
