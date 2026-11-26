"""外部 IM 消息适配器：先提供可插拔接口与本地 JSON 导入实现。

真实飞书/豆包接入时，在 `ExternalIMService.register()` 注册适配器即可，
不需要改悬浮窗或 MCP 工具协议；凭据与出域授权由具体适配器负责。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MAX_IMPORT_BYTES = 8 * 1024 * 1024


@dataclass
class ExternalMessage:
    id: str
    provider: str
    conversation: str
    sender: str
    content: str
    ts: str
    link: str = ""


class ExternalIMAdapter(Protocol):
    name: str

    def configured(self) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def search(self, query: str, limit: int = 10) -> list[ExternalMessage]: ...


class LocalJsonAdapter:
    """把 `data/external_messages.json` 当作外部 IM 导出文件读取。

    文件格式：数组，每项含 id/provider/conversation/sender/content/ts/link。
    只读、不执行外部网络请求，作为真实 IM 适配器接入前的兜底与联调入口。
    """

    name = "local-json"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def configured(self) -> bool:
        return self.path.is_file()

    def _load(self) -> list[ExternalMessage]:
        if not self.configured():
            return []
        try:
            if self.path.stat().st_size > MAX_IMPORT_BYTES:
                return []
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        items: list[ExternalMessage] = []
        for raw in value[:5000]:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            items.append(ExternalMessage(
                id=str(raw.get("id") or "")[:80],
                provider=str(raw.get("provider") or "external")[:32],
                conversation=str(raw.get("conversation") or "")[:200],
                sender=str(raw.get("sender") or "")[:120],
                content=content[:4000],
                ts=str(raw.get("ts") or "")[:40],
                link=str(raw.get("link") or "")[:300],
            ))
        return items

    def status(self) -> dict[str, Any]:
        exists = self.configured()
        return {
            "name": self.name,
            "configured": exists,
            "path": str(self.path) if exists else "",
            "mode": "本地 JSON 导入（只读）",
            "notice": ("已加载本地外部消息导出文件" if exists else
                       "未配置外部 IM；可放置 data/external_messages.json 联调，"
                       "或注册飞书/豆包适配器"),
        }

    def search(self, query: str, limit: int = 10) -> list[ExternalMessage]:
        q = str(query or "").strip().lower()
        rows = self._load()
        if q:
            rows = [r for r in rows
                    if q in r.content.lower() or q in r.sender.lower()
                    or q in r.conversation.lower() or q in r.provider.lower()]
        rows.sort(key=lambda r: r.ts, reverse=True)
        return rows[:max(1, min(30, int(limit or 10)))]


class ExternalIMService:
    """适配器注册表；当前内置 local-json，后续可追加 feishu/doubao。"""

    def __init__(self, data_dir: Path) -> None:
        self._lock = threading.RLock()
        self._adapters: list[ExternalIMAdapter] = [LocalJsonAdapter(Path(data_dir) / "external_messages.json")]

    def register(self, adapter: ExternalIMAdapter) -> None:
        with self._lock:
            self._adapters = [a for a in self._adapters if a.name != adapter.name]
            self._adapters.append(adapter)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a.status() for a in self._adapters]

    def configured(self) -> bool:
        with self._lock:
            return any(a.configured() for a in self._adapters)

    def search(self, query: str, limit: int = 10) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        with self._lock:
            adapters = list(self._adapters)
        for adapter in adapters:
            statuses.append(adapter.status())
            if not adapter.configured():
                continue
            for msg in adapter.search(query, limit):
                items.append({
                    "id": msg.id, "provider": msg.provider,
                    "conversation": msg.conversation, "sender": msg.sender,
                    "content": msg.content, "ts": msg.ts, "link": msg.link,
                    "source": adapter.name,
                })
        items.sort(key=lambda x: x.get("ts") or "", reverse=True)
        return items[:max(1, min(30, int(limit or 10)))], statuses
