#!/usr/bin/env python3
"""规则库/候选 profile 差异对比（builder 侧工具，不分发给子 Skill）。

用法：python rule_library_diff.py <left> <right> [--json out.json]
left/right 可为规则库 xlsx/csv，或候选 profile（需 PyYAML）。

身份键 = 故障呈现/规则名称（重名按序号区分）；内容摘要 = 关键字+日志路径+检查规则。
仅一侧有身份键 → added/removed；两侧都有但内容摘要不同 → changed。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_rule_library import detect_and_parse  # noqa: E402


def load_profile_rules(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            sys.exit(f"{path} 为 YAML 但缺少 PyYAML：pip install pyyaml")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    rules = []
    for st in (data.get("analysis") or {}).get("steps") or []:
        for r in st.get("rules") or []:
            desc = re.sub(r"（来源 .*?）$", "", str(r.get("description", ""))) or r.get("id", "")
            lib = r.get("library_meta") or {}
            rules.append({"key": desc,
                          "fp_kw": "；".join(r.get("all_of") or []) + "|" + "；".join(r.get("any_of") or []),
                          "fp_path": ",".join(r.get("globs") or []),
                          "fp_check": lib.get("check") or lib.get("detail", "")})
    return rules


def load_rules(path):
    p = Path(path)
    if not p.is_file():
        sys.exit(f"文件不存在: {p}")
    if p.suffix.lower() in (".yaml", ".yml", ".json"):
        return load_profile_rules(p)
    warns = []
    _t, rules, _m = detect_and_parse(p, "auto", warns)
    for w in warns:
        print(f"[!] {w}", file=sys.stderr)
    return rules


def index(rules):
    out, cnt = {}, {}
    for r in rules:
        k = r["key"]
        cnt[k] = cnt.get(k, 0) + 1
        ident = k if cnt[k] == 1 else f"{k}#{cnt[k]}"
        out[ident] = {"keywords": r["fp_kw"], "paths": r["fp_path"], "check": r["fp_check"]}
    return out


def diff(left, right):
    li, ri = index(left), index(right)
    added = [{"rule": k, **ri[k]} for k in ri if k not in li]
    removed = [{"rule": k, **li[k]} for k in li if k not in ri]
    changed = [{"rule": k, "left": li[k], "right": ri[k]}
               for k in li if k in ri and li[k] != ri[k]]
    return added, removed, changed


def main(argv=None):
    ap = argparse.ArgumentParser(description="规则库差异对比")
    ap.add_argument("left")
    ap.add_argument("right")
    ap.add_argument("--json")
    ap.add_argument("--fail-on-change", action="store_true", help="存在任何差异时退出码 1（CI 用）")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    added, removed, changed = diff(load_rules(a.left), load_rules(a.right))
    print(f"新增 {len(added)}；删除 {len(removed)}；变更 {len(changed)}")
    for tag, items in (("+", added), ("-", removed)):
        for it in items:
            print(f"  [{tag}] {it['rule']}")
    for it in changed:
        l, r = it["left"], it["right"]
        print(f"  [~] {it['rule']}\n      左: {l['keywords']} | {l['paths']} | {l['check']}"
              f"\n      右: {r['keywords']} | {r['paths']} | {r['check']}")
    if a.json:
        Path(a.json).write_text(json.dumps(
            {"left": a.left, "right": a.right, "added": added, "removed": removed,
             "changed": changed}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if (a.fail_on_change and (added or removed or changed)) else 0


if __name__ == "__main__":
    sys.exit(main())
