"""置信度校准：只用 calibration/dev 集拟合，hidden/test 只评估。

不输出伪精确概率：保留 low/medium/high 三档，附加“该档在 dev 集上的经验
准确率 + Wilson 区间 + 样本数”。样本不足或分档缺失时明确 `uncalibrated`。
"""
from __future__ import annotations

import json
import math
from typing import Any

from .db import Database
from .util import iso_now


def wilson_interval(success: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = success / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)]


class CalibrationStore:
    def __init__(self, db: Database, config: Any) -> None:
        self.db = db
        self.config = config

    def _min_samples(self) -> int:
        return max(1, int(self.config.get("calibration_min_samples", 20)
                          if self.config is not None else 20))

    def fit(self, split: str = "dev") -> dict[str, Any]:
        if split == "hidden":
            raise ValueError("calibration 不允许使用 hidden 集")
        evaluations = self.db.all(
            "SELECT id,name,created_at FROM evaluations WHERE split=? AND status='done' "
            "ORDER BY created_at DESC LIMIT 4", (split,))
        samples = self.db.all(
            "SELECT predicted_json,metrics_json FROM evaluation_cases WHERE evaluation_id=?",
            (evaluations[0]["id"],)) if evaluations else []
        buckets: dict[str, dict[str, int]] = {}
        total = 0
        for row in samples:
            try:
                predicted = json.loads(row.get("predicted_json") or "{}")
                metrics = json.loads(row.get("metrics_json") or "{}")
            except json.JSONDecodeError:
                continue
            level = str(predicted.get("confidence") or "unknown")
            if level not in ("low", "medium", "high"):
                level = "unknown"
            bucket = buckets.setdefault(level, {"total": 0, "correct": 0})
            bucket["total"] += 1
            bucket["correct"] += 1 if metrics.get("exact") else 0
            total += 1
        status = "calibrated" if total >= self._min_samples() else "uncalibrated"
        payload = {
            "version": "bbu-calibration/1",
            "status": status,
            "split": split,
            "sample_count": total,
            "min_samples": self._min_samples(),
            "source_evaluation": (evaluations[0] if evaluations else None),
            "levels": {
                level: {
                    "total": bucket["total"], "correct": bucket["correct"],
                    "accuracy": round(bucket["correct"] / bucket["total"], 3)
                    if bucket["total"] else None,
                    "accuracy_ci95": wilson_interval(bucket["correct"], bucket["total"]),
                }
                for level, bucket in sorted(buckets.items())
            },
            "limitations": [
                "只在 dev/calibration 集拟合；hidden/test 不参与。",
                "三档置信度不是概率；样本不足显示 uncalibrated。",
                "分设备/版本的具体准确率需要更大样本，当前不拆分。",
            ],
            "created_at": iso_now(),
        }
        self.db.execute(
            "INSERT INTO calibration_snapshots(split,sample_count,payload_json,created_at) "
            "VALUES(?,?,?,?)", (split, total, json.dumps(payload, ensure_ascii=False),
                                payload["created_at"]))
        return payload

    def latest(self) -> dict[str, Any]:
        row = self.db.one(
            "SELECT payload_json,created_at FROM calibration_snapshots ORDER BY id DESC LIMIT 1")
        if not row:
            return {
                "version": "bbu-calibration/1", "status": "uncalibrated",
                "sample_count": 0, "min_samples": self._min_samples(), "split": "dev",
                "levels": {}, "limitations": ["尚未执行 dev 校准"],
            }
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {"status": "uncalibrated", "sample_count": 0, "levels": {}}
        payload["cached_at"] = row.get("created_at")
        return payload

    def annotate(self, confidence: str) -> dict[str, Any]:
        snapshot = self.latest()
        level = str(confidence or "medium")
        item = (snapshot.get("levels") or {}).get(level)
        calibrated = bool(item and item.get("total") and snapshot.get("status") == "calibrated")
        return {
            "status": "calibrated" if calibrated else "uncalibrated",
            "level": level,
            "sample_count": item.get("total", 0) if item else 0,
            "accuracy": item.get("accuracy") if item else None,
            "accuracy_ci95": item.get("accuracy_ci95") if item else None,
            "source": snapshot.get("source_evaluation") or {},
            "min_samples": snapshot.get("min_samples"),
            "note": ("展示三档置信度与 dev 经验区间，不展示伪精确概率"
                     if calibrated else "样本不足或当前档位未校准，仅为保守三档判断"),
        }
