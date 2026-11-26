"""证据质量评估：独立性、重复度、支撑范围与置信度降级。

这里不尝试把启发式分数包装成“概率”。输出是可解释的证据质量分与
high/medium/low 三档；真实校准必须由 evaluation.py 在带标签案例上计算。
"""
from __future__ import annotations

import fnmatch
import math
import re
from typing import Any

_TS_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),
    re.compile(r"\bt=\d+\b"),
    re.compile(r"\btick[=: ]+\d+\b", re.I),
    re.compile(r"\b\d{7,}\b"),
]


def _signature(text: str) -> str:
    value = (text or "").strip().lower()
    for pattern in _TS_PATTERNS:
        value = pattern.sub("<n>", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value[:320]


def _roots_for_rule(meta: dict[str, Any]) -> list[str]:
    return [str(x) for x in meta.get("roots") or []]


def _match_glob(path: str, pattern: str) -> bool:
    path = str(path or "").replace("\\", "/")
    pattern = str(pattern or "").replace("\\", "/")
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    if "/" not in pattern and fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern):
        return True
    return False


def source_coverage(result: dict[str, Any]) -> list[dict[str, Any]]:
    """O7：required_sources 逐类覆盖；只认明确 glob 命中的证据文件。"""
    sources = result.get("required_sources") or []
    evidence_files = [str(e.get("file") or "") for e in (result.get("evidence") or [])
                      if isinstance(e, dict)]
    fallback = set(str(x) for x in (result.get("glob_fallback") or []))
    rows = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        sid = str(src.get("id") or "")
        title = str(src.get("title") or sid)
        globs = [str(g) for g in (src.get("globs") or [])]
        matched = sorted({f for f in evidence_files
                          if f and f not in fallback
                          and any(_match_glob(f, g) for g in globs)})
        scanned = len(evidence_files)
        rows.append({
            "id": sid, "title": title, "globs": globs,
            "required": bool(src.get("required", True)),
            "scanned": scanned, "eligible": len(matched), "matched_files": matched[:10],
            "missing": not matched, "coverage": round(len(matched) / scanned, 3) if scanned else 0.0,
        })
    return rows


