"""技能导入供应链：quarantine、清单、签名和完整性复核。

外部 zip 先进入隔离区，按实际解压字节计数，拒绝路径穿越、符号链接/设备文件、
重复路径、压缩炸弹和超限包；通过后再原子替换安装目录并写入内容清单与 HMAC
签名。签名密钥是实例本地组织密钥，不等同于公钥 PKI；目标环境可替换为离线
密钥托管，但接口语义保持“签名绑定确切内容”。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import iso_now, sha256_file, write_json

MANIFEST_NAME = ".bbu-manifest.json"
SIGNATURE_NAME = ".bbu-signature.json"


@dataclass
class ImportLimits:
    max_files: int = 5000
    max_total_bytes: int = 512 * 1024 * 1024
    max_ratio: float = 100.0
    max_path: int = 240
    max_single_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_config(cls, config: Any) -> "ImportLimits":
        return cls(
            max_files=max(1, int(config.get("import_max_files", 5000))),
            max_total_bytes=max(1, int(config.get("import_max_total_mb", 512))) * 1024 * 1024,
            max_ratio=max(1.0, float(config.get("import_max_ratio", 100))),
            max_single_bytes=max(1, int(config.get("max_extract_mb", 8192))) * 1024 * 1024,
        )


class ImportRejected(ValueError):
    pass


@dataclass
class QuarantineResult:
    root: Path
    staging: Path
    name: str
    files: list[dict[str, Any]] = field(default_factory=list)
    total_bytes: int = 0
    content_sha256: str = ""
    limits: dict[str, Any] = field(default_factory=dict)


def _safe_member(name: str, limits: ImportLimits) -> str:
    raw = name.replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~"):
        raise ImportRejected("技能包包含绝对路径")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ImportRejected("技能包包含路径穿越")
    if len(raw) > limits.max_path:
        raise ImportRejected(f"技能包路径过长: {raw[:80]}")
    return "/".join(parts)


def _is_special(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return bool(mode and (stat.S_ISLNK(mode) or stat.S_ISCHR(mode)
                          or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode)
                          or stat.S_ISSOCK(mode)))


def quarantine_zip(zip_path: Path, work_root: Path, limits: ImportLimits) -> QuarantineResult:
    if not zipfile.is_zipfile(zip_path):
        raise ImportRejected("只支持 zip 格式的技能包")
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="skill-q-", dir=str(work_root)))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [x for x in zf.infolist() if not x.is_dir()]
            if not infos:
                raise ImportRejected("技能包为空")
            if len(infos) > limits.max_files:
                raise ImportRejected(f"技能包文件数 {len(infos)} 超过上限 {limits.max_files}")
            compressed = sum(max(0, x.compress_size) for x in infos) or 1
            declared = sum(max(0, x.file_size) for x in infos)
            if declared > limits.max_total_bytes:
                raise ImportRejected(f"声明解压大小 {declared} 超过上限 {limits.max_total_bytes}")
            if declared / compressed > limits.max_ratio:
                raise ImportRejected(f"压缩比 {declared / compressed:.1f} 超过上限 {limits.max_ratio}")
            seen: set[str] = set()
            extracted = 0
            files: list[dict[str, Any]] = []
            for info in infos:
                name = _safe_member(info.filename, limits)
                if _is_special(info):
                    raise ImportRejected(f"技能包包含特殊文件: {name}")
                if name in seen:
                    raise ImportRejected(f"技能包包含重复路径: {name}")
                seen.add(name)
                if info.flag_bits & 0x1:
                    raise ImportRejected("不支持加密 zip")
                target = (staging / name).resolve()
                if staging.resolve() not in target.parents:
                    raise ImportRejected("技能包路径越界")
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        extracted += len(chunk)
                        if written > limits.max_single_bytes:
                            raise ImportRejected(f"单文件超过上限: {name}")
                        if extracted > limits.max_total_bytes:
                            raise ImportRejected("实际解压大小超过上限，疑似压缩炸弹")
                        dst.write(chunk)
                files.append({"path": name, "size": written, "sha256": sha256_file(target)})
            skills = list(staging.rglob("SKILL.md"))
            if len(skills) != 1:
                raise ImportRejected("技能包必须且只能包含一个 SKILL.md")
            root = skills[0].parent
            content = _manifest_digest(files)
            return QuarantineResult(root=root, staging=staging, name=root.name,
                                    files=files, total_bytes=extracted,
                                    content_sha256=content,
                                    limits={"files": len(infos), "declared_bytes": declared,
                                            "actual_bytes": extracted,
                                            "ratio": round(declared / compressed, 3)})
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _manifest_digest(files: list[dict[str, Any]]) -> str:
    rows = [f"{f.get('path')}:{f.get('sha256')}:{f.get('size')}" for f in files]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def build_manifest(root: Path, *, name: str, version: str = "", builder: str = "import",
                   verification: dict[str, Any] | None = None) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for item in sorted(Path(root).rglob("*")):
        if not item.is_file() or item.name in (MANIFEST_NAME, SIGNATURE_NAME):
            continue
        if item.name.endswith((".pyc", ".pyo")):
            continue
        rel = item.relative_to(root).as_posix()
        files.append({"path": rel, "size": item.stat().st_size, "sha256": sha256_file(item)})
        if len(files) > 10000:
            break
    return {
        "manifest_version": "bbu-skill-manifest/1",
        "name": name,
        "version": version,
        "builder": builder,
        "created_at": iso_now(),
        "files": files,
        "content_sha256": _manifest_digest(files),
        "verification": dict(verification or {}),
    }


def key_path(data_dir: Path) -> Path:
    return Path(data_dir) / "skill-signing.key"


def _load_key(data_dir: Path) -> bytes:
    path = key_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def sign_payload(data_dir: Path | str, payload: dict[str, Any]) -> dict[str, str]:
    """对任意 JSON 治理对象签名，复用供应链签名密钥。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    key = _load_key(Path(data_dir))
    return {"algorithm": "HMAC-SHA256", "key_id": hashlib.sha256(key).hexdigest()[:12],
            "signature": hmac.new(key, body, hashlib.sha256).hexdigest(),
            "content_sha256": hashlib.sha256(body).hexdigest()}


