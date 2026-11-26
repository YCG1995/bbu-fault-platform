"""平台文档知识库：Markdown 分块、轻量中文检索、资源读取。

悬浮助手与 MCP 资源共用同一份索引；不引入向量库或外部依赖。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DOC_FILES: tuple[tuple[str, str, str], ...] = (
    ("readme", "README.md", "平台总览、快速开始、目录结构与测试"),
    ("user-guide", "docs/使用手册.md", "日常使用：诊断、技能、工坊、账号与通知"),
    ("deploy-guide", "docs/部署手册.md", "Linux/Windows 部署、服务、备份与排障"),
    ("oneclick-deploy", "docs/一键部署说明.md", "离线一键包安装与 Ubuntu 16 兼容"),
    ("upgrade-rollback", "docs/升级与回滚.md", "网页升级、离线包、回滚与升级助手"),
    ("architecture", "docs/架构设计.md", "后端模块、任务编排、数据模型与安全边界"),
    ("windows", "docs/Windows运行说明.md", "Windows 启动、服务与升级入口"),
    ("upgrade-sudo", "docs/升级助手sudo故障修复.md", "sudo setuid/nosuid 与升级助手修复"),
    ("workshop", "docs/技能工坊AI生成优化建议.md", "技能工坊生成、评审与配方"),
    ("release-1.20.1", "docs/版本说明-1.20.1.md", "当前版本助手、MCP、真实模型评测与已知限制"),
    ("diagnostic-data", "docs/诊断推理数据说明.md", "推理数据、证据与案例字段"),
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
ASCII_RE = re.compile(r"[a-zA-Z0-9_.:/-]{2,}")
MAX_CHUNK = 1500
MAX_DOC_BYTES = 160_000
DOC_PRIORITY = {
    "user-guide": 3.5, "deploy-guide": 2.0, "oneclick-deploy": 2.0,
    "upgrade-rollback": 2.5, "upgrade-sudo": 1.5, "architecture": 1.5,
    "windows": 1.5, "workshop": 1.5, "diagnostic-data": 1.0,
    "readme": 0.5, "release-1.20.1": 0.2,
}


@dataclass(frozen=True)
class DocChunk:
    slug: str
    doc_title: str
    heading: str
    text: str
    order: int


@dataclass(frozen=True)
class DocEntry:
    slug: str
    title: str
    description: str
    path: str


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_long(text: str, limit: int = MAX_CHUNK) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", limit // 2, limit)
        if cut <= 0:
            cut = rest.rfind("。", limit // 2, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def _query_terms(query: str) -> list[str]:
    q = str(query or "").lower()
    terms: set[str] = set()
    for word in ASCII_RE.findall(q):
        if word not in ("the", "and", "for", "with", "this", "that"):
            terms.add(word)
    for run in CHINESE_RE.findall(q):
        terms.add(run)
        if len(run) <= 24:
            for i in range(len(run) - 1):
                terms.add(run[i:i + 2])
    return sorted(terms, key=len, reverse=True)[:24]


def _excerpt(text: str, query: str, limit: int = 420) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[#*>`_|-]{2,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    q = str(query or "").strip()
    if q:
        idx = text.lower().find(q.lower())
        if idx >= 0:
            start = max(0, idx - 120)
            end = min(len(text), idx + limit - 80)
            return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return text[:limit].strip() + "…"


class KnowledgeBase:
    """按 Markdown 标题分块；搜索只在本机内存完成。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._chunks: list[DocChunk] | None = None
        self._docs: list[DocEntry] | None = None
        self._raw: dict[str, tuple[DocEntry, str]] = {}

    def _load(self) -> None:
        if self._chunks is not None and self._docs is not None:
            return
        with self._lock:
            if self._chunks is not None and self._docs is not None:
                return
            chunks: list[DocChunk] = []
            docs: list[DocEntry] = []
            raw: dict[str, tuple[DocEntry, str]] = {}
            for slug, relative, description in DOC_FILES:
                path = (self.root / relative).resolve()
                try:
                    path.relative_to(self.root.resolve())
                except ValueError:
                    continue
                if not path.is_file() or path.stat().st_size > MAX_DOC_BYTES:
                    continue
                try:
                    text = _clean(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                title = path.stem
                for line in text.splitlines():
                    match = HEADING_RE.match(line)
                    if match:
                        title = match.group(2).strip()
                        break
                entry = DocEntry(slug=slug, title=title, description=description,
                                 path=relative)
                docs.append(entry)
                raw[slug] = (entry, text)
                heading_stack: list[str] = []
                current_lines: list[str] = []
                current_heading = title
                order = 0

                def flush() -> None:
                    nonlocal order
                    body = "\n".join(current_lines).strip()
                    if not body:
                        return
                    for part in _split_long(body):
                        chunks.append(DocChunk(slug=slug, doc_title=title,
                                               heading=current_heading, text=part,
                                               order=order))
                        order += 1

                for line in text.splitlines():
                    match = HEADING_RE.match(line)
                    if match:
                        flush()
                        current_lines = []
                        level = len(match.group(1))
                        heading_stack = heading_stack[:level - 1] + [match.group(2).strip()]
                        current_heading = " / ".join(h for h in heading_stack if h)
                    else:
                        current_lines.append(line)
                flush()
            self._chunks = chunks
            self._docs = docs
            self._raw = raw

    def list_docs(self) -> list[dict[str, Any]]:
        self._load()
        assert self._docs is not None
        return [{"slug": e.slug, "title": e.title, "description": e.description,
                 "uri": f"platform://docs/{e.slug}", "path": e.path}
                for e in self._docs]

    def _entry(self, slug: str) -> DocEntry | None:
        self._load()
        assert self._docs is not None
        return next((d for d in self._docs if d.slug == slug), None)

    def read(self, slug: str, max_chars: int = 50000) -> dict[str, Any] | None:
        self._load()
        item = self._raw.get(str(slug or "").strip())
        if not item:
            return None
        entry, text = item
        limit = max(1000, min(80000, int(max_chars or 50000)))
        clipped = len(text) > limit
        return {"slug": entry.slug, "title": entry.title, "path": entry.path,
                "text": text[:limit], "truncated": clipped,
                "total_chars": len(text)}

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        self._load()
        assert self._chunks is not None
        q = str(query or "").strip().lower()
        if not q:
            return []
        terms = _query_terms(q)
        scored: list[tuple[float, DocChunk]] = []
        for chunk in self._chunks:
            body = chunk.text.lower()
            heading = chunk.heading.lower()
            title = chunk.doc_title.lower()
            score = 0.0
            if q in heading:
                score += 12
            elif q in body:
                score += 8
            elif q in title:
                score += 6
            for term in terms:
                if term in heading:
                    score += 5
                elif term in body:
                    score += min(4, body.count(term))
                elif term in title:
                    score += 3
            if score <= 0:
                continue
            # 标题块比正文块更有导航价值，轻微加权。
            if chunk.heading == chunk.doc_title:
                score += 1.5
            # 版本历史/变更记录通常不是操作说明，避免压过使用手册。
            if re.search(r"版本|历史|本轮变化|更新记录|变更|功能更新", chunk.heading):
                score -= 6
            score += DOC_PRIORITY.get(chunk.slug, 0)
            # 操作意图优先：问“新建诊断”时，“发起一次诊断”应压过助手/MCP 介绍。
            if any(k in query for k in ("新建诊断", "发起诊断", "创建诊断")):
                if "发起一次诊断" in chunk.heading:
                    score += 8
                if "平台助手" in chunk.heading:
                    score -= 14
            scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].slug, item[1].order))
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for score, chunk in scored:
            key = (chunk.slug, chunk.heading)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "slug": chunk.slug, "doc_title": chunk.doc_title,
                "heading": chunk.heading, "score": round(score, 1),
                "excerpt": _excerpt(chunk.text, q),
                "uri": f"platform://docs/{chunk.slug}",
            })
            if len(results) >= max(1, min(10, int(limit))):
                break
        return results

    def prompt_context(self, limit: int = 5) -> str:
        """给提示词用的文档目录，不含正文。"""
        docs = self.list_docs()
        return "\n".join(f"- {d['title']}（{d['slug']}）：{d['description']}"
                         for d in docs[:limit])
