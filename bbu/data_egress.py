"""模型数据出域策略：最小摘录、敏感值脱敏与无内容审计。

原始日志默认不整体发送给模型。工坊只发送经筛选、脱敏的必要来源摘录和
确定性 FaultSpec；诊断只发送任务描述与有限证据视图。所有审计字段仅包含
长度、哈希、策略和计数，不保存发送正文。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

POLICY_VERSION = "1"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*:\s*)(bearer\s+)?[^\s,;]+"), r"\1\2<REDACTED>"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|private[_-]?key)"
                 r"\s*[:=]\s*)([^\s,;]+)"), r"\1<REDACTED>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 <REDACTED>"),
    (re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,})\b"),
     "<REDACTED_JWT>"),
    (re.compile(r"(?i)\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b"), "<REDACTED_EMAIL>"),
    (re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"), "<REDACTED_PHONE>"),
)
_PEM_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_EXCERPT_KEYWORDS = (
    "故障", "现象", "日志", "告警", "关键字", "阈值", "根因", "原因", "处理", "恢复",
    "版本", "设备", "单板", "模块", "时钟", "电源", "链路", "温度", "电压", "电流",
    "error", "fail", "alarm", "warn", "threshold", "root cause", "action", "verify",
)
_MAX_LINE = 600


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True)
class DataEgressPolicy:
    """按用途固定的模型出域策略，创建后不可变。"""

    purpose: str
    mode: str
    max_chars: int

    @classmethod
    def for_workshop(cls, config: Any) -> "DataEgressPolicy":
        mode = str(config.get("workshop_source_mode", "excerpt") or "excerpt").strip().lower()
        if mode not in ("none", "excerpt", "full"):
            mode = "excerpt"
        return cls("workshop", mode, _bounded_int(config.get("workshop_egress_max_chars", 12000), 12000))

    @classmethod
    def for_diagnostic(cls, config: Any) -> "DataEgressPolicy":
        return cls("diagnostic", "evidence_only",
                   _bounded_int(config.get("diagnostic_egress_max_chars", 8000), 8000))

    def describe(self) -> dict[str, Any]:
        return {"purpose": self.purpose, "mode": self.mode, "max_chars": self.max_chars,
                "policy_version": POLICY_VERSION, "raw_source_allowed": self.mode == "full"}

    def redact(self, text: Any) -> tuple[str, int]:
        """移除常见凭证和个人标识；保留告警码、寄存器、阈值和时序数字。"""
        value = str(text or "").replace("\x00", "")
        count = 0
        value, n = _PEM_RE.subn("<REDACTED_PRIVATE_KEY>", value)
        count += n
        for pattern, replacement in _SECRET_PATTERNS:
            value, n = pattern.subn(replacement, value)
            count += n
        return value, count

    def sanitize_text(self, text: Any, *, limit: int | None = None) -> str:
        value, _ = self.redact(text)
        value = re.sub(r"\r\n?", "\n", value).strip()
        cap = max(1, int(limit or self.max_chars))
        return value[:cap]

    def sanitize_structure(self, value: Any) -> Any:
        """递归清理模型载荷中的字符串；dict/list 结构和数字保持不变。"""
        if isinstance(value, str):
            return self.sanitize_text(value)
        if isinstance(value, list):
            return [self.sanitize_structure(x) for x in value]
        if isinstance(value, dict):
            return {str(k): self.sanitize_structure(v) for k, v in value.items()}
        return value

    def excerpt(self, text: Any) -> tuple[str, dict[str, Any]]:
        """生成工坊使用的必要行摘录；不截断告警码、阈值和关键字所在行。"""
        original = str(text or "").replace("\x00", "")
        if self.mode == "none":
            return "", self._audit(original, "", lines_kept=0, lines_total=0)
        if self.mode == "full":
            sent = self.sanitize_text(original)
            return sent, self._audit(original, sent, lines_kept=len(sent.splitlines()),
                                     lines_total=len(original.splitlines()),
                                     explicit_full=True)

        lines = original.splitlines()
        selected: list[tuple[int, str, int]] = []
        seen: set[str] = set()
        keywords = [k.lower() for k in _EXCERPT_KEYWORDS]
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            # 首尾行通常包含标题、结论或补充说明；关键字行优先占用字符预算。
            edge = idx < 12 or idx >= max(0, len(lines) - 8)
            relevant = any(k in stripped.lower() for k in keywords)
            if not edge and not relevant:
                continue
            redacted, _ = self.redact(stripped[:_MAX_LINE])
            selected.append((idx, redacted, 0 if relevant else 1))
            seen.add(stripped)
        chosen: list[tuple[int, str]] = []
        used = 0
        for priority in (0, 1):
            for idx, line, rank in selected:
                if rank != priority:
                    continue
                cost = len(line) + (1 if chosen else 0)
                if used + cost > self.max_chars:
                    continue
                chosen.append((idx, line))
                used += cost
        chosen.sort(key=lambda x: x[0])
        sent = "\n".join(line for _, line in chosen)[: self.max_chars]
        return sent, self._audit(original, sent, lines_kept=len(chosen),
                                 lines_total=len(original.splitlines()))

    def signal_audit(self, payload: Any) -> dict[str, Any]:
        """记录模型载荷长度和哈希，不保存内容本身。"""
        try:
            canonical = json.dumps(self.sanitize_structure(payload), ensure_ascii=False,
                                   sort_keys=True, default=str)
        except (TypeError, ValueError):
            canonical = str(payload)
        return {"purpose": self.purpose, "mode": self.mode,
                "payload_chars": len(canonical), "payload_sha256": _sha256(canonical),
                "policy_version": POLICY_VERSION}

    def payload_audit(self, original: Any, sanitized: Any) -> dict[str, Any]:
        """对比清理前后的载荷，只保留长度、哈希与抑制量。"""
        try:
            before = json.dumps(original, ensure_ascii=False, sort_keys=True, default=str)
            after = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            before, after = str(original), str(sanitized)
        return {
            "purpose": self.purpose,
            "mode": self.mode,
            "policy_version": POLICY_VERSION,
            "source_chars": len(before),
            "sent_chars": len(after),
            "suppressed_chars": max(0, len(before) - len(after)),
            "source_sha256": _sha256(before),
            "sent_sha256": _sha256(after),
        }

    def _audit(self, original: str, sent: str, *, lines_kept: int,
               lines_total: int, explicit_full: bool = False) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "mode": self.mode,
            "policy_version": POLICY_VERSION,
            "raw_source_allowed": self.mode == "full" or explicit_full,
            "source_chars": len(original),
            "sent_chars": len(sent),
            "suppressed_chars": max(0, len(original) - len(sent)),
            "lines_total": lines_total,
            "lines_kept": lines_kept,
            "source_sha256": _sha256(original),
            "sent_sha256": _sha256(sent),
        }


def _bounded_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(256, min(100000, parsed))
