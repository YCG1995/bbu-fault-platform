#!/usr/bin/env python3
"""
validate_skill_md.py — 按 Agent Skills 规范（agentskills.io/specification）校验 SKILL.md frontmatter

校验项：frontmatter 存在且可解析（PyYAML 缺失时用内置简易解析器，覆盖本仓库生成的扁平/一层 metadata 结构）；
字段白名单 {name, description, license, compatibility, metadata, allowed-tools}；name 1-64、hyphen-case、
无首尾/连续连字符、与目录名一致；description 1-1024；compatibility ≤500；metadata 为 str→str 映射。
零依赖；退出码 0 全部通过 / 1 有错误。

用法：
    python3 validate_skill_md.py <skill目录|SKILL.md> [...]
    python3 validate_skill_md.py --skills-dir <skills根>      # 校验根下所有含 SKILL.md 的子目录
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

ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME, MAX_DESC, MAX_COMPAT = 64, 1024, 500
FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


def _scalar(raw: str):
    raw = raw.strip()
    if raw.startswith('"'):
        return json.loads(raw)  # YAML 双引号字符串 ⊇ JSON 字符串
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    return raw


def parse_frontmatter_simple(text: str):
    """无 PyYAML 时的简易解析：顶层 `key: value`，metadata 下一层两空格缩进 `key: value`。"""
    data, cur = {}, None
    for ln in text.split("\n"):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln.startswith("  ") and cur is not None:
            k, _, v = ln.strip().partition(":")
            if not _:
                raise ValueError(f"metadata 行无法解析: {ln!r}")
            data[cur][k.strip()] = _scalar(v)
            continue
        k, _, v = ln.partition(":")
        if not _ or ln.startswith(" "):
            raise ValueError(f"frontmatter 行无法解析: {ln!r}")
        k = k.strip()
        if k in data:
            raise ValueError(f"重复字段: {k}")
        if v.strip() == "":
            data[k], cur = {}, k
        else:
            data[k], cur = _scalar(v), None
    return data


def validate_file(skill_md: Path):
    errors = []
    text = skill_md.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        return ["缺少 frontmatter（文件须以 --- 开头并以 --- 结束）"]
    fm_text = m.group(1)
    try:
        if _HAS_YAML:
            data = yaml.safe_load(fm_text)
            # 重复键：PyYAML 静默覆盖，这里按行再查一次
            top_keys = [ln.split(":", 1)[0] for ln in fm_text.split("\n") if ln and not ln.startswith((" ", "#")) and ":" in ln]
            dup = {k for k in top_keys if top_keys.count(k) > 1}
            if dup:
                errors.append(f"重复字段: {sorted(dup)}")
        else:
            data = parse_frontmatter_simple(fm_text)
    except Exception as e:
        return [f"frontmatter 无法解析: {e}"]
    if not isinstance(data, dict):
        return ["frontmatter 不是映射"]
    extra = set(data) - ALLOWED
    if extra:
        errors.append(f"未知字段 {sorted(extra)}（允许: {sorted(ALLOWED)}）")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        errors.append("name 缺失或非字符串")
    else:
        if len(name) > MAX_NAME or not NAME_RE.fullmatch(name):
            errors.append(f"name 非法: {name!r}（1-{MAX_NAME} 字符，a-z0-9 与连字符，无首尾/连续连字符）")
        if name != skill_md.parent.name:
            errors.append(f"name {name!r} 与目录名 {skill_md.parent.name!r} 不一致")
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append("description 缺失或为空")
    elif len(desc) > MAX_DESC:
        errors.append(f"description 超长 {len(desc)} > {MAX_DESC}")
    compat = data.get("compatibility")
    if compat is not None:
        if not isinstance(compat, str):
            errors.append("compatibility 须为字符串")
        elif len(compat) > MAX_COMPAT:
            errors.append(f"compatibility 超长 {len(compat)} > {MAX_COMPAT}")
    meta = data.get("metadata")
    if meta is not None:
        if not isinstance(meta, dict):
            errors.append("metadata 须为映射")
        else:
            bad = [k for k, v in meta.items() if not isinstance(k, str) or not isinstance(v, str)]
            if bad:
                errors.append(f"metadata 值须为字符串（请加引号）: {bad}")
    for k in ("license", "allowed-tools"):
        if k in data and not isinstance(data[k], str):
            errors.append(f"{k} 须为字符串")
    if text.count("{{") and re.search(r"\{\{[A-Z_?/]+\}\}", text):
        errors.append("正文残留未渲染的模板标记 {{...}}")
    return errors


def main():
    ap = argparse.ArgumentParser(description="按 Agent Skills 规范校验 SKILL.md")
    ap.add_argument("targets", nargs="*", help="skill 目录或 SKILL.md 路径")
    ap.add_argument("--skills-dir", help="校验该目录下所有含 SKILL.md 的子目录")
    args = ap.parse_args()
    _utf8_stdio()
    files = []
    for t in args.targets:
        p = Path(t)
        files.append(p if p.is_file() else p / "SKILL.md")
    if args.skills_dir:
        files += sorted(q / "SKILL.md" for q in Path(args.skills_dir).iterdir() if (q / "SKILL.md").exists())
    if not files:
        ap.error("请给出 skill 目录/SKILL.md 或 --skills-dir")
    failed = 0
    for f in files:
        if not f.exists():
            print(f"[x] {f}: 不存在")
            failed += 1
            continue
        errs = validate_file(f)
        if errs:
            failed += 1
            print(f"[x] {f.parent.name}: {len(errs)} 项不符合规范")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"[OK] {f.parent.name}: frontmatter 符合 Agent Skills 规范"
                  + ("" if _HAS_YAML else "（无 PyYAML，简易解析）"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
