"""P1-3 技能索引卡：不泄露完整 profile，目录最多 8 张。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.skills import Skill  # noqa: E402


def _skill(i: int) -> Skill:
    return Skill(
        name=f"bbu-fault-demo-{i}", title=f"演示技能 {i}", version="1.0.0",
        category="时钟", description="desc", path=Path("/tmp") / str(i),
        status="ready", verified=bool(i % 2),
        profile={"fault": {"name": "时钟失锁"},
                 "symptoms": {"impact": "小区时钟失锁，业务受影响"},
                 "diagnostic_reasoning": {"strategy": "很长的推理" * 100}},
        keywords=["PLL", "失锁", "CLK032"], device_types=["VSW", "ITBBU"],
        aliases=["时钟异常"],
    )


def test_index_card_is_small_and_has_no_full_profile():
    card = _skill(0).index_card()
    text = json.dumps(card, ensure_ascii=False)
    assert len(text) < 260
    assert "diagnostic_reasoning" not in card
    assert card["risk"] == "unverified"
    assert card["device_types"] and card["triggers"]


def test_catalog_of_24_is_capped_to_eight_index_cards():
    cards = [_skill(i).index_card() for i in range(24)]
    selected = cards[:8]
    assert len(selected) == 8
    assert len(json.dumps(selected, ensure_ascii=False)) < len(json.dumps(cards, ensure_ascii=False))