def assess_evidence(result: dict[str, Any]) -> dict[str, Any]:
    evidence = [e for e in (result.get("evidence") or []) if isinstance(e, dict)]
    meta = result.get("rule_meta") or {}
    root = result.get("root_cause") or {}
    root_id = str(root.get("id") or result.get("primary") or "")
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    other_clusters = 0
    for item in evidence:
        rid = str(item.get("rule_id") or "?")
        info = meta.get(rid) or {}
        sig = _signature(str(item.get("text") or ""))
        key = (rid, sig)
        if key not in clusters:
            clusters[key] = {
                "rule_id": rid, "text": str(item.get("text") or "")[:240],
                "file": str(item.get("file") or ""), "line_no": item.get("line_no"),
                "timestamp": item.get("timestamp"),
                "occurrences": 0, "files": set(),
                "description": info.get("description") or "",
                "weight": float(info.get("weight") or 10),
                "supports_root": (not root_id) or (root_id in _roots_for_rule(info)),
                "verified_rule": bool(info.get("verified", True)),
            }
        cluster = clusters[key]
        cluster["occurrences"] += 1
        if item.get("file"):
            cluster["files"].add(str(item.get("file")))
        if not cluster.get("timestamp") and item.get("timestamp"):
            cluster["timestamp"] = item.get("timestamp")
    supporting = [c for c in clusters.values() if c["supports_root"]]
    unrelated = [c for c in clusters.values() if not c["supports_root"]]
    other_clusters = len(unrelated)
    for c in clusters.values():
        c["files"] = sorted(c["files"])

    direct_clusters = [c for c in supporting if c["verified_rule"]]
    independent_files = sorted({f for c in supporting for f in c["files"]})
    distinct_rules = sorted({c["rule_id"] for c in supporting})
    total_occurrences = sum(c["occurrences"] for c in clusters.values())
    duplicate_occurrences = max(0, total_occurrences - len(clusters))

    strength = 0.0
    for c in direct_clusters:
        occurrence_factor = 1.0 + min(0.6, 0.18 * math.log2(1 + c["occurrences"]))
        file_factor = 1.0 + min(0.8, 0.2 * max(0, len(c["files"]) - 1))
        strength += c["weight"] * occurrence_factor * file_factor
    strength += min(12.0, 3.0 * max(0, len(independent_files) - 1))
    strength += min(8.0, 2.0 * max(0, len(distinct_rules) - 1))
    penalties: list[str] = []
    sources = source_coverage(result)
    missing_required = [s for s in sources if s["required"] and s["missing"]]
    if missing_required:
        penalties.append("关键来源缺失：" + "、".join(str(s["title"] or s["id"])
                                                      for s in missing_required))
    if result.get("glob_fallback"):
        strength -= min(15.0, 5.0 * len(result.get("glob_fallback") or []))
        penalties.append("存在 glob 兜底证据，文件名可能不属于目标网元")
    limitations = result.get("limitations") or []
    if any("截断" in str(x) for x in limitations):
        strength -= 5.0
        penalties.append("证据达到截断上限，可能漏掉站点或重复样本")
    if any("triage" in str(x) for x in limitations):
        strength -= 12.0
        penalties.append("规则来自 triage 草稿，未经真实案例验证")
    if any("增量缓存" in str(x) for x in limitations):
        strength -= 2.0
        penalties.append("使用增量缓存，未重新扫描全部输入")
    if any("不受支持" in str(x) for x in limitations):
        strength -= 5.0
        penalties.append("存在无法解析的压缩包格式")
    if other_clusters and not direct_clusters:
        penalties.append("命中证据不属于当前根因条件，不能作为直接依据")
    # 单条重复日志无论打印多少次都不能单独升级为高置信度。
    if len(direct_clusters) <= 1 and len(independent_files) <= 1:
        strength = min(strength, 44.0)
    quality_score = max(0, min(100, round(strength, 1)))

    static_conf = str(root.get("confidence") or "medium").lower()
    if quality_score >= 70 and len(direct_clusters) >= 2 and len(independent_files) >= 2 \
            and not any("glob 兜底" in p or "triage" in p for p in penalties):
        confidence = "high"
    elif quality_score >= 40 and direct_clusters:
        confidence = "medium"
    else:
        confidence = "low"
    if static_conf == "low" and confidence == "high":
        confidence = "medium"
    if static_conf == "medium" and confidence == "high" and len(distinct_rules) < 2:
        confidence = "medium"

    if missing_required:
        if len(missing_required) >= 2:
            confidence = "low"
        elif confidence == "high":
            confidence = "medium"
    reasons: list[str] = []
    if missing_required:
        reasons.append("关键来源缺失：" + "、".join(str(s["title"] or s["id"])
                                                    for s in missing_required))
    if not evidence:
        reasons.append("没有任何可定位证据")
    if duplicate_occurrences:
        reasons.append(f"{total_occurrences} 条证据去重后为 {len(clusters)} 组，重复 {duplicate_occurrences} 条")
    if len(independent_files) < 2 and direct_clusters:
        reasons.append("直接证据只来自一个文件，独立性不足")
    if other_clusters:
        reasons.append(f"{other_clusters} 组命中证据不支撑当前根因")
    reasons.extend(penalties)
    if quality_score >= 70:
        reasons.append("直接证据跨文件/规则形成交叉支撑" if len(direct_clusters) >= 2 else "证据质量较高")
    summary_clusters = []
    for c in supporting[:8]:
        summary_clusters.append({
            "rule_id": c["rule_id"], "description": c["description"],
            "file": c["file"], "line_no": c["line_no"], "timestamp": c["timestamp"],
            "occurrences": c["occurrences"], "files": len(c["files"]),
            "text": c["text"],
        })
    return {
        "quality_score": quality_score,
        "confidence": confidence,
        "static_confidence": static_conf,
        "raw_evidence": len(evidence),
        "unique_clusters": len(clusters),
        "supporting_clusters": len(supporting),
        "unrelated_clusters": other_clusters,
        "duplicate_occurrences": duplicate_occurrences,
        "direct_rules": distinct_rules,
        "independent_files": independent_files,
        "source_coverage": sources,
        "missing_required_sources": [s["id"] for s in missing_required],
        "reasons": reasons,
        "clusters": summary_clusters,
    }


def evidence_quality_line(result: dict[str, Any]) -> str:
    quality = result.get("evidence_quality") or {}
    if not quality:
        return ""
    return (f"证据质量 {quality.get('quality_score', 0)}/100，"
            f"{quality.get('unique_clusters', 0)} 组独立证据，"
            f"{len(quality.get('independent_files') or [])} 个来源文件，"
            f"置信度 {quality.get('confidence', 'low')}")
