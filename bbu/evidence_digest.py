"""工坊日志证据摘要：把大包压成可审计、可复现的确定性上下文。"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_SEVERITY = re.compile(
    r"(?i)\b(ERROR|FATAL|CRITICAL|FAIL(?:ED|URE)?|Exception|timeout|timed out|"
    r"reset|restart|link down|alarm|unlocked|loss of signal)\b|失锁|驻波|复位|掉电|链路断")
_TS = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")
_SECRET = re.compile(r"(?i)(sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._-]{8,}|"
                     r"(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+)")
_ABS_PATH = re.compile(r"/(?:home|Users|root|opt|var|tmp)/[^\s:]+")
_CLASSES = [
    ("clock", re.compile(r"(?i)clock|pll|ocxo|gnss|1588|tod|时间|时钟|失锁")),
    ("alarm", re.compile(r"(?i)alarm|fm-|alert|告警")),
    ("reset", re.compile(r"(?i)reset|reboot|restart|watchdog|复位|重启")),
    ("power", re.compile(r"(?i)power|voltage|vswr|驻波|电源|电压")),
    ("link", re.compile(r"(?i)link|optical|fiber|eth|rx|光模块|链路")),
    ("upgrade", re.compile(r"(?i)upgrade|firmware|package|升级|版本")),
]


def _classify(path: str) -> str:
    for name, pattern in _CLASSES:
        if pattern.search(path):
            return name
    return "other"


def _redact(text: str) -> str:
    text = _SECRET.sub("<redacted>", text)
    return _ABS_PATH.sub("<path>", text)


def _safe_read(path: Path, limit: int = 1_000_000) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            return ""
        return raw.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def build_digest(root: Path, *, max_chars: int = 12000, max_lines: int = 4000,
                 candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    classes: Counter[str] = Counter()
    total_bytes = 0
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        text = _safe_read(path)
        if not text:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        cls = _classify(rel)
        classes[cls] += 1
        total_bytes += path.stat().st_size
        files.append({"file": rel, "class": cls, "text": text, "bytes": path.stat().st_size})
    anomalies: list[dict[str, Any]] = []
    timeline: Counter[str] = Counter()
    for item in files:
        lines = item["text"].splitlines()
        for no, line in enumerate(lines, 1):
            if not _SEVERITY.search(line):
                continue
            stripped = _redact(line.strip())[:400]
            signature = re.sub(r"\d+", "<n>", stripped.lower())[:120]
            anomalies.append({"file": item["file"], "line": no, "severity": "high",
                              "text": stripped, "signature": signature,
                              "class": item["class"]})
            ts = _TS.search(line)
            if ts:
                timeline[f"{ts.group(1)}T{ts.group(2)}"] += 1
    sig_files: dict[str, set[str]] = defaultdict(set)
    for a in anomalies:
        sig_files[a["signature"]].add(a["file"])
    for a in anomalies:
        a["score"] = 3.0 + min(2.0, len(sig_files[a["signature"]]) - 1) * 0.5
        if a["class"] in ("clock", "alarm"):
            a["score"] += 0.3
    anomalies.sort(key=lambda x: (-x["score"], x["file"], x["line"]))
    peak = []
    if timeline:
        ordered = sorted(timeline.items())
        top = sorted(ordered, key=lambda x: -x[1])[:1]
        if top:
            idx = ordered.index(top[0])
            for i in range(max(0, idx - 1), min(len(ordered), idx + 2)):
                peak.append({"minute": ordered[i][0], "count": ordered[i][1]})
    samples = []
    seen_class: Counter[str] = Counter()
    for a in anomalies:
        if seen_class[a["class"]] >= 2:
            continue
        seen_class[a["class"]] += 1
        lines = files_text = next(x["text"] for x in files if x["file"] == a["file"])
        snippet = "\n".join(_redact(x)[:200] for x in lines.splitlines()
                            [max(0, a["line"] - 2):a["line"] + 1])
        samples.append({"file": a["file"], "reason": "high_severity",
                        "lines": snippet, "class": a["class"]})
        if len(samples) >= 8:
            break
    selected_candidates = []
    candidate_list = candidates if isinstance(candidates, list) else (
        (candidates or {}).get("rule_suggestions") if isinstance(candidates, dict) else [])
    for c in (candidate_list or [])[:12]:
        if not isinstance(c, dict):
            continue
        item = {
            "rule_id": c.get("rule_id") or c.get("id") or "",
            "pattern": str(c.get("pattern") or c.get("regex")
                           or " / ".join(str(x) for x in c.get("any_of") or []))[:160],
            "files": int(c.get("files") or c.get("file_count") or 0),
            "hits": int(c.get("hits") or c.get("hit_count") or 0),
        }
        if c.get("globs"):
            item["globs"] = [str(x) for x in c.get("globs") or []][:4]
        if c.get("any_of"):
            item["any_of"] = [str(x) for x in c.get("any_of") or []][:6]
        selected_candidates.append(item)
    budget = max(1000, int(max_chars))
    text_parts = [f"资料盘点：{len(files)} 个文本日志，{total_bytes} 字节；分类 " +
                  ", ".join(f"{k}={v}" for k, v in sorted(classes.items()))]
    if peak:
        text_parts.append("时间线峰值：" + ", ".join(f"{x['minute']}={x['count']}" for x in peak))
    if anomalies:
        text_parts.append("异常行（按分数）：")
        for a in anomalies[:40]:
            text_parts.append(f"- {a['file']}:{a['line']} {a['text'][:180]}")
    if samples:
        text_parts.append("代表样本：")
        for s in samples:
            text_parts.append(f"- {s['file']}\n{s['lines']}")
    if selected_candidates:
        text_parts.append("候选规则：")
        for c in selected_candidates:
            text_parts.append(f"- {c['rule_id']} {c['pattern']} (files={c['files']}, hits={c['hits']})")
    summary = "\n".join(text_parts)
    notes = []
    if len(summary) > budget:
        notes.append(f"摘要超预算，已从 {len(summary)} 字符截断到 {budget}")
        summary = summary[:budget] + "\n[摘要已截断]"
    if not files:
        notes.append("未找到可读文本日志")
    if len(anomalies) > max_lines:
        notes.append(f"异常行超过上限 {max_lines}，仅保留前 {max_lines} 行")
    return {
        "inventory": {"files": len(files), "classes": dict(classes), "bytes": total_bytes},
        "timeline": peak,
        "anomaly_lines": anomalies[:max_lines],
        "candidates": selected_candidates,
        "samples": samples,
        "coverage_notes": notes,
        "summary_text": summary,
    }
