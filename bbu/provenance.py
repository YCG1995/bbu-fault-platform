"""FaultSpec 字段级来源：JSON Pointer、原文区间、quote hash、锁和审计。

来源是证据链的一部分：没有来源的阈值、动作和根因条件不得当作已验证字段。
用户手工锁定的字段在模型修复/局部 Patch 中必须保持不变。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _quote_for(value: Any) -> str:
    if isinstance(value, str):
        return value[:180]
    if isinstance(value, list):
        return str(value[0])[:180] if value else ""
    return ""


def _pointer(base: str, key: str, index: int | None = None) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{base}/{token}" + (f"/{index}" if index is not None else "")


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """返回折叠空白后的文本及每个字符对应原文位置。"""
    out: list[str] = []
    positions: list[int] = []
    pending_space = False
    for idx, char in enumerate(str(text or "")):
        if char.isspace():
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(" ")
            positions.append(idx)
            pending_space = False
        out.append(char)
        positions.append(idx)
    return "".join(out), positions


def _locate(text: str, quote: str) -> tuple[int, int, bool]:
    """先精确匹配，再按规范化空白匹配；返回原文起止位置。"""
    quote = _norm(quote)
    if not quote:
        return -1, -1, False
    exact = str(text or "").find(quote)
    if exact >= 0:
        return exact, exact + len(quote), True
    normalized, positions = _norm_with_map(text)
    pos = normalized.find(quote)
    if pos < 0 or not positions:
        return -1, -1, False
    end_pos = min(len(positions) - 1, pos + len(quote) - 1)
    return positions[pos], positions[end_pos] + 1, True


def _line_for(text: str, anchor: str, limit: int = 220) -> tuple[str, int, int]:
    anchor = str(anchor or "").strip()
    if not anchor or not text:
        return "", -1, -1
    candidates = [anchor]
    if " " in anchor:
        candidates.extend([p for p in re.split(r"[\s/，。；:：]+", anchor) if len(p) >= 4])
    for token in sorted(candidates, key=len, reverse=True)[:8]:
        for line in str(text).splitlines():
            if token and token in line:
                value = line.strip()[:limit]
                pos = text.find(value)
                return value, pos, pos + len(value) if pos >= 0 else -1
    return "", -1, -1


def _record(spec: dict[str, Any], path: str, value: Any, quote: str, text: str,
            revision: str, *, hard: bool, origin: str,
            derivation: str = "", derived_from: list[str] | None = None,
            located: tuple[int, int, bool] | None = None) -> None:
    quote = _norm(quote)[:220]
    if quote:
        start, end, found = located or _locate(text, quote)
    else:
        start, end, found = -1, -1, False
    status = "derived" if origin == "derived" else (
        "verified" if found else ("missing_source" if not quote else "unverified"))
    item = {
        "path": path, "quote": quote, "quote_hash": _hash(quote) if quote else "",
        "char_start": start, "char_end": end,
        "normalization": "strip_ws" if found else "",
        "source_revision": revision, "hard": bool(hard), "origin": origin,
        "validation_status": status,
        "locked": bool((spec.get("provenance") or {}).get(path, {}).get("locked", False)),
        "value_hash": _hash(str(value)) if value is not None else "",
        "derivation": derivation[:240],
        "derived_from": list(derived_from or [])[:8],
    }
    spec.setdefault("provenance", {})[path] = item


def _derive_quote(spec: dict[str, Any], paths: list[str], fallback: str = "") -> tuple[str, list[str]]:
    for path in paths:
        info = (spec.get("provenance") or {}).get(path) or {}
        if info.get("quote"):
            return str(info["quote"]), [path]
    if fallback:
        return _norm(fallback)[:220], []
    return "", []


def _rule_paths(spec: dict[str, Any], condition: str) -> list[str]:
    refs = [int(x) for x in re.findall(r"E(\d+)", str(condition or ""))]
    paths = []
    for number in refs:
        idx = number - 1
        if 0 <= idx < len(spec.get("evidence_rules") or []):
            paths.append(f"/evidence_rules/{idx}/source_excerpt")
    return paths


def attach_provenance(spec: dict[str, Any], text: str, revision: str = "") -> dict[str, Any]:
    text = str(text or "")
    revision = revision or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    existing = spec.get("provenance") or {}
    spec["provenance"] = {}
    spec["source_revision"] = revision

    def source_field(path: str, value: Any, hard: bool, origin: str = "source",
                     derivation: str = "") -> None:
        quote = _quote_for(value)
        start, end, found = _locate(text, quote)
        if not found and quote:
            line, start, end = _line_for(text, quote)
            if line:
                quote = line
                found = True
                origin = "derived"
                derivation = derivation or "line_anchor"
        _record(spec, path, value, quote, text, revision, hard=hard, origin=origin,
                derivation=derivation, located=(start, end, found))

    source_field("/fault_name", spec.get("fault_name"), True)
    source_field("/summary", spec.get("summary"), False)
    domain = spec.get("domain") or {}
    source_field("/domain/version_scope", domain.get("version_scope"), True)

    for i, rule in enumerate(spec.get("evidence_rules") or []):
        base = f"/evidence_rules/{i}"
        desired = rule.get("source_excerpt") or ""
        source_path = f"{base}/source_excerpt"
        quote = _quote_for(desired)
        start, end, found = _locate(text, quote)
        if not found and quote:
            line, start, end = _line_for(text, quote or _quote_for(rule.get("any_of")))
            if line:
                quote = line
                found = True
                _record(spec, source_path, desired, quote, text, revision, hard=True,
                        origin="derived", derivation="line_anchor",
                        located=(start, end, found))
            else:
                _record(spec, source_path, desired, quote, text, revision, hard=True,
                        origin="rule")
        else:
            _record(spec, source_path, desired, quote, text, revision, hard=True,
                    origin="rule", located=(start, end, found))
        rule_quote = (spec["provenance"].get(source_path) or {}).get("quote") or quote
        _record(spec, f"{base}/any_of", rule.get("any_of"), rule_quote, text, revision,
                hard=True, origin="rule",
                derivation="rule source_excerpt",
                derived_from=[source_path])
        for j, pattern in enumerate(rule.get("any_of") or []):
            pattern_text = str(pattern)
            start, end, found = _locate(text, pattern_text)
            if found:
                _record(spec, f"{base}/any_of/{j}", pattern, pattern_text, text, revision,
                        hard=True, origin="rule", located=(start, end, found))
            else:
                _record(spec, f"{base}/any_of/{j}", pattern, rule_quote, text, revision,
                        hard=True, origin="derived", derivation="regex/source_excerpt",
                        derived_from=[source_path])

    for i, root in enumerate(spec.get("root_causes") or []):
        base = f"/root_causes/{i}"
        name_path = f"{base}/name"
        source_field(name_path, root.get("name"), True, origin="root")
        condition_paths = _rule_paths(spec, root.get("condition"))
        name_info = spec["provenance"].get(name_path) or {}
        if name_info.get("validation_status") not in ("verified", "derived"):
            cond_quote, cond_from = _derive_quote(spec, condition_paths)
            if cond_quote:
                _record(spec, name_path, root.get("name"), cond_quote, text, revision,
                        hard=True, origin="derived",
                        derivation="root name inferred from condition/evidence rule",
                        derived_from=cond_from)
        name_quote = (spec["provenance"].get(name_path) or {}).get("quote") or ""
        cond_quote, cond_from = _derive_quote(spec, condition_paths, name_quote)
        if cond_quote:
            _record(spec, f"{base}/condition", root.get("condition"), cond_quote, text,
                    revision, hard=True, origin="derived",
                    derivation="condition references evidence rules",
                    derived_from=cond_from)
        else:
            _record(spec, f"{base}/condition", root.get("condition"), "", text, revision,
                    hard=True, origin="root")
        action_base = _derive_quote(spec, condition_paths, name_quote)
        for j, action in enumerate(root.get("actions") or []):
            quote = _quote_for(action)
            start, end, found = _locate(text, quote)
            if found:
                _record(spec, f"{base}/actions/{j}", action, quote, text, revision,
                        hard=True, origin="root", located=(start, end, found))
            else:
                _record(spec, f"{base}/actions/{j}", action, action_base[0], text, revision,
                        hard=True, origin="derived", derivation="root/evidence rule context",
                        derived_from=action_base[1])
        mech_path = f"{base}/mechanism"
        source_field(mech_path, root.get("mechanism"), False, origin="root")
        mech_info = spec["provenance"].get(mech_path) or {}
        if mech_info.get("validation_status") not in ("verified", "derived"):
            mech_base = _derive_quote(spec, condition_paths, name_quote)
            if mech_base[0]:
                _record(spec, mech_path, root.get("mechanism"), mech_base[0], text,
                        revision, hard=False, origin="derived",
                        derivation="mechanism inferred from condition/root name",
                        derived_from=mech_base[1])

    for i, threshold in enumerate(spec.get("thresholds") or []):
        base = f"/thresholds/{i}"
        quote = threshold.get("source_excerpt") or threshold.get("metric") or ""
        value_path = f"{base}/value"
        quote_norm = _quote_for(quote)
        start, end, found = _locate(text, quote_norm)
        if found:
            _record(spec, value_path, threshold.get("value"), quote_norm, text, revision,
                    hard=True, origin="threshold", located=(start, end, found))
        elif quote_norm:
            line, start, end = _line_for(text, quote_norm)
            _record(spec, value_path, threshold.get("value"), line or quote_norm, text,
                    revision, hard=True, origin="derived",
                    derivation="threshold source line",
                    located=(start, end, bool(line)))
        else:
            _record(spec, value_path, threshold.get("value"), "", text, revision,
                    hard=True, origin="threshold")
        source_field(f"{base}/source_excerpt", quote, True, origin="threshold")

    # 保留既有人工锁；模型重写不能清锁。
    for path, item in existing.items():
        if item.get("locked") and path in spec["provenance"]:
            spec["provenance"][path]["locked"] = True
    spec["hard_fields"] = [p for p, item in spec["provenance"].items() if item.get("hard")]
    return spec["provenance"]


def _pointer_parts(path: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~")
            for p in str(path or "").split("/") if p]


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in _pointer_parts(path):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def set_path(obj: Any, path: str, value: Any) -> bool:
    parts = _pointer_parts(path)
    if not parts:
        return False
    cur = obj
    for part in parts[:-1]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    last = parts[-1]
    if isinstance(cur, list):
        try:
            cur[int(last)] = value
        except (ValueError, IndexError):
            return False
        return True
    if isinstance(cur, dict) and last in cur:
        cur[last] = value
        return True
    return False


def locked_values(spec: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for path, item in (spec.get("provenance") or {}).items():
        if item.get("locked"):
            values[path] = get_path(spec, path)
    return values


def restore_locked(spec: dict[str, Any], values: dict[str, Any]) -> list[str]:
    restored = []
    for path, value in values.items():
        if set_path(spec, path, value):
            restored.append(path)
    return restored



def audit_provenance(spec: dict[str, Any]) -> dict[str, Any]:
    provenance = spec.get("provenance") or {}
    hard = [p for p in (spec.get("hard_fields") or []) if p in provenance]
    missing = [p for p in hard if provenance[p].get("validation_status") not in
               ("verified", "derived")]
    return {
        "version": "bbu-provenance/1",
        "source_revision": spec.get("source_revision") or "",
        "hard_fields": len(hard), "recorded": len(provenance),
        "verified": len(hard) - len(missing), "missing": missing[:20],
        "coverage": round((len(hard) - len(missing)) / max(1, len(hard)), 3),
        "locked": [p for p, x in provenance.items() if x.get("locked")],
        "ok": not missing,
    }


def lock_fields(spec: dict[str, Any], paths: list[str], locked: bool = True) -> dict[str, Any]:
    provenance = spec.setdefault("provenance", {})
    for path in paths:
        if path in provenance:
            provenance[path]["locked"] = bool(locked)
    spec["hard_fields"] = [p for p, x in provenance.items() if x.get("hard")]
    return audit_provenance(spec)
