"""Outbound HTTP safety for model discovery and inference.

The platform deliberately keeps zero third-party dependencies, so this module
uses only ``urllib`` + ``ipaddress``.  It is intentionally strict by default:
model endpoints are administrator-controlled, but an admin account can still be
phished into pointing the server at an internal address.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


DEFAULT_MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
_METADATA_HOSTS = {
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
    "metadata.google.internal",
}
_SENSITIVE_HEADERS = {
    "authorization", "x-api-key", "x-goog-api-key", "cookie", "proxy-authorization",
}


class OutboundPolicyError(ValueError):
    """A URL was rejected by the outbound address policy."""


class OutboundResponseTooLarge(ValueError):
    """The response exceeded the configured byte budget."""


class OutboundContentTypeError(ValueError):
    """The response was not JSON where JSON was required."""


@dataclass(frozen=True)
class OutboundPolicy:
    allow_private: bool = False
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = MAX_REDIRECTS
    # 私网放行白名单：CIDR（10.0.0.0/8）、精确主机（llm.corp）或后缀（*.corp）。
    # 为空且 allow_private=True 时维持“允许所有私网”；非空时只放行匹配目标。
    private_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "private_allowlist",
                           _normalize_allowlist(self.private_allowlist))


def _normalize_allowlist(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    normalized: list[str] = []
    for item in items:
        entry = str(item or "").strip().lower().rstrip(".")
        if entry and entry not in normalized:
            normalized.append(entry)
    return tuple(normalized)


def _host_pattern_match(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".corp"
        return host.endswith(suffix) and len(host) > len(suffix)
    return host == pattern


def _allowlist_match(host: str, ip: ipaddress._BaseAddress,
                     entries: tuple[str, ...]) -> bool:
    """Return True when a private target is explicitly allowlisted.

    An empty list is handled by the caller as "allow all private".  A non-empty
    list rejects every private target that neither the hostname nor the
    resolved address matches.
    """
    for entry in entries:
        if not entry:
            continue
        if entry.startswith(("http://", "https://")):
            continue
        if "/" in entry:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        try:
            if ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            pass
        if _host_pattern_match(host, entry):
            return True
    return False


def _is_metadata_ip(ip: ipaddress._BaseAddress) -> bool:
    # IPv4-mapped IPv6 addresses need unwrapping before classification.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    text = str(ip)
    if text in _METADATA_HOSTS:
        return True
    # Link-local is the metadata/link-local range; never allow it even when
    # private endpoints are explicitly enabled.
    return ip.is_link_local


def _is_forbidden(ip: ipaddress._BaseAddress, allow_private: bool,
                  host: str = "", allowlist: tuple[str, ...] = ()) -> bool:
    if _is_metadata_ip(ip):
        return True
    if ip.is_unspecified or ip.is_multicast:
        return True
    if ip.is_loopback or ip.is_private:
        if not allow_private:
            return True
        if not allowlist:
            return False
        return not _allowlist_match(host, ip, allowlist)
    if ip.is_reserved:
        return True
    return False


def validate_outbound_url(url: str, policy: OutboundPolicy | None = None) -> str:
    """Validate scheme/host/address before opening a connection.

    Raises ``ValueError`` for malformed input and ``OutboundPolicyError`` for a
    host that resolves to a forbidden address.  DNS is resolved here *and*
    again by urllib; the second resolution is still subject to the same host
    policy through the redirect handler, but a determined DNS-rebinding
    attacker remains a residual risk.  This is documented in the release notes.
    """
    policy = policy or OutboundPolicy()
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("地址必须是 http/https URL")
    if not parsed.hostname:
        raise ValueError("地址缺少主机名")
    if parsed.username or parsed.password:
        raise OutboundPolicyError("地址不允许包含用户名/密码")
    if parsed.fragment:
        raise OutboundPolicyError("地址不允许包含 fragment")
    host = parsed.hostname.strip().lower()
    if host in _METADATA_HOSTS:
        raise OutboundPolicyError("禁止访问云元数据地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析主机名: {host}") from exc
    if not infos:
        raise ValueError(f"无法解析主机名: {host}")
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise OutboundPolicyError(f"主机解析出非法地址: {address}") from exc
        if _is_forbidden(ip, policy.allow_private, host, policy.private_allowlist):
            reason = "云元数据/link-local 地址" if _is_metadata_ip(ip) else "私网/环回/保留地址"
            raise OutboundPolicyError(f"禁止访问{reason}: {host} -> {address}")
    return raw


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every hop and strip credentials when the host changes."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, policy: OutboundPolicy) -> None:
        self.policy = policy
        self.redirects = 0
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        self.redirects += 1
        if self.redirects > self.policy.max_redirects:
            raise urllib.error.HTTPError(
                newurl, 403, "too many redirects", headers, fp)
        validate_outbound_url(newurl, self.policy)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = (urlparse(req.full_url).hostname or "").lower()
        new_host = (urlparse(new.full_url).hostname or "").lower()
        if old_host != new_host:
            for mapping in (new.headers, new.unredirected_hdrs):
                for key in list(mapping.keys()):
                    if str(key).lower() in _SENSITIVE_HEADERS:
                        del mapping[key]
        return new


def open_url(req: urllib.request.Request, *, timeout: float,
             policy: OutboundPolicy | None = None):
    """Open an URL through the policy-aware opener."""
    policy = policy or OutboundPolicy()
    validate_outbound_url(req.full_url, policy)
    opener = urllib.request.build_opener(SafeRedirectHandler(policy))
    return opener.open(req, timeout=timeout)


def read_limited(resp: Any, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Read at most ``max_bytes``; reject rather than silently truncating."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise OutboundResponseTooLarge(
                f"响应超过上限 {max_bytes} 字节")
        chunks.append(chunk)
    return b"".join(chunks)


def iter_limited(resp: Any, max_bytes: int = DEFAULT_MAX_BYTES):
    """Yield response lines while enforcing a total byte budget."""
    total = 0
    for line in resp:
        total += len(line)
        if total > max_bytes:
            raise OutboundResponseTooLarge(
                f"流式响应超过上限 {max_bytes} 字节")
        yield line


def require_json_content_type(resp: Any) -> None:
    """Reject an obviously non-JSON content type.

    Some compatible gateways omit Content-Type; those are still parsed as JSON
    for backwards compatibility.  A declared non-JSON type is rejected.
    """
    ctype = str(resp.headers.get("Content-Type") or "").lower()
    if ctype and "json" not in ctype:
        raise OutboundContentTypeError(f"响应 Content-Type 不是 JSON: {ctype[:120]}")


def error_body(exc: urllib.error.HTTPError, max_bytes: int = 4096) -> str:
    try:
        raw = exc.read(max_bytes + 1) or b""
    except Exception:
        raw = b""
    text = raw.decode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        text += "…[truncated]"
    return text


def policy_from_config(config: Any, *, max_key: str = "model_request_max_bytes") -> OutboundPolicy:
    # 1.28.1 起默认允许私网/回环；元数据与 link-local 仍永久拒绝。
    allow = True
    max_bytes = DEFAULT_MAX_BYTES
    private_allowlist: Any = ()
    if config is not None:
        try:
            allow = bool(config.get("model_discovery_allow_private", True))
            max_bytes = max(1024, int(config.get(max_key, DEFAULT_MAX_BYTES)))
            private_allowlist = config.get("model_private_allowlist", "")
        except (TypeError, ValueError):
            pass
    return OutboundPolicy(allow_private=allow, max_bytes=max_bytes,
                          private_allowlist=private_allowlist)


def endpoint_target(url: str) -> tuple[str, str]:
    """Return the normalized host/path used for outbound audit records."""
    parsed = urlparse(str(url or ""))
    return (parsed.hostname or "").lower(), parsed.path or "/"
