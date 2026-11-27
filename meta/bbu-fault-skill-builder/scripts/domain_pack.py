#!/usr/bin/env python3
"""domain_pack.py — skill-domain-pack/1 的加载、校验与描述。

元技能内核只认这个接口；具体域（BBU/其它日志域）通过 domains/<id>/domain-pack.yaml 注入。
本脚本不导入任何 bbu.* 模块，也不解释域内业务含义。
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

SCHEMA = "skill-domain-pack/1"
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_ID = "bbu"


class DomainPackError(ValueError):
    """域包缺失、格式错误或版本不兼容。"""


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def domains_root(root: Path | None = None) -> Path:
    return (root or skill_root()) / "domains"


def _read(path: Path) -> dict:
    if not path.exists():
        raise DomainPackError(f"DOMAIN_PACK_NOT_FOUND: {path}")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainPackError(f"DOMAIN_PACK_INVALID: {path}: {exc}") from exc
    else:
        if not _HAS_YAML:
            raise DomainPackError(
                "DOMAIN_PACK_NEEDS_PYYAML: 读取 domain-pack.yaml 需要 PyYAML；"
                "可改用同名 .json 或安装 PyYAML")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise DomainPackError(f"DOMAIN_PACK_INVALID: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DomainPackError(f"DOMAIN_PACK_INVALID: {path} 顶层不是对象")
    return data


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value))
    if not parts:
        raise DomainPackError(f"DOMAIN_PACK_INVALID: 非法版本号 {value!r}")
    return tuple(int(x) for x in parts)


def version_satisfies(version: str, spec: str) -> bool:
    """支持 >=2.13,<3 这类逗号分隔的简单约束；不引入打包依赖。"""
    if not spec:
        return True
    current = _version_tuple(version)
    for clause in str(spec).split(","):
        clause = clause.strip()
        m = re.match(r"^(>=|<=|>|<|==)?\s*v?([0-9][0-9.]*)$", clause)
        if not m:
            raise DomainPackError(f"DOMAIN_PACK_INVALID: 无法解析版本约束 {clause!r}")
        op, target_s = m.group(1) or "==", m.group(2)
        target = _version_tuple(target_s)
        n = max(len(current), len(target))
        a = current + (0,) * (n - len(current))
        b = target + (0,) * (n - len(target))
        ok = {
            ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b,
        }[op]
        if not ok:
            return False
    return True


def validate_pack(pack: dict, source: str = "<memory>") -> list[str]:
    errors: list[str] = []
    required = ("schema", "id", "version", "title")
    for key in required:
        if not pack.get(key):
            errors.append(f"{source}: 缺少 {key}")
    if pack.get("schema") != SCHEMA:
        errors.append(f"{source}: schema 应为 {SCHEMA}，实际 {pack.get('schema')!r}")
    if pack.get("id") and not ID_RE.fullmatch(str(pack["id"])):
        errors.append(f"{source}: id 非法（小写字母/数字/连字符）: {pack['id']!r}")
    if pack.get("version"):
        try:
            _version_tuple(str(pack["version"]))
        except DomainPackError as exc:
            errors.append(str(exc))
    requires = pack.get("requires") or {}
    if not isinstance(requires, dict):
        errors.append(f"{source}: requires 应为对象")
    for key in ("contract", "engine"):
        if key in requires:
            try:
                _version_tuple(str(requires[key]).lstrip("<>="))
            except DomainPackError:
                # 约束可能形如 >=2,<3，交给 version_satisfies 做真正的语法检查
                try:
                    version_satisfies("0", str(requires[key]))
                except DomainPackError as exc:
                    errors.append(str(exc))
    components = pack.get("components") or {}
    if not isinstance(components, dict):
        errors.append(f"{source}: components 应为对象")
    for key in ("parsers", "assets"):
        if key in components and not isinstance(components[key], list):
            errors.append(f"{source}: components.{key} 应为列表")
    extensions = pack.get("profile_extensions") or {}
    if not isinstance(extensions, dict):
        errors.append(f"{source}: profile_extensions 应为对象")
    allow = extensions.get("allow")
    if allow is not None and not isinstance(allow, list):
        errors.append(f"{source}: profile_extensions.allow 应为列表")
    policy = pack.get("format_policy") or {}
    if policy and not isinstance(policy, dict):
        errors.append(f"{source}: format_policy 应为对象")
    return errors


def load_domain_pack(pack_id: str | None = None, pack_path: str | Path | None = None,
                     root: Path | None = None) -> dict:
    """按 id 或显式路径加载域包，返回带 _source/_path 的字典。"""
    root = root or skill_root()
    if pack_path:
        path = Path(pack_path).expanduser().resolve()
        if path.is_dir():
            path = path / "domain-pack.yaml"
            if not path.exists():
                path = path.parent / "domain-pack.json"
        data = _read(path)
    else:
        pid = pack_id or DEFAULT_ID
        base = domains_root(root) / pid
        path = base / "domain-pack.yaml"
        if not path.exists():
            path = base / "domain-pack.json"
        data = _read(path)
    errors = validate_pack(data, str(path))
    if errors:
        raise DomainPackError("DOMAIN_PACK_INVALID: " + "; ".join(errors))
    if pack_id and data.get("id") != pack_id:
        raise DomainPackError(
            f"DOMAIN_PACK_ID_MISMATCH: 请求 {pack_id}，域包声明 {data.get('id')}")
    data["_source"] = str(path)
    data["_path"] = str(path)
    return data


def check_compatible(pack: dict, contract_version: str, engine_version: str) -> list[str]:
    errors: list[str] = []
    requires = pack.get("requires") or {}
    if requires.get("contract") and not version_satisfies(contract_version, str(requires["contract"])):
        errors.append(
            f"DOMAIN_PACK_INCOMPATIBLE: 域包 {pack.get('id')} 要求 contract {requires['contract']}，"
            f"当前 {contract_version}")
    if requires.get("engine") and not version_satisfies(engine_version, str(requires["engine"])):
        errors.append(
            f"DOMAIN_PACK_INCOMPATIBLE: 域包 {pack.get('id')} 要求 engine {requires['engine']}，"
            f"当前 {engine_version}")
    return errors


def list_domain_packs(root: Path | None = None) -> list[dict]:
    base = domains_root(root)
    items: list[dict] = []
    if not base.is_dir():
        return items
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        path = child / "domain-pack.yaml"
        if not path.exists():
            path = child / "domain-pack.json"
        if not path.exists():
            continue
        try:
            data = load_domain_pack(child.name, root=root)
        except DomainPackError as exc:
            items.append({"id": child.name, "ok": False, "error": str(exc)})
            continue
        items.append({
            "id": data.get("id"), "version": data.get("version"),
            "title": data.get("title"), "path": str(path), "ok": True,
        })
    return items


def summary(pack: dict) -> dict:
    return {
        "schema": pack.get("schema"),
        "id": pack.get("id"),
        "version": pack.get("version"),
        "title": pack.get("title"),
        "requires": pack.get("requires") or {},
        "components": pack.get("components") or {},
        "format_policy": pack.get("format_policy") or {},
        "routing": pack.get("routing") or {},
        "report": pack.get("report") or {},
        "path": pack.get("_path") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="加载/校验/描述 skill-domain-pack")
    ap.add_argument("--root", help="元技能根目录（默认脚本上一级）")
    ap.add_argument("--list", action="store_true", help="列出 domains/ 下的域包")
    ap.add_argument("--describe", metavar="ID", help="输出域包摘要")
    ap.add_argument("--validate", metavar="ID", help="校验域包")
    ap.add_argument("--path", help="显式域包路径（可与 --describe/--validate 组合）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else skill_root()
    try:
        if args.list:
            data = list_domain_packs(root)
        else:
            pack = load_domain_pack(args.describe or args.validate, args.path, root=root)
            data = summary(pack)
            errors = validate_pack(pack, pack.get("_source", "<memory>"))
            if errors:
                raise DomainPackError("; ".join(errors))
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json
              else json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except DomainPackError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
