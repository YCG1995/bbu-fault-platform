"""真实案例库、可复现评测与置信度校准。

设计原则：
- 没有真实标签的日志也能入库，但绝不参与准确率统计；
- 评测使用与线上相同的 TaskPipeline，只关闭 LLM 和自学习，保证可复现；
- 每个案例保留输入哈希、原始结果和报告，失败可回放；
- 根因按归一化名称匹配，支持多根因、未知故障与阴性样本。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .ai import AIGateway
from .db import Database
from .learning import ExperienceStore
from .notifications import create_notification
from .pipeline import TaskPipeline
from .security import SecretBox
from .skills import SkillRegistry, SkillRunner
from .util import iso_now, new_id, read_json, sha256_file, write_json, write_text

UNKNOWN_ROOTS = {"__unknown__", "unknown", "未知", "未知故障", "none"}
VALID_STATUS = {"unlabeled", "labeled", "golden", "deprecated"}
VALID_SPLITS = {"unlabeled", "dev", "test", "hidden", "train"}
LEAK_SPLITS = {"dev", "test", "hidden", "train"}
VALID_LLM_MODES = {"off", "planner", "report", "full"}


def _shingles(text: str, size: int = 3) -> set[str]:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if len(normalized) < size:
        return set()
    return {normalized[i:i + size] for i in range(len(normalized) - size + 1)}


def _wilson(success: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = success / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(max(0.0, p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return [round(max(0.0, center - margin), 3), round(min(1.0, center + margin), 3)]


def _bootstrap_f1(results: list[dict[str, Any]], iterations: int = 1000) -> list[float] | None:
    rows = [r.get("metrics") or {} for r in results if (r.get("metrics") or {}).get("status")]
    if len(rows) < 3:
        return None
    rng = random.Random(20261125)
    values: list[float] = []
    for _ in range(iterations):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        tp = sum(int(r.get("tp") or 0) for r in sample)
        fp = sum(int(r.get("fp") or 0) for r in sample)
        fn = sum(int(r.get("fn") or 0) for r in sample)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        values.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    values.sort()
    return [round(values[int(0.025 * len(values))], 3),
            round(values[min(len(values) - 1, int(0.975 * len(values)))], 3)]


def normalize_root(value: Any) -> str:
    text = str(value or "").strip().lower()
    for ch in " \t\r\n_-—（）()[]【】/\\":
        text = text.replace(ch, "")
    for suffix in ("导致", "引起", "故障", "异常"):
        if text.endswith(suffix) and len(text) > len(suffix) + 2:
            text = text[: -len(suffix)]
    return text


def roots_equivalent(expected: Any, actual: Any) -> bool:
    a, b = normalize_root(expected), normalize_root(actual)
    if not a or not b:
        return False
    if a == b:
        return True
    # 允许“根因名/根因 ID”之外的等价别名，但不做模糊包含，避免把相似故障误算正确。
    return False


def _json_loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else (value if value is not None else default)
    except (TypeError, json.JSONDecodeError):
        return default


def _paths_fingerprint(paths: list[Path]) -> tuple[str, int]:
    rows = []
    total = 0
    for path in paths:
        if path.is_file():
            total += path.stat().st_size
            rows.append(f"file:{sha256_file(path)}:{path.stat().st_size}")
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file():
                    total += item.stat().st_size
                    rows.append(f"file:{item.relative_to(path).as_posix()}:{sha256_file(item)}")
        else:
            rows.append(f"missing:{path}")
    blob = "\n".join(rows).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), total


class RealCaseStore:
    def __init__(self, db: Database, data_dir: Path) -> None:
        self.db = db
        self.root = Path(data_dir) / "real_cases"
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, case_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM real_cases WHERE id=? OR name=?", (case_id, case_id))
        return self._decode(row) if row else None

    def list(self, split: str = "", status: str = "", reviewer_id: int | None = None,
             review_status: str = "", created_by: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM real_cases WHERE 1=1"
        args: list[Any] = []
        if split and split != "all":
            sql += " AND split=?"
            args.append(split)
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if reviewer_id is not None:
            sql += " AND reviewer_id=?"
            args.append(reviewer_id)
        if review_status and review_status != "all":
            sql += " AND review_status=?"
            args.append(review_status)
        if created_by:
            sql += " AND created_by=?"
            args.append(created_by)
        sql += " ORDER BY updated_at DESC"
        return [self._decode(r) for r in self.db.all(sql, tuple(args))]

    def assign_reviewer(self, case_ref: str, reviewer_id: int | None, reviewer_name: str = "",
                        actor: str = "", note: str = "") -> dict[str, Any]:
        case = self.get(case_ref)
        if not case:
            raise ValueError("案例不存在")
        now = iso_now()
        old = {"reviewer_id": case.get("reviewer_id"), "review_status": case.get("review_status")}
        self.db.execute(
            "UPDATE real_cases SET reviewer_id=?,review_status=?,assigned_at=?,updated_at=? WHERE id=?",
            (reviewer_id, "pending" if reviewer_id else "unassigned", now if reviewer_id else None,
             now, case["id"]))
        if reviewer_id:
            create_notification(self.db, reviewer_id, "case_assigned", "真实案例待复核",
                                f"{case['title']} 已分配给你，请核对根因与证据时间窗",
                                f"/#/cases/{case['id']}")
        self._record_history(case["id"], 0, actor or reviewer_name or "system",
                             "assign" if reviewer_id else "unassign", old,
                             {"reviewer_id": reviewer_id, "review_status": "pending" if reviewer_id else "unassigned"},
                             note)
        return self.get(case["id"]) or {}

    def history(self, case_ref: str) -> list[dict[str, Any]]:
        case = self.get(case_ref)
        if not case:
            raise ValueError("案例不存在")
        rows = self.db.all(
            "SELECT id,case_id,actor_id,actor_name,action,old_json,new_json,note,created_at "
            "FROM case_label_history WHERE case_id=? ORDER BY id DESC", (case["id"],))
        return [{**row, "old": _json_loads(row.pop("old_json"), {}),
                 "new": _json_loads(row.pop("new_json"), {})} for row in rows]

    def ingest(self, paths: list[Path], *, name: str, title: str = "", description: str = "",
               site: str = "", device_type: str = "", software_version: str = "",
               split: str = "unlabeled", status: str = "unlabeled", created_by: str = "",
               expected_roots: list[str] | None = None,
               excluded_roots: list[str] | None = None, notes: str = "",
               origin: str = "real", copy_inputs: bool = True) -> dict[str, Any]:
        paths = [Path(p).resolve() for p in paths]
        if not paths or not any(p.exists() for p in paths):
            raise ValueError("输入路径不存在")
        if not name.strip():
            raise ValueError("案例名称不能为空")
        if status not in VALID_STATUS:
            raise ValueError(f"非法案例状态: {status}")
        if split not in VALID_SPLITS:
            raise ValueError(f"非法数据集切分: {split}")
        if origin not in ("real", "review", "synthetic", "imported"):
            raise ValueError(f"非法案例来源: {origin}")
        expected = [str(x).strip() for x in (expected_roots or []) if str(x).strip()]
        excluded = [str(x).strip() for x in (excluded_roots or []) if str(x).strip()]
        if status in ("labeled", "golden") and not expected and not excluded:
            raise ValueError("已标注案例必须至少提供 --expected 或 --excluded")
        fingerprint, size = _paths_fingerprint(paths)
        case_id = "C-" + hashlib.sha1(f"{name}|{fingerprint}".encode()).hexdigest()[:10]
        case_dir = self.root / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        stored_paths: list[Path] = []
        if copy_inputs:
            target_dir = case_dir / "inputs"
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            for src in paths:
                if not src.exists():
                    continue
                dest = target_dir / src.name
                if src.is_dir():
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
                stored_paths.append(dest.resolve())
        else:
            stored_paths = paths
        now = iso_now()
        existing = self.db.one("SELECT id, created_at FROM real_cases WHERE name=?", (name,))
        if existing:
            case_id = existing["id"]
            created_at = existing["created_at"]
        else:
            created_at = now
        self.db.execute(
            "INSERT INTO real_cases(id,name,title,description,site,device_type,software_version,"
            "split,status,expected_roots_json,excluded_roots_json,labels_json,origin,input_paths_json,"
            "dataset_sha256,size_bytes,notes,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET title=excluded.title,"
            "description=excluded.description,site=excluded.site,"
            "device_type=excluded.device_type,software_version=excluded.software_version,"
            "split=excluded.split,status=excluded.status,"
            "expected_roots_json=excluded.expected_roots_json,"
            "excluded_roots_json=excluded.excluded_roots_json,labels_json=excluded.labels_json,"
            "origin=excluded.origin,input_paths_json=excluded.input_paths_json,"
            "dataset_sha256=excluded.dataset_sha256,"
            "size_bytes=excluded.size_bytes,notes=excluded.notes,updated_at=excluded.updated_at",
            (case_id, name.strip(), (title or name).strip(), description.strip(), site.strip(),
             device_type.strip(), software_version.strip(), split, status,
             json.dumps(expected, ensure_ascii=False), json.dumps(excluded, ensure_ascii=False),
             json.dumps({"source": "manual", "copy_inputs": copy_inputs}, ensure_ascii=False),
             origin, json.dumps([str(p) for p in stored_paths], ensure_ascii=False),
             fingerprint, size, notes.strip(), created_by.strip(), created_at, now))
        payload = self.get(case_id)
        write_json(case_dir / "case.json", payload or {})
        return payload or {}

    def update_labels(self, case_ref: str, *, expected_roots: list[str] | None = None,
                      excluded_roots: list[str] | None = None, split: str | None = None,
                      status: str | None = None, notes: str | None = None,
                      labels: dict[str, Any] | None = None, actor_id: int = 0,
                      actor_name: str = "", action: str = "label",
                      note: str = "", review_status: str | None = None) -> dict[str, Any]:
        case = self.get(case_ref)
        if not case:
            raise ValueError("案例不存在")
        expected = case["expected_roots"] if expected_roots is None else expected_roots
        excluded = case["excluded_roots"] if excluded_roots is None else excluded_roots
        next_status = status or case["status"]
        next_split = split or case["split"]
        if next_status not in VALID_STATUS:
            raise ValueError(f"非法案例状态: {next_status}")
        if next_split not in VALID_SPLITS:
            raise ValueError(f"非法数据集切分: {next_split}")
        if next_status in ("labeled", "golden") and not expected and not excluded:
            raise ValueError("已标注案例必须至少有一个期望或排除根因")
        if next_status == "golden" and next_split == "unlabeled":
            raise ValueError("golden 案例必须切分到 dev/test/hidden")
        merged_labels = {**(case.get("labels") or {}), **(labels or {})}
        next_review = review_status or case.get("review_status") or "unassigned"
        if next_review not in ("unassigned", "pending", "confirmed", "rejected"):
            raise ValueError(f"非法复核状态: {next_review}")
        now = iso_now()
        old = {"expected_roots": case.get("expected_roots"), "excluded_roots": case.get("excluded_roots"),
               "split": case.get("split"), "status": case.get("status"),
               "labels": case.get("labels"), "notes": case.get("notes")}
        self.db.execute(
            "UPDATE real_cases SET expected_roots_json=?,excluded_roots_json=?,split=?,status=?,"
            "labels_json=?,notes=?,review_status=?,updated_at=? WHERE id=?",
            (json.dumps(expected, ensure_ascii=False), json.dumps(excluded, ensure_ascii=False),
             next_split, next_status, json.dumps(merged_labels, ensure_ascii=False),
             case.get("notes", "") if notes is None else notes, next_review, now, case["id"]))
        updated = self.get(case["id"]) or {}
        self._record_history(case["id"], actor_id, actor_name or "system", action, old,
                             {"expected_roots": expected, "excluded_roots": excluded,
                              "split": next_split, "status": next_status, "labels": merged_labels,
                              "notes": updated.get("notes"), "review_status": next_review}, note)
        write_json(self.root / case["id"] / "case.json", updated)
        return updated

    def _decode(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "title": row["title"],
            "description": row["description"], "site": row["site"],
            "device_type": row["device_type"], "software_version": row["software_version"],
            "split": row["split"], "status": row["status"],
            "expected_roots": _json_loads(row["expected_roots_json"], []),
            "excluded_roots": _json_loads(row["excluded_roots_json"], []),
            "labels": _json_loads(row["labels_json"], {}),
            "origin": row.get("origin") or "real",
            "input_paths": _json_loads(row["input_paths_json"], []),
            "dataset_sha256": row["dataset_sha256"], "size_bytes": row["size_bytes"],
            "notes": row["notes"], "created_by": row["created_by"],
            "reviewer_id": row.get("reviewer_id"), "review_status": row.get("review_status") or "unassigned",
            "assigned_at": row.get("assigned_at"),
            "inputs_state": row.get("inputs_state") or "available",
            "raw_inputs_available": (row.get("inputs_state") or "available") == "available",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def _record_history(self, case_id: str, actor_id: int, actor_name: str, action: str,
                        old: dict[str, Any], new: dict[str, Any], note: str = "") -> None:
        self.db.execute(
            "INSERT INTO case_label_history(case_id,actor_id,actor_name,action,old_json,new_json,"
            "note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (case_id, actor_id, actor_name, action, json.dumps(old, ensure_ascii=False, default=str),
             json.dumps(new, ensure_ascii=False, default=str), note, iso_now()))


def _root_list(result: dict[str, Any]) -> list[str]:
    verdict = result.get("verdict") or {}
    roots = verdict.get("roots") or []
    names = [str((r or {}).get("name") or r) for r in roots if (r or {}).get("name") or r]
    if names:
        return names
    name = verdict.get("root_cause_name") or (result.get("root_cause") or {}).get("name")
    if name and str(name) not in ("NONE", "None", "none"):
        return [str(name)]
    return []


def _negative_expected(expected: list[str], excluded: list[str]) -> bool:
    if expected and any(normalize_root(x) in UNKNOWN_ROOTS for x in expected):
        return True
    return not expected and bool(excluded)


class EvaluationService:
    def __init__(self, db: Database, config: Any, data_dir: Path | None = None,
                 case_store: RealCaseStore | None = None) -> None:
        self.db = db
        self.config = config
        self.data_dir = Path(data_dir or config.data_dir)
        self.store = case_store or RealCaseStore(db, self.data_dir)

    def _model_gateway(self, llm_mode: str, model_config_id: int | None) -> AIGateway | None:
        """校验模型参与模式；返回使用主库模型配置的网关，不把密钥复制进评测目录。"""
        if llm_mode not in VALID_LLM_MODES:
            raise ValueError(f"非法 llm_mode: {llm_mode}；可选 {sorted(VALID_LLM_MODES)}")
        if llm_mode == "off":
            return None
        if model_config_id is not None:
            try:
                mid = int(model_config_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("model_config_id 必须是整数") from exc
            row = self.db.one(
                "SELECT * FROM model_configs WHERE id=? AND enabled=1", (mid,))
            if not row:
                raise ValueError(f"模型配置不存在或未启用: {mid}")
            if str(row.get("provider") or "").lower() == "mock":
                raise ValueError(f"模型配置 {mid} 是 mock provider，不能用于模型参与评测")
            try:
                roles = set(json.loads(row.get("roles") or "[]"))
            except (TypeError, json.JSONDecodeError):
                roles = set()
            required = []
            if llm_mode in ("planner", "full"):
                required.append("planner")
            if llm_mode in ("report", "full"):
                required.append("writer")
            missing = [role for role in required if role not in roles]
            if missing:
                raise ValueError(f"模型配置 {mid} 缺少角色: {', '.join(missing)}")
        gateway = AIGateway(self.db, self.config,
                            secret_box=SecretBox(Path(self.config.data_dir)))
        required = []
        if llm_mode in ("planner", "full"):
            required.append("planner")
        if llm_mode in ("report", "full"):
            required.append("writer")
        missing = [role for role in required if not gateway.has_real_model(role)]
        if missing:
            raise ValueError(
                "模型参与评测需要先配置真实模型角色: " + ", ".join(missing)
                + "；请先在模型接入中启用对应模型，或改用 llm_mode=off")
        return gateway

    @staticmethod
    def _llm_usage(results: list[dict[str, Any]]) -> dict[str, Any]:
        calls = 0
        tokens = 0
        models: set[str] = set()
        used_cases = 0
        narrative_used = 0
        narrative_fallback = 0
        for item in results:
            result = item.get("result") or {}
            stats = result.get("stats") or {}
            case_calls = int(stats.get("llm_calls") or 0)
            calls += case_calls
            used_cases += 1 if case_calls else 0
            tokens += int(stats.get("total_tokens") or 0)
            if result.get("narrative"):
                narrative_used += 1
            check = result.get("narrative_check") or {}
            if check and not check.get("skipped") and not check.get("ok"):
                narrative_fallback += 1
            for name in stats.get("models") or []:
                if name:
                    models.add(str(name))
        return {"calls": calls, "tokens": tokens, "models": sorted(models),
                "used_cases": used_cases, "narrative_used": narrative_used,
                "narrative_fallback": narrative_fallback}

    def audit_splits(self) -> dict[str, Any]:
        """跨 split 泄漏检查：输入指纹精确重复 + 描述近重复。"""
        cases = [c for c in self.store.list(split="all")
                 if c.get("status") in ("labeled", "golden") and c.get("split") in LEAK_SPLITS]
        counts: dict[str, int] = {}
        for case in cases:
            counts[case["split"]] = counts.get(case["split"], 0) + 1
        leaks: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()

        def add_pair(left: dict[str, Any], right: dict[str, Any], kind: str, score: float) -> None:
            if left["split"] == right["split"]:
                return
            key = tuple(sorted((left["id"], right["id"])))
            if key in seen_pairs:
                return
            seen_pairs.add(key)
            leaks.append({
                "kind": kind, "score": round(score, 3),
                "left_id": left["id"], "left_name": left["name"], "left_split": left["split"],
                "right_id": right["id"], "right_name": right["name"], "right_split": right["split"],
            })

        by_fingerprint: dict[str, list[dict[str, Any]]] = {}
        for case in cases:
            fingerprint = str(case.get("dataset_sha256") or "")
            if fingerprint and case.get("input_paths"):
                by_fingerprint.setdefault(fingerprint, []).append(case)
        for group in by_fingerprint.values():
            for i, left in enumerate(group):
                for right in group[i + 1:]:
                    add_pair(left, right, "exact_input", 1.0)

        candidates = [c for c in cases if len(_shingles(
            f"{c.get('title') or ''}{c.get('description') or ''}{c.get('notes') or ''}")) >= 12]
        for i, left in enumerate(candidates):
            left_text = _shingles(f"{left.get('title') or ''}{left.get('description') or ''}{left.get('notes') or ''}")
            for right in candidates[i + 1:]:
                if left["split"] == right["split"]:
                    continue
                right_text = _shingles(f"{right.get('title') or ''}{right.get('description') or ''}{right.get('notes') or ''}")
                if not left_text or not right_text:
                    continue
                score = len(left_text & right_text) / len(left_text | right_text)
                if score >= 0.82:
                    add_pair(left, right, "near_description", score)
        return {
            "ok": not leaks, "cases": len(cases), "counts": counts,
            "leaks": sorted(leaks, key=lambda x: (-x["score"], x["left_id"]))[:200],
            "thresholds": {"near_description_jaccard": 0.82},
            "limitations": [
                "只检查已标注/ golden 的 dev/test/hidden/train 案例。",
                "输入指纹来自文件路径与内容哈希；缺失原文件的历史案例无法完成精确指纹检查。",
                "近重复基于标题、描述和备注的字符三元组，不等同于专家判定。",
            ],
        }

    def _hidden_gate(self, cases: list[dict[str, Any]], summary: dict[str, Any], split: str) -> dict[str, Any]:
        min_cases = max(1, int(self.config.get("evaluation_hidden_min_cases", 20))
                        if hasattr(self.config, "get") else 20)
        min_f1 = float(self.config.get("evaluation_hidden_min_f1", 0.75))
        applied = split == "hidden"
        passed = (not applied) or (len(cases) >= min_cases and summary.get("f1", 0) >= min_f1)
        return {"applied": applied, "passed": passed, "min_cases": min_cases,
                "min_f1": min_f1, "cases": len(cases), "f1": summary.get("f1"),
                "reason": "" if passed else (
                    f"hidden 样本 {len(cases)} < {min_cases}" if len(cases) < min_cases
                    else f"hidden F1 {summary.get('f1')} < {min_f1}")}

    def evaluate(self, *, split: str = "test", limit: int = 0,
                 case_ids: list[str] | None = None, name: str = "",
                 llm_mode: str = "off",
                 model_config_id: int | None = None) -> dict[str, Any]:
        mode = str(llm_mode or "off").strip().lower()
        gateway = self._model_gateway(mode, model_config_id)
        audit = self.audit_splits()
        if split == "hidden":
            blocking = [item for item in audit["leaks"]
                        if "hidden" in (item["left_split"], item["right_split"])]
            if blocking:
                raise ValueError(
                    f"hidden 与其它 split 存在 {len(blocking)} 条泄漏，拒绝运行独立评测")
        cases = self._select_cases(split, case_ids, limit)
        if not cases:
            raise ValueError("没有可用于评测的已标注案例；请先 label 案例并设置 dev/test/hidden")
        min_cases = max(1, int(self.config.get("evaluation_hidden_min_cases", 20)))
        if split == "hidden" and len(cases) < min_cases:
            raise ValueError(
                f"hidden 独立评测至少需要 {min_cases} 个已标注案例，当前 {len(cases)} 个；"
                "数据不足时只报告缺口，不用合成案例补齐")
        eval_id = new_id("EV")
        started = time.time()
        now = iso_now()
        self.db.execute(
            "INSERT INTO evaluations(id,name,split,status,config_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (eval_id, name or f"{split}-{now[:10]}", split, "running",
             json.dumps({"deterministic": mode == "off", "llm_mode": mode,
                         "model_config_id": model_config_id, "limit": limit},
                        ensure_ascii=False), now))
        results: list[dict[str, Any]] = []
        failures = 0
        for case in cases:
            case_started = time.time()
            try:
                outcome = self._run_case(case, eval_id, mode, model_config_id, gateway)
                metrics = self._case_metrics(case, outcome["result"])
                status = "ok" if metrics["exact"] else "miss"
                results.append({"case": case, **outcome, "metrics": metrics, "status": status})
                self.db.execute(
                    "INSERT INTO evaluation_cases(evaluation_id,case_id,case_name,status,"
                    "expected_json,predicted_json,metrics_json,duration_sec,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (eval_id, case["id"], case["name"], status,
                     json.dumps({"expected": case["expected_roots"],
                                 "excluded": case["excluded_roots"]}, ensure_ascii=False),
                     json.dumps(outcome["result"].get("verdict") or {}, ensure_ascii=False, default=str),
                     json.dumps(metrics, ensure_ascii=False, default=str),
                     round(time.time() - case_started, 2), iso_now()))
            except Exception as exc:  # noqa: BLE001 - 单个案例失败不能中断评测
                failures += 1
                error = f"{type(exc).__name__}: {exc}"
                results.append({"case": case, "status": "error", "error": error,
                                "metrics": {"exact": False},
                                "duration_sec": round(time.time() - case_started, 2)})
                self.db.execute(
                    "INSERT INTO evaluation_cases(evaluation_id,case_id,case_name,status,"
                    "expected_json,predicted_json,metrics_json,duration_sec,error,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (eval_id, case["id"], case["name"], "error",
                     json.dumps({"expected": case["expected_roots"],
                                 "excluded": case["excluded_roots"]}, ensure_ascii=False),
                     "{}", "{}", round(time.time() - case_started, 2), error, iso_now()))
        summary = self._summarize(results, failures, round(time.time() - started, 2))
        summary["llm_mode"] = mode
        summary["model_config_id"] = int(model_config_id) if model_config_id is not None else None
        summary["llm"] = self._llm_usage(results)
        if mode != "off":
            summary["limitations"][1] = (
                "模型参与评测：模型只负责规划/报告叙述，根因仍由技能规则与证据链决定；"
                "结果不应当作确定性版本基线。")
        summary["leakage"] = audit
        summary["hidden_gate"] = self._hidden_gate(cases, summary, split)
        self.db.execute(
            "UPDATE evaluations SET status='done', summary_json=?, finished_at=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False, default=str), iso_now(), eval_id))
        out_dir = self.data_dir / "evaluations" / eval_id
        write_json(out_dir / "summary.json", {"id": eval_id, "summary": summary,
                                               "cases": [self._public_case_result(r) for r in results]})
        write_text(out_dir / "report.md", format_evaluation_markdown(eval_id, summary, results))
        return {"id": eval_id, "summary": summary, "cases": results, "report": str(out_dir / "report.md")}

    def _select_cases(self, split: str, case_ids: list[str] | None, limit: int) -> list[dict[str, Any]]:
        if case_ids:
            cases = [self.store.get(ref) for ref in case_ids]
            cases = [c for c in cases if c and c["status"] in ("labeled", "golden")]
        else:
            cases = self.store.list(split=split)
            cases = [c for c in cases if c["status"] in ("labeled", "golden")]
        available = [c for c in cases if (c.get("inputs_state") or "available") == "available"]
        if cases and not available:
            raise ValueError("所选案例的原始输入未随精简迁移包迁移，无法评测；请重新导入原始案例数据")
        cases = available
        if limit:
            cases = cases[:limit]
        return cases

    def _run_case(self, case: dict[str, Any], eval_id: str, llm_mode: str = "off",
                  model_config_id: int | None = None,
                  model_gateway: AIGateway | None = None) -> dict[str, Any]:
        inputs = [Path(p) for p in case["input_paths"] if Path(p).exists()]
        if not inputs:
            raise ValueError("案例输入文件不存在，可能已被移动或清理")
        work = self.data_dir / "eval_runs" / eval_id / case["id"]
        work.mkdir(parents=True, exist_ok=True)
        eval_config = copy.deepcopy(self.config)
        eval_config.values["data_dir"] = str(work / "data")
        eval_config.values["planner_enabled"] = llm_mode in ("planner", "full")
        eval_config.values["planner_replan"] = llm_mode in ("planner", "full")
        eval_config.values["auto_distill"] = False
        eval_config.values["report_llm"] = llm_mode in ("report", "full")
        eval_config.values["max_workers"] = 1
        eval_config.ensure_dirs()
        db = Database(eval_config.db_path)
        # 技能签名密钥属于实例级资产，不能在每个临时评测库里重新生成；
        # 任务产物仍写入 eval_config.data_dir，只有验签使用主库 data_dir。
        registry = SkillRegistry(db, Path(eval_config.skills_dir), data_dir=Path(self.config.data_dir),
                             manifest_mode=str(eval_config.get("skill_manifest_mode", "shadow")))
        registry.scan()
        # 模型配置来自主库，任务/证据仍然只写入本案例的临时库。
        gateway = model_gateway or AIGateway(
            db, eval_config, secret_box=SecretBox(eval_config.data_dir))
        runner = SkillRunner(eval_config)
        pipeline = TaskPipeline(db, eval_config, registry, gateway, runner,
                                experiences=ExperienceStore(db))
        task_id = new_id("E")
        description = case.get("description") or f"{case['title']}（真实案例评测）"
        db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
            "model_override,input_json,options_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, 0, case["title"][:80], description, "queued", "queued", 0, "deep",
             int(model_config_id) if model_config_id is not None else None,
             json.dumps([{"name": p.name, "stored_path": str(p), "size": p.stat().st_size
                          if p.is_file() else 0} for p in inputs], ensure_ascii=False),
             json.dumps({"evaluation": True, "case_id": case["id"],
                         "llm_mode": llm_mode}, ensure_ascii=False),
             iso_now()))
        started = time.time()
        result = pipeline.run(task_id, threading.Event())
        if not isinstance(result, dict):
            raise RuntimeError("评测流水线未返回结果")
        write_json(work / "result.json", result)
        # 复制报告，评测目录不依赖临时 tasks 目录
        report_md = work / "data" / "tasks" / task_id / "report.md"
        if report_md.exists():
            shutil.copy2(report_md, work / "report.md")
        return {"task_id": task_id, "result": result,
                "duration_sec": round(time.time() - started, 2)}

    def _case_metrics(self, case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        expected = [x for x in case["expected_roots"] if normalize_root(x) not in UNKNOWN_ROOTS]
        excluded = case["excluded_roots"]
        negative = _negative_expected(case["expected_roots"], excluded)
        actual = _root_list(result)
        status = (result.get("verdict") or {}).get("status") or ""
        matched = status == "matched" or bool(actual)
        if negative:
            exact = not matched
            tp, fp, fn = 0, (len(actual) if matched else 0), 0
        else:
            hit = [a for a in actual if any(roots_equivalent(e, a) for e in expected)]
            exact = len(hit) == len(expected) and len(actual) == len(hit)
            tp = len(hit)
            fp = len(actual) - tp
            fn = len(expected) - tp
        confidence = (result.get("verdict") or {}).get("confidence") or "low"
        return {"exact": exact, "negative": negative, "matched": matched,
                "expected": expected, "actual": actual, "tp": tp, "fp": fp, "fn": fn,
                "confidence": confidence, "status": status}

    def _summarize(self, results: list[dict[str, Any]], errors: int, duration: float) -> dict[str, Any]:
        total = len(results)
        exact = sum(1 for r in results if r.get("metrics", {}).get("exact"))
        tp = sum(r.get("metrics", {}).get("tp", 0) for r in results)
        fp = sum(r.get("metrics", {}).get("fp", 0) for r in results)
        fn = sum(r.get("metrics", {}).get("fn", 0) for r in results)
        negative_cases = [r for r in results if r.get("metrics", {}).get("negative")]
        negative_fp = sum(1 for r in negative_cases if r.get("metrics", {}).get("matched"))
        abstain = sum(1 for r in results if not r.get("metrics", {}).get("matched"))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        buckets: dict[str, dict[str, int]] = {k: {"total": 0, "exact": 0} for k in ("high", "medium", "low")}
        for r in results:
            conf = str(r.get("metrics", {}).get("confidence") or "low").lower()
            bucket = buckets.setdefault(conf, {"total": 0, "exact": 0})
            bucket["total"] += 1
            bucket["exact"] += 1 if r.get("metrics", {}).get("exact") else 0
        calibration = {k: {**v, "accuracy": round(v["exact"] / v["total"], 3) if v["total"] else None}
                       for k, v in buckets.items()}
        return {
            "cases": total, "completed": total - errors, "errors": errors,
            "exact": exact, "exact_rate": round(exact / total, 3) if total else 0.0,
            "exact_rate_ci95": _wilson(exact, total),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 3), "recall": round(recall, 3),
            "precision_ci95": _wilson(tp, tp + fp), "recall_ci95": _wilson(tp, tp + fn),
            "f1": round(f1, 3), "f1_ci95": _bootstrap_f1(results),
            "negative_cases": len(negative_cases),
            "false_positive_rate": round(negative_fp / len(negative_cases), 3) if negative_cases else None,
            "abstain_rate": round(abstain / total, 3) if total else 0.0,
            "duration_sec": duration,
            "confidence_calibration": calibration,
            "limitations": [
                "仅统计 labeled/golden 案例；unlabeled 不参与准确率。",
                "确定性评测关闭 LLM 与自学习，用于版本可复现对比。",
                "样本规模不足时，指标只能作为缺口信号，不能对外宣称现网准确率。",
                "置信区间只反映已标注样本内的抽样不确定性，不代表现网分布。",
            ],
        }

    @staticmethod
    def _public_case_result(r: dict[str, Any]) -> dict[str, Any]:
        case = r.get("case") or {}
        result = r.get("result") or {}
        stats = result.get("stats") or {}
        return {"case_id": case.get("id"), "case_name": case.get("name"),
                "expected": case.get("expected_roots"), "status": r.get("status"),
                "metrics": r.get("metrics"), "error": r.get("error"),
                "duration_sec": r.get("duration_sec"),
                "llm": {"calls": int(stats.get("llm_calls") or 0),
                        "models": stats.get("models") or [],
                        "tokens": int(stats.get("total_tokens") or 0)},
                "llm_used": bool((result.get("routing") or {}).get("llm_used")
                                 or (result.get("arbitration") or {}).get("llm_used")
                                 or result.get("narrative")
                                 or int(stats.get("llm_calls") or 0)),
                "narrative": bool(result.get("narrative")),
                }


def format_evaluation_markdown(eval_id: str, summary: dict[str, Any],
                               results: list[dict[str, Any]]) -> str:
    lines = [
        "# BBU 真实案例评测报告", "",
        f"- 评测 ID：`{eval_id}`",
        f"- 模型参与：{summary.get('llm_mode', 'off')}；调用 {((summary.get('llm') or {}).get('calls', 0))} 次；"
        f"模型 {((summary.get('llm') or {}).get('models') or ['无'])}；"
        f"模型叙述采纳 {((summary.get('llm') or {}).get('narrative_used', 0))}，"
        f"回退 {((summary.get('llm') or {}).get('narrative_fallback', 0))}",
        f"- 案例数：{summary.get('cases')}，完成 {summary.get('completed')}，错误 {summary.get('errors')}",
        f"- 完全命中率：{summary.get('exact_rate')}（95% CI {summary.get('exact_rate_ci95')}）",
        f"- 根因 Precision / Recall / F1：{summary.get('precision')} / {summary.get('recall')} / {summary.get('f1')}",
        f"- Precision 95% CI：{summary.get('precision_ci95')}；Recall 95% CI：{summary.get('recall_ci95')}；F1 95% CI：{summary.get('f1_ci95')}",
        f"- 阴性误报率：{summary.get('false_positive_rate')}",
        f"- 拒判率：{summary.get('abstain_rate')}",
        f"- 跨 split 泄漏：{'无' if (summary.get('leakage') or {}).get('ok') else len((summary.get('leakage') or {}).get('leaks') or [])}",
        f"- hidden 门禁：{json.dumps(summary.get('hidden_gate'), ensure_ascii=False)}",
        f"- 置信度校准：{json.dumps(summary.get('confidence_calibration'), ensure_ascii=False)}",
        "", "## 案例明细", "",
        "| 案例 | 期望 | 实际 | 完全命中 | 状态 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        case = item.get("case") or {}
        metrics = item.get("metrics") or {}
        lines.append(
            f"| {case.get('name') or '-'} | {'、'.join(case.get('expected_roots') or []) or '阴性/未知'} "
            f"| {'、'.join(metrics.get('actual') or []) or '未匹配'} | "
            f"{'是' if metrics.get('exact') else '否'} | {item.get('status')} | "
            f"{item.get('duration_sec', 0)}s |")
    lines += ["", "## 边界", "",
              "本报告只反映当前已标注案例与当前技能版本；未标注真实日志不计入准确率。",
              "失败案例的输入、结果与报告保存在对应评测目录，可直接回放。"]
    return "\n".join(lines) + "\n"
