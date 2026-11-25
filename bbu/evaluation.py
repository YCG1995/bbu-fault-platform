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

    def evaluate(self, *, split: str = "test", limit: int = 0,
                 case_ids: list[str] | None = None, name: str = "") -> dict[str, Any]:
        cases = self._select_cases(split, case_ids, limit)
        if not cases:
            raise ValueError("没有可用于评测的已标注案例；请先 label 案例并设置 dev/test/hidden")
        eval_id = new_id("EV")
        started = time.time()
        now = iso_now()
        self.db.execute(
            "INSERT INTO evaluations(id,name,split,status,config_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (eval_id, name or f"{split}-{now[:10]}", split, "running",
             json.dumps({"deterministic": True, "limit": limit}, ensure_ascii=False), now))
        results: list[dict[str, Any]] = []
        failures = 0
        for case in cases:
            case_started = time.time()
            try:
                outcome = self._run_case(case, eval_id)
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

    def _run_case(self, case: dict[str, Any], eval_id: str) -> dict[str, Any]:
        inputs = [Path(p) for p in case["input_paths"] if Path(p).exists()]
        if not inputs:
            raise ValueError("案例输入文件不存在，可能已被移动或清理")
        work = self.data_dir / "eval_runs" / eval_id / case["id"]
        work.mkdir(parents=True, exist_ok=True)
        eval_config = copy.deepcopy(self.config)
        eval_config.values["data_dir"] = str(work / "data")
        eval_config.values["planner_enabled"] = False
        eval_config.values["planner_replan"] = False
        eval_config.values["auto_distill"] = False
        eval_config.values["report_llm"] = False
        eval_config.values["max_workers"] = 1
        eval_config.ensure_dirs()
        db = Database(eval_config.db_path)
        registry = SkillRegistry(db, Path(eval_config.skills_dir))
        registry.scan()
        gateway = AIGateway(db, eval_config, secret_box=SecretBox(eval_config.data_dir))
        runner = SkillRunner(eval_config)
        pipeline = TaskPipeline(db, eval_config, registry, gateway, runner,
                                experiences=ExperienceStore(db))
        task_id = new_id("E")
        description = case.get("description") or f"{case['title']}（真实案例评测）"
        db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
            "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, 0, case["title"][:80], description, "queued", "queued", 0, "deep",
             json.dumps([{"name": p.name, "stored_path": str(p), "size": p.stat().st_size
                          if p.is_file() else 0} for p in inputs], ensure_ascii=False),
             json.dumps({"evaluation": True, "case_id": case["id"]}, ensure_ascii=False),
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
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(f1, 3),
            "negative_cases": len(negative_cases),
            "false_positive_rate": round(negative_fp / len(negative_cases), 3) if negative_cases else None,
            "abstain_rate": round(abstain / total, 3) if total else 0.0,
            "duration_sec": duration,
            "confidence_calibration": calibration,
            "limitations": [
                "仅统计 labeled/golden 案例；unlabeled 不参与准确率。",
                "确定性评测关闭 LLM 与自学习，用于版本可复现对比。",
                "样本规模不足时，指标只能作为缺口信号，不能对外宣称现网准确率。",
            ],
        }

    @staticmethod
    def _public_case_result(r: dict[str, Any]) -> dict[str, Any]:
        case = r.get("case") or {}
        return {"case_id": case.get("id"), "case_name": case.get("name"),
                "expected": case.get("expected_roots"), "status": r.get("status"),
                "metrics": r.get("metrics"), "error": r.get("error"),
                "duration_sec": r.get("duration_sec")}


def format_evaluation_markdown(eval_id: str, summary: dict[str, Any],
                               results: list[dict[str, Any]]) -> str:
    lines = [
        "# BBU 真实案例评测报告", "",
        f"- 评测 ID：`{eval_id}`",
        f"- 案例数：{summary.get('cases')}，完成 {summary.get('completed')}，错误 {summary.get('errors')}",
        f"- 完全命中率：{summary.get('exact_rate')}",
        f"- 根因 Precision / Recall / F1：{summary.get('precision')} / {summary.get('recall')} / {summary.get('f1')}",
        f"- 阴性误报率：{summary.get('false_positive_rate')}",
        f"- 拒判率：{summary.get('abstain_rate')}",
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
