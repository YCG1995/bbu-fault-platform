"""模型 endpoint 能力探测与缓存。

目的不是追求“最严格协议”，而是把网关差异显式化：
- 是否支持 response_format/json_object；
- 是否能稳定返回 usage；
- 上下文/最大输出提示；
- 探测失败时自动降级为“提示词约束 + 本地 Schema 校验”，业务校验绝不关闭。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .util import iso_now


def endpoint_key(model: dict[str, Any]) -> str:
    text = "|".join(str(model.get(k) or "") for k in ("provider", "base_url", "model"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class ModelCapabilityStore:
    def __init__(self, db: Any, config: Any, call_probe=None) -> None:
        self.db = db
        self.config = config
        self.call_probe = call_probe

    def mode(self) -> str:
        if self.config is None:
            return "auto"
        return str(self.config.get("model_schema_mode", "auto")).lower()

    def cached(self, model: dict[str, Any]) -> dict[str, Any] | None:
        key = endpoint_key(model)
        row = self.db.one("SELECT * FROM model_capabilities WHERE endpoint_key=?", (key,))
        if not row:
            return None
        if (row.get("expires_at") or "") <= iso_now():
            return None
        try:
            caps = json.loads(row.get("capabilities_json") or "{}")
        except json.JSONDecodeError:
            caps = {}
        caps.update({"cached": True, "checked_at": row.get("checked_at"),
                     "expires_at": row.get("expires_at"), "source": row.get("source")})
        return caps

    def save(self, model: dict[str, Any], caps: dict[str, Any], source: str = "probe") -> dict[str, Any]:
        hours = max(1, int(self.config.get("model_capability_cache_hours", 24)
                      if self.config is not None else 24))
        now = time.time()
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + hours * 3600))
        key = endpoint_key(model)
        payload = dict(caps)
        payload.pop("cached", None)
        self.db.execute(
            "INSERT INTO model_capabilities(endpoint_key,provider,base_url,model,"
            "capabilities_json,checked_at,expires_at,source) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(endpoint_key) DO UPDATE SET capabilities_json=excluded.capabilities_json,"
            "checked_at=excluded.checked_at,expires_at=excluded.expires_at,source=excluded.source",
            (key, str(model.get("provider") or ""), str(model.get("base_url") or ""),
             str(model.get("model") or ""), json.dumps(payload, ensure_ascii=False),
             iso_now(), expires, source))
        result = dict(payload)
        result.update({"cached": True, "checked_at": iso_now(), "expires_at": expires,
                       "source": source})
        return result

    def get(self, model: dict[str, Any], probe: bool = False) -> dict[str, Any]:
        provider = str(model.get("provider") or "")
        extra = model.get("extra") if isinstance(model.get("extra"), dict) else {}
        declared = extra.get("capabilities") if isinstance(extra.get("capabilities"), dict) else None
        if provider == "mock":
            return self.save(model, {"json_mode": True, "strict_schema": True,
                                     "usage": True, "context_window": 32768,
                                     "max_output": 4096, "degraded": False},
                             source="mock")
        mode = self.mode()
        if declared:
            base = {"json_mode": bool(declared.get("json_mode", True)),
                    "strict_schema": bool(declared.get("strict_schema", True)),
                    "usage": bool(declared.get("usage", True)),
                    "context_window": int(declared.get("context_window") or 32768),
                    "max_output": int(declared.get("max_output") or 4096),
                    "degraded": bool(declared.get("degraded", False)),
                    "probe_error": ""}
            return self.save(model, base, source="declared")
        cached = self.cached(model)
        if cached and mode != "probe":
            return cached
        if not probe or self.call_probe is None:
            base = {"json_mode": True, "strict_schema": False, "usage": True,
                    "context_window": int(extra.get("context_window") or 32768),
                    "max_output": int(extra.get("max_output") or model.get("max_tokens") or 4096),
                    "degraded": True, "probe_error": "未探测，按支持 JSON Mode 的保守假设运行"}
            return self.save(model, base, source="assumed")
        try:
            caps = self.call_probe(model)
            return self.save(model, caps, source="probe")
        except Exception as exc:  # noqa: BLE001 - 探测失败要降级，不能阻断模型调用
            base = {"json_mode": False, "strict_schema": False, "usage": False,
                    "context_window": int(extra.get("context_window") or 32768),
                    "max_output": int(extra.get("max_output") or model.get("max_tokens") or 4096),
                    "degraded": True, "probe_error": str(exc)[:300]}
            return self.save(model, base, source="probe_failed")

    def public(self, model: dict[str, Any]) -> dict[str, Any]:
        return self.get(model, probe=True)
