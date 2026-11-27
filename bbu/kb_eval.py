"""故障知识库评测台：数据集、运行、分层指标与失败样例下钻。

设计约束：
- 纯标准库，不引入 numpy/scipy；
- 指标只使用专家 qrels，不生成伪相关标注；
- 同一冻结索引重复运行必须得到相同指标，索引 checksum 变化时标记“不可比”；
- 空索引/空 qrels 不参与分母，不产生除零。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
import time
from typing import Any, Iterable

from .kb_service import KBError
from .util import iso_now, new_id

EVAL_KINDS = ("retrieval", "qa")
CATEGORY_ALIASES = {
    "实体精确": "实体精确",
    "entity": "实体精确",
    "exact": "实体精确",
    "board": "实体精确",
    "症状描述": "症状描述",
    "symptom": "症状描述",
    "description": "症状描述",
    "口语改写": "口语改写",
    "colloquial": "口语改写",
    "paraphrase": "口语改写",
    "库外无答案": "库外无答案",
    "no_answer": "库外无答案",
    "out_of_scope": "库外无答案",
    "negative": "库外无答案",
}
SPLIT_ALIASES = {"train": "train", "dev": "dev", "validation": "dev",
                 "test": "test", "final": "test"}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _split_qrels(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        text = str(value or "").strip()
        if not text:
            return []
        if text.startswith("["):
            items = _json_list(text)
        else:
            items = [x for x in text.replace("|", ",").replace(";", ",").split(",")]
    return [str(x).strip() for x in items if str(x).strip()]


def _category(value: Any) -> str:
    raw = str(value or "").strip()
    return CATEGORY_ALIASES.get(raw.lower(), CATEGORY_ALIASES.get(raw, raw))


def _split(value: Any) -> str:
    raw = str(value or "test").strip().lower()
    return SPLIT_ALIASES.get(raw, "test")


def _wilson(successes: int, total: int, z: float = 1.96) -> dict[str, float]:
    if total <= 0:
        return {"lower": 0.0, "upper": 0.0}
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt(max(0.0, p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return {"lower": round(max(0.0, center - spread), 4),
            "upper": round(min(1.0, center + spread), 4)}


def _dcg(rel_ids: set[str], top_ids: list[str], k: int = 10) -> float:
    return sum(1.0 / math.log2(rank + 2)
               for rank, case_id in enumerate(top_ids[:k]) if case_id in rel_ids)


def _ndcg(rel_ids: set[str], top_ids: list[str], k: int = 10) -> float:
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel_ids), k)))
    if ideal <= 0:
        return 0.0
    return min(1.0, _dcg(rel_ids, top_ids, k) / ideal)


def _metric_bucket(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    graded = [r for r in rows if r.get("qrels")]
    no_answer = [r for r in rows if not r.get("qrels")]
    recall = {k: 0 for k in (1, 5, 10)}
    reciprocal = 0.0
    ndcg = 0.0
    for row in graded:
        rel = set(row["qrels"])
        top = list(row.get("top_case_ids") or [])
        for k in recall:
            if rel & set(top[:k]):
                recall[k] += 1
        first = next((i for i, cid in enumerate(top[:10], 1) if cid in rel), 0)
        reciprocal += 1.0 / first if first else 0.0
        ndcg += _ndcg(rel, top)
    n = len(graded)
    no_answer_correct = sum(1 for r in no_answer if r.get("no_answer"))
    latencies = [int(r.get("latency_ms") or 0) for r in rows]
    summary: dict[str, Any] = {
        "queries": len(rows),
        "graded_queries": n,
        "no_answer_queries": len(no_answer),
        "recall@1": round(recall[1] / n, 4) if n else 0.0,
        "recall@5": round(recall[5] / n, 4) if n else 0.0,
        "recall@10": round(recall[10] / n, 4) if n else 0.0,
        "mrr@10": round(reciprocal / n, 4) if n else 0.0,
        "ndcg@10": round(ndcg / n, 4) if n else 0.0,
        "no_answer_accuracy": (round(no_answer_correct / len(no_answer), 4)
                               if no_answer else 0.0),
        "answered_rate": (round(sum(1 for r in rows if not r.get("no_answer")) / len(rows), 4)
                          if rows else 0.0),
        "avg_latency_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": (sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
                           if latencies else 0),
        "recall@5_ci95": _wilson(recall[5], n),
    }
    return summary


def _public_query(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"), "query": row.get("query") or "",
        "category": row.get("category") or "", "split": row.get("split") or "test",
        "kind": row.get("kind") or "open",
        "qrels": _json_list(row.get("qrels_json")),
        "created_at": row.get("created_at") or "",
    }


class KBEvaluator:
    """基于 KBService 的同步评测执行器。"""

    def __init__(self, kb) -> None:  # noqa: ANN001
        self.kb = kb
        self.db = kb.db
        self.config = kb.config

    def _admin(self, user: dict[str, Any]) -> None:
        if (user or {}).get("role") != "admin":
            raise KBError("仅管理员可使用知识库评测台", 403, "forbidden")

    def _library_checksum(self, kb_id: int) -> str:
        row = self.db.one(
            "SELECT checksum,status FROM kb_library_index_meta WHERE kb_id=?", (int(kb_id),))
        return str((row or {}).get("checksum") or "")

    def _library_name(self, kb_id: int) -> str:
        row = self.db.one("SELECT name FROM kb_libraries WHERE id=?", (int(kb_id),))
        return str((row or {}).get("name") or f"知识库 {kb_id}")

    def list_sets(self, user: dict[str, Any]) -> dict[str, Any]:
        self._admin(user)
        sets = self.db.all("SELECT * FROM kb_eval_sets ORDER BY created_at DESC")
        items: list[dict[str, Any]] = []
        for row in sets:
            count = self.db.one(
                "SELECT COUNT(*) AS n FROM kb_eval_queries WHERE set_id=?",
                (row["id"],)) or {}
            last = self.db.one(
                "SELECT id,status,created_at,finished_at FROM kb_eval_runs "
                "WHERE set_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],))
            metrics = {}
            if last:
                run = self.db.one("SELECT metrics_json FROM kb_eval_runs WHERE id=?",
                                  (last["id"],)) or {}
                metrics = self._loads(run.get("metrics_json"))
            items.append({
                **dict(row), "query_count": int(count.get("n") or 0),
                "last_run": dict(last) if last else None,
                "last_summary": (metrics or {}).get("summary") or {},
            })
        return {"items": items, "total": len(items)}

    def get_set(self, set_id: str, user: dict[str, Any],
                *, include_queries: bool = True) -> dict[str, Any]:
        self._admin(user)
        row = self.db.one("SELECT * FROM kb_eval_sets WHERE id=?", (set_id,))
        if not row:
            raise KBError("评测集不存在", 404, "not_found")
        result = dict(row)
        if include_queries:
            result["queries"] = [_public_query(q) for q in self.db.all(
                "SELECT * FROM kb_eval_queries WHERE set_id=? ORDER BY rowid", (set_id,))]
        result["query_count"] = len(result.get("queries") or []) if include_queries else int(
            (self.db.one("SELECT COUNT(*) AS n FROM kb_eval_queries WHERE set_id=?",
                         (set_id,)) or {}).get("n") or 0)
        result["runs"] = self.list_runs(set_id, user, limit=10)["items"]
        return result

    def import_set(self, user: dict[str, Any], *, name: str, data: bytes,
                   filename: str = "eval.jsonl", description: str = "",
                   kind: str = "retrieval") -> dict[str, Any]:
        self._admin(user)
        name = str(name or "").strip()
        if not name:
            raise KBError("评测集名称不能为空", 400, "bad_name")
        rows = self._parse_import(data, filename)
        if not rows:
            raise KBError("评测集没有有效查询", 400, "empty_set")
        if len(rows) > 5000:
            raise KBError("单个评测集最多 5000 条查询", 413, "too_many_queries")
        set_id = new_id("KE", with_time=False, entropy=8)
        now = iso_now()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO kb_eval_sets(id,name,kind,description,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (set_id, name[:200], kind if kind in EVAL_KINDS else "retrieval",
                 description[:1000], int(user.get("id") or 0), now))
            for index, row in enumerate(rows, 1):
                query_id = new_id("KQ", with_time=False, entropy=8)
                conn.execute(
                    "INSERT INTO kb_eval_queries(id,set_id,query,kind,qrels_json,category,"
                    "split,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (query_id, set_id, row["query"][:4000], row.get("kind") or "open",
                     json.dumps(row["qrels"], ensure_ascii=False), row["category"],
                     row["split"], now))
        result = self.get_set(set_id, user)
        result["imported"] = len(rows)
        return result

    def delete_set(self, set_id: str, user: dict[str, Any]) -> dict[str, Any]:
        self._admin(user)
        row = self.db.one("SELECT id,name FROM kb_eval_sets WHERE id=?", (set_id,))
        if not row:
            raise KBError("评测集不存在", 404, "not_found")
        self.db.execute("DELETE FROM kb_eval_sets WHERE id=?", (set_id,))
        self.db.execute("DELETE FROM kb_eval_runs WHERE set_id=?", (set_id,))
        return {"ok": True, "id": set_id, "name": row.get("name") or ""}

    def list_runs(self, set_id: str, user: dict[str, Any], *,
                  limit: int = 30) -> dict[str, Any]:
        self._admin(user)
        rows = self.db.all(
            "SELECT * FROM kb_eval_runs WHERE set_id=? ORDER BY created_at DESC LIMIT ?",
            (set_id, max(1, min(100, int(limit or 30)))))
        items = []
        for row in rows:
            metrics = self._loads(row.get("metrics_json"))
            items.append({
                "id": row.get("id"), "set_id": row.get("set_id"),
                "status": row.get("status"), "created_at": row.get("created_at"),
                "finished_at": row.get("finished_at"),
                "summary": (metrics or {}).get("summary") or {},
                "library_id": (metrics or {}).get("library_id"),
                "library_name": (metrics or {}).get("library_name"),
                "index_checksum": (metrics or {}).get("index_checksum") or "",
                "comparable": (metrics or {}).get("comparable", True),
                "warnings": (metrics or {}).get("warnings") or [],
            })
        return {"items": items, "total": len(items)}

    def get_run(self, run_id: str, user: dict[str, Any]) -> dict[str, Any]:
        self._admin(user)
        row = self.db.one("SELECT * FROM kb_eval_runs WHERE id=?", (run_id,))
        if not row:
            raise KBError("评测运行不存在", 404, "not_found")
        result = dict(row)
        result["metrics"] = self._loads(row.get("metrics_json"))
        result["failures"] = self._loads(row.get("failures_json"))
        return result

    def run_set(self, set_id: str, user: dict[str, Any], *,
                kb_id: int = 1, top_k: int = 10) -> dict[str, Any]:
        self._admin(user)
        set_row = self.db.one("SELECT * FROM kb_eval_sets WHERE id=?", (set_id,))
        if not set_row:
            raise KBError("评测集不存在", 404, "not_found")
        queries = self.db.all(
            "SELECT * FROM kb_eval_queries WHERE set_id=? ORDER BY rowid", (set_id,))
        if not queries:
            raise KBError("评测集没有查询", 409, "empty_set")
        kb_id = max(1, int(kb_id or 1))
        # 防止对不可见库运行：通过一次空查询验证权限。
        self.kb.search("__eval_permission_check__", user, kb_id=kb_id, limit=1, log=False)
        self.kb.ensure_index(kb_id)
        before_checksum = self._library_checksum(kb_id)
        run_id = new_id("KR", with_time=False, entropy=8)
        started = time.monotonic()
        self.db.execute(
            "INSERT INTO kb_eval_runs(id,set_id,status,created_by,created_at) "
            "VALUES(?,?,?,?,?)", (run_id, set_id, "running", int(user.get("id") or 0),
                                  iso_now()))
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for query in queries:
            qrels = [str(x) for x in _json_list(query.get("qrels_json"))]
            started_q = time.monotonic()
            error = ""
            result: dict[str, Any] = {}
            try:
                result = self.kb.search(
                    str(query.get("query") or ""), user, kb_id=kb_id, limit=top_k, log=False)
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整个评测
                error = str(exc)[:300]
            latency = int((time.monotonic() - started_q) * 1000)
            items = result.get("items") or []
            top_ids = [str(item.get("case_id") or "") for item in items][:top_k]
            top_titles = [str(item.get("title") or "") for item in items][:top_k]
            no_answer = bool(result.get("no_answer")) or (not top_ids)
            hit_rank = next((i for i, cid in enumerate(top_ids, 1) if cid in set(qrels)), 0)
            row = {
                "query_id": query.get("id"), "query": query.get("query") or "",
                "category": query.get("category") or "", "split": query.get("split") or "test",
                "qrels": qrels, "top_case_ids": top_ids, "top_titles": top_titles,
                "no_answer": no_answer, "latency_ms": latency,
                "hit_rank": hit_rank, "error": error,
            }
            rows.append(row)
            if error:
                failures.append({**row, "reason": f"检索错误：{error}"})
            elif qrels and not set(qrels) & set(top_ids[:top_k]):
                failures.append({**row, "reason": "Top10 未命中任何相关案例"})
            elif not qrels and not no_answer:
                failures.append({**row, "reason": "库外问题未拒答"})
        after_checksum = self._library_checksum(kb_id)
        warning_list: list[str] = []
        if before_checksum and after_checksum and before_checksum != after_checksum:
            warning_list.append("运行期间索引 checksum 变化，本次结果不可比")
        previous = self.db.one(
            "SELECT metrics_json FROM kb_eval_runs WHERE set_id=? AND id!=? "
            "AND status='success' ORDER BY created_at DESC LIMIT 1", (set_id, run_id))
        previous_checksum = str((self._loads((previous or {}).get("metrics_json"))
                                 .get("index_checksum") or "")) if previous else ""
        comparable = True
        if previous_checksum and before_checksum and previous_checksum != before_checksum:
            comparable = False
            warning_list.append("索引已变化，与上一次运行不可直接比较")

        summary = _metric_bucket(rows)
        by_category = {}
        for category in sorted({r["category"] for r in rows if r["category"]}):
            by_category[category] = _metric_bucket(
                [r for r in rows if r["category"] == category])
        by_split = {}
        for split in sorted({r["split"] for r in rows}):
            by_split[split] = _metric_bucket([r for r in rows if r["split"] == split])
        qrels_payload = "\n".join(
            f"{q.get('id')}:{','.join(str(x) for x in _json_list(q.get('qrels_json')))}"
            for q in queries)
        metrics = {
            "schema": "kb-eval/1",
            "run_id": run_id, "set_id": set_id, "set_name": set_row.get("name") or "",
            "library_id": kb_id, "library_name": self._library_name(kb_id),
            "top_k": max(1, min(10, int(top_k or 10))),
            "config_hash": hashlib.sha256(json.dumps({
                "no_answer_threshold": self.config.get("kb_no_answer_threshold", 0.18),
                "index_max_cases": self.config.get("kb_index_max_cases", 20000),
                "schema": 27,
            }, sort_keys=True).encode()).hexdigest()[:24],
            "prompt_version": "kb-retrieval-v1",
            "index_checksum": before_checksum,
            "qrels_sha256": hashlib.sha256(qrels_payload.encode()).hexdigest()[:24],
            "comparable": comparable, "warnings": warning_list,
            "summary": summary, "by_category": by_category, "by_split": by_split,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "run_at": iso_now(),
        }
        self.db.execute(
            "UPDATE kb_eval_runs SET status='success',metrics_json=?,failures_json=?,"
            "finished_at=? WHERE id=?",
            (json.dumps(metrics, ensure_ascii=False), json.dumps(failures, ensure_ascii=False),
             iso_now(), run_id))
        return {
            "id": run_id, "status": "success", "metrics": metrics,
            "failures": failures, "summary": summary,
        }

    def export_run(self, run_id: str, user: dict[str, Any], fmt: str = "csv") -> tuple[bytes, str]:
        run = self.get_run(run_id, user)
        metrics = run.get("metrics") or {}
        failures = run.get("failures") or []
        if fmt == "md":
            return self._export_md(run, metrics, failures), "text/markdown; charset=utf-8"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["query_id", "query", "category", "split", "qrels",
                         "top_case_ids", "hit_rank", "no_answer", "latency_ms", "reason"])
        for row in failures:
            writer.writerow([row.get("query_id"), row.get("query"), row.get("category"),
                             row.get("split"), ",".join(row.get("qrels") or []),
                             ",".join(row.get("top_case_ids") or []),
                             row.get("hit_rank"), row.get("no_answer"), row.get("latency_ms"),
                             row.get("reason")])
        return ("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8"

    def _parse_import(self, data: bytes, filename: str) -> list[dict[str, Any]]:
        text = data.decode("utf-8-sig")
        suffix = (filename or "").lower().rsplit(".", 1)[-1]
        if suffix == "csv":
            return self._parse_csv(text)
        stripped = text.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            return self._normalize_items(parsed if isinstance(parsed, list) else [parsed])
        items = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except ValueError as exc:
                raise KBError(f"第 {line_no} 行 JSON 无效：{exc}", 400, "bad_eval_file")
        return self._normalize_items(items)

    def _parse_csv(self, text: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise KBError("CSV 缺少表头", 400, "bad_eval_file")
        items = []
        for row in reader:
            items.append({
                "query": row.get("query") or row.get("question") or row.get("问题") or "",
                "qrels": row.get("qrels") or row.get("case_ids") or row.get("相关案例") or "",
                "category": row.get("category") or row.get("类别") or "",
                "split": row.get("split") or row.get("分组") or "test",
                "kind": row.get("kind") or "open",
            })
        return self._normalize_items(items)

    def _normalize_items(self, items: list[Any]) -> list[dict[str, Any]]:
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or item.get("question") or item.get("问题") or "").strip()
            if not query:
                continue
            qrels = _split_qrels(item.get("qrels") or item.get("case_ids") or [])
            rows.append({
                "query": query, "qrels": qrels,
                "category": _category(item.get("category") or item.get("类别") or ""),
                "split": _split(item.get("split") or item.get("分组") or "test"),
                "kind": str(item.get("kind") or "open")[:40],
            })
        return rows

    def _loads(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}

    def _export_md(self, run: dict[str, Any], metrics: dict[str, Any],
                   failures: list[dict[str, Any]]) -> bytes:
        summary = metrics.get("summary") or {}
        lines = [
            f"# 知识库评测报告 {metrics.get('set_name') or run.get('set_id')}",
            "",
            f"- 运行 ID：{run.get('id')}",
            f"- 知识库：{metrics.get('library_name')} (id={metrics.get('library_id')})",
            f"- 索引 checksum：{metrics.get('index_checksum') or '—'}",
            f"- 可比：{'是' if metrics.get('comparable') else '否'}",
            "",
            "## 汇总",
            "",
            "| 指标 | 值 |",
            "| --- | --- |",
            f"| 查询数 | {summary.get('queries', 0)} |",
            f"| Recall@1 | {summary.get('recall@1', 0):.2%} |",
            f"| Recall@5 | {summary.get('recall@5', 0):.2%} |",
            f"| Recall@10 | {summary.get('recall@10', 0):.2%} |",
            f"| MRR@10 | {summary.get('mrr@10', 0):.4f} |",
            f"| nDCG@10 | {summary.get('ndcg@10', 0):.4f} |",
            f"| 无答案准确率 | {summary.get('no_answer_accuracy', 0):.2%} |",
            "",
            "## 失败样例",
            "",
            "| 查询 | 类别 | 原因 |",
            "| --- | --- | --- |",
        ]
        for item in failures[:100]:
            lines.append(f"| {str(item.get('query') or '').replace('|', '/')} | "
                         f"{item.get('category') or ''} | {str(item.get('reason') or '').replace('|', '/')} |")
        return ("\n".join(lines) + "\n").encode("utf-8")
