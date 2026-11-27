"""R3 P0-1 日志证据摘要测试。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.evidence_digest import build_digest  # noqa: E402
from bbu.prompts import workshop_extract_user  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_digest_inventory_classes_and_anomalies(tmp_path: Path):
    _write(tmp_path, "siteA/clockstatuslog.txt",
           "2026-01-01 00:00:00 ERROR CLK032 PLL unlocked\n"
           "2026-01-01 00:00:01 INFO ok\n")
    _write(tmp_path, "siteA/fm-active.txt", "2026-01-01 00:00:02 ALARM active\n")
    _write(tmp_path, "siteA/reset.log", "watchdog reset board 3\n")
    (tmp_path / "siteA" / "bin.dat").write_bytes(b"\x00\x01\x02")
    digest = build_digest(tmp_path)
    assert digest["inventory"]["files"] == 3
    assert digest["inventory"]["classes"]["clock"] == 1
    assert digest["inventory"]["classes"]["alarm"] == 1
    assert digest["inventory"]["classes"]["reset"] == 1
    assert digest["anomaly_lines"]
    assert digest["timeline"][0]["minute"].startswith("2026-01-01T00:00")


def test_digest_redacts_secrets_and_absolute_paths(tmp_path: Path):
    _write(tmp_path, "clock.log",
           "ERROR /home/user/private/clock.log token=sk-abcdefgh12345678\n")
    digest = build_digest(tmp_path)
    text = digest["summary_text"]
    assert "sk-abcdefgh" not in text
    assert "/home/user/private" not in text
    assert "<redacted>" in text and "<path>" in text


def test_digest_budget_and_coverage_notes(tmp_path: Path):
    _write(tmp_path, "clock.log", "\n".join(f"ERROR line {i}" for i in range(80)))
    digest = build_digest(tmp_path, max_chars=1000)
    assert len(digest["summary_text"]) <= 1020
    assert any("截断" in n for n in digest["coverage_notes"])


def test_digest_candidates_and_samples_are_bounded(tmp_path: Path):
    for i in range(4):
        _write(tmp_path, f"site{i}/clock.log", f"2026-01-01 00:0{i}:00 ERROR PLL unlocked\n")
    candidates = [{"rule_id": "E1", "pattern": "PLL unlocked", "files": 4, "hits": 4}]
    digest = build_digest(tmp_path, candidates=candidates)
    assert digest["candidates"] == [{"rule_id": "E1", "pattern": "PLL unlocked",
                                     "files": 4, "hits": 4}]
    assert len(digest["samples"]) <= 8
    assert all(len(s["lines"]) <= 600 for s in digest["samples"])


def test_workshop_prompt_contains_digest_but_stays_bounded():
    digest = {"inventory": {"files": 2, "classes": {"clock": 2}}, "coverage_notes": []}
    prompt = workshop_extract_user("资料", {"fault_name": "时钟失锁"}, digest)
    assert "日志证据摘要" in prompt
    assert "资料盘点" in prompt or '"files": 2' in prompt