def verify_payload(data_dir: Path | str, payload: dict[str, Any],
                   signature: dict[str, Any]) -> bool:
    expected = sign_payload(data_dir, payload)
    return (str(signature.get("algorithm") or "").lower() == "hmac-sha256"
            and hmac.compare_digest(expected["signature"],
                                    str(signature.get("signature") or "")))


def sign_skill(root: Path, *, data_dir: Path, name: str, version: str = "",
               builder: str = "import",
               verification: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = build_manifest(root, name=name, version=version, builder=builder,
                              verification=verification)
    write_json(root / MANIFEST_NAME, manifest)
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_load_key(data_dir), payload, hashlib.sha256).hexdigest()
    write_json(root / SIGNATURE_NAME, {
        "algorithm": "HMAC-SHA256", "key_id": hashlib.sha256(_load_key(data_dir)).hexdigest()[:12],
        "signature": signature, "content_sha256": manifest["content_sha256"],
        "signed_at": iso_now(),
    })
    return {"ok": True, "manifest": manifest, "signature": signature}


def verify_skill(root: Path, *, data_dir: Path) -> dict[str, Any]:
    manifest_path = Path(root) / MANIFEST_NAME
    sig_path = Path(root) / SIGNATURE_NAME
    if not manifest_path.exists():
        return {"ok": False, "status": "missing", "reason": "缺少内容清单"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        signature = json.loads(sig_path.read_text(encoding="utf-8")) if sig_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": "invalid", "reason": f"清单/签名不可读: {exc}"}
    if not signature:
        return {"ok": False, "status": "unsigned", "reason": "缺少签名"}
    files = manifest.get("files") or []
    if not isinstance(files, list):
        return {"ok": False, "status": "invalid", "reason": "清单 files 非法"}
    current = build_manifest(root, name=str(manifest.get("name") or ""),
                             version=str(manifest.get("version") or ""),
                             builder=str(manifest.get("builder") or ""))
    if current["content_sha256"] != manifest.get("content_sha256"):
        return {"ok": False, "status": "tampered", "reason": "文件内容与清单不一致",
                "expected": manifest.get("content_sha256"),
                "actual": current["content_sha256"]}
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    expected = hmac.new(_load_key(Path(data_dir)), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature.get("signature") or "")):
        return {"ok": False, "status": "bad_signature", "reason": "签名不匹配"}
    return {"ok": True, "status": "verified", "reason": "",
            "content_sha256": manifest.get("content_sha256"),
            "verification": manifest.get("verification") or {},
            "manifest": manifest, "signature": signature}


def install_from_quarantine(result: QuarantineResult, target: Path, *, data_dir: Path,
                            name: str, version: str = "") -> dict[str, Any]:
    if target.exists():
        raise FileExistsError(str(target))
    shutil.copytree(result.root, target)
    signed = sign_skill(target, data_dir=Path(data_dir), name=name, version=version,
                        builder="import")
    return signed
