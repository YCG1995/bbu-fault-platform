"""R2 真值表：O1 逐文件量词、O2 站点作用域、O3 关联键贯穿布尔根因。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "meta/bbu-fault-skill-builder/scripts"))
from search_evidence import eval_verdict, search_all  # noqa: E402


def _root() -> Path:
    return Path(tempfile.mkdtemp(prefix="bbu-scope-"))


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _rule(**kw):
    base = {"id": "E1", "type": "text", "globs": ["**/*.log"], "any_of": ["ERR"]}
    base.update(kw)
    return base


def _result(rule_id: str, **kw):
    base = {"rule_id": rule_id, "type": "text", "hit": True, "min_matches": 1,
            "hits_by_site": {}, "hits_by_associate": {}, "evidence": []}
    base.update(kw)
    return base


def _verdict(root_causes, results, sites=("A", "B")):
    return eval_verdict(root_causes, results, [], [], list(sites), False, "on")


# ---------------------------------------------------------------- O1
def test_o1_five_files_one_hit_each_at_least_three():
    root = _root()
    for i in range(5):
        _write(root, f"f{i}.log", "ERR\n")
    res = search_all(root, [_rule(min_files=3, file_scope="at_least")], 50, 8, False)[0]
    assert res["hit"] is True
    assert len(res["files_hit"]) == 5 and res["files_eligible"] == 5
    assert res["file_coverage"] == 1.0
    assert res["files_missed"] == []


def test_o1_one_file_five_hits_does_not_satisfy_three_files():
    root = _root()
    _write(root, "f0.log", "ERR\n" * 5)
    _write(root, "f1.log", "ERR\n")
    res = search_all(root, [_rule(min_files=3, file_scope="at_least")], 50, 8, False)[0]
    assert res["hit"] is False
    assert res["match_count"] == 6
    assert res["files_hit"] == ["f0.log", "f1.log"]
    assert res["hits_by_file"]["f0.log"] == 5


def test_o1_three_files_two_hits_each_min_matches_two():
    root = _root()
    for i in range(3):
        _write(root, f"f{i}.log", "ERR\nERR\n")
    res = search_all(root, [_rule(min_files=3, min_matches=2, file_scope="at_least")],
                     50, 8, False)[0]
    assert res["hit"] is True
    assert len(res["files_hit"]) == 3
    assert all(v == 2 for v in res["hits_by_file"].values())


def test_o1_each_scope_rejects_one_short_file():
    root = _root()
    for i in range(3):
        _write(root, f"f{i}.log", "ERR\nERR\n")
    _write(root, "short.log", "ERR\n")
    res = search_all(root, [_rule(min_matches=2, file_scope="each", min_files=0)],
                     50, 8, False)[0]
    assert res["hit"] is False
    assert res["files_missed"] == ["short.log"]
    assert res["file_coverage"] == 0.75


def test_o1_skipped_binary_files_do_not_become_zero_file_hit():
    root = _root()
    (root / "binary.log").write_bytes(b"\x00\x01\x02\x03\xff" * 100)
    res = search_all(root, [_rule(min_files=1, file_scope="at_least")], 50, 8, False)[0]
    assert res["hit"] is False
    assert res["files_eligible"] == 0
    assert res["files_skipped"] >= 1
    assert res["hits_by_file"] == {}


# ---------------------------------------------------------------- O2
def test_o2_cross_site_e1_e2_is_unmatched():
    results = [
        _result("E1", hits_by_site={"A": 1}),
        _result("E2", hits_by_site={"B": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "site"}], results)
    assert v["status"] == "unmatched"
    assert v["primary"] == "NONE"
    assert v["multi_site_conflict"] is True
    assert v["groups"]["ALL"]["primary"] == "R1"
    assert v["groups"]["A"]["primary"] == "NONE"


def test_o2_same_site_e1_e2_establishes_primary_site():
    results = [
        _result("E1", hits_by_site={"A": 1}),
        _result("E2", hits_by_site={"A": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "site"}], results)
    assert v["status"] == "matched"
    assert v["primary"] == "R1"
    assert v["primary_site"] == "A"
    assert v["established_sites"] == ["A"]


def test_o2_all_bucket_does_not_supply_missing_site_rule():
    results = [
        _result("E1", hits_by_site={"A": 1}),
        _result("E2", hits_by_site={"ALL": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "site"}], results)
    assert v["status"] == "unmatched"
    assert v["multi_site_conflict"] is True


def test_o2_single_site_profile_keeps_legacy_result():
    results = [
        _result("E1", hits_by_site={"ALL": 1}),
        _result("E2", hits_by_site={"ALL": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "site"}], results, sites=("A",))
    assert v["status"] == "matched" and v["primary"] == "R1"
    assert v["scope"] == "all"


def test_o2_two_sites_established_lists_all():
    results = [
        _result("E1", hits_by_site={"A": 1, "B": 1}),
        _result("E2", hits_by_site={"A": 1, "B": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "site"}], results)
    assert v["established_sites"] == ["A", "B"]
    assert v["primary_site"] == "A"


# ---------------------------------------------------------------- O3
def test_o3_cross_key_e1_e2_is_unmatched():
    results = [
        _result("E1", hits_by_associate={"SN1": 1}),
        _result("E2", hits_by_associate={"SN2": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "associate"}], results,
                 sites=("A",))
    assert v["status"] == "unmatched" and v["primary"] == "NONE"
    assert v["keys_evaluated"] == ["SN1", "SN2"]
    assert v["keys_established"] == []
    assert {m["key"] for m in v["keys_missing"]} == {"SN1", "SN2"}


def test_o3_same_key_establishes_primary_key():
    results = [
        _result("E1", hits_by_associate={"SN1": 1}),
        _result("E2", hits_by_associate={"SN1": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "associate"}], results,
                 sites=("A",))
    assert v["status"] == "matched" and v["primary"] == "R1"
    assert v["primary_key"] == "SN1" and v["keys_established"] == ["SN1"]


def test_o3_missing_key_not_borrowed_from_all():
    results = [
        _result("E1", hits_by_associate={"SN1": 1}),
        _result("E2", hits_by_associate={"ALL": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "associate"}], results,
                 sites=("A",))
    assert v["status"] == "unmatched"
    assert v["keys_missing"][0]["rules"] == ["E2"]


def test_o3_not_is_local_to_key():
    results = [
        _result("E1", hits_by_associate={"SN1": 1}),
        _result("E3", hits_by_associate={"SN2": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and not E3", "scope": "associate"}],
                 results, sites=("A",))
    assert v["keys_established"] == ["SN1"]
    assert v["primary_key"] == "SN1"


def test_o3_two_keys_established_lists_all():
    results = [
        _result("E1", hits_by_associate={"SN1": 1, "SN2": 1}),
        _result("E2", hits_by_associate={"SN1": 1, "SN2": 1}),
    ]
    v = _verdict([{"id": "R1", "condition": "E1 and E2", "scope": "associate"}], results,
                 sites=("A",))
    assert v["keys_established"] == ["SN1", "SN2"]
    assert v["primary_key"] == "SN1"
