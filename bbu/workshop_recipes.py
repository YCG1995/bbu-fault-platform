"""领域配方库：把看门狗、SSD、USB FLASH 等少量领域知识从通用编译器里拆出来。

配方是数据，不是代码分支。配方规则默认 verified=false，必须由用户确认或
在真实案例回归通过后才能进入根因条件；这样避免“编译器默认把少量样本的规则
标成已验证”。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


RECIPE_DIR = Path(__file__).resolve().parent / "workshop_recipes"
RECIPE_SCHEMA = "bbu-workshop-recipe/1"

SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "reset": ("复位", "simplelog", "bbx"),
    "kernel": ("内核", "kernel"),
    "board": ("boardinit", "board init", "启动"),
    "health": ("boardhealth", "smart", "健康", "health"),
}

SOURCE_FALLBACK: dict[str, list[str]] = {
    "reset": ["**/*Simplelog*", "**/*BBX_Simplelog*"],
    "kernel": ["**/*kernel*.klog*", "**/*kernel*.log*"],
    "board": ["**/*BoardInit.log*", "**/BoardInit*"],
    "health": ["**/*BoardHealth.log*", "**/BoardHealth*", "**/*BoardInit.log*"],
}


class RecipeError(ValueError):
    """配方文件缺失、格式错误或未知配方。"""


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _validate_recipe(data: dict[str, Any], filename: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", str(data.get("id") or "")):
        raise RecipeError(f"配方 id 非法: {filename}")
    slug = str(data.get("skill_slug") or "")
    if slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", slug):
        raise RecipeError(f"配方 skill_slug 非法: {filename}")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RecipeError(f"配方缺少 rules: {filename}")
    rule_ids = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise RecipeError(f"配方规则必须是对象: {filename}")
        rid = str(rule.get("id") or "")
        if not re.fullmatch(r"E\d+", rid):
            raise RecipeError(f"配方规则 ID 必须是 E1..En: {filename}: {rid}")
        if rid in rule_ids:
            raise RecipeError(f"配方规则 ID 重复: {filename}: {rid}")
        rule_ids.append(rid)
        if not isinstance(rule.get("any_of"), list) and not isinstance(rule.get("all_of"), list):
            raise RecipeError(f"配方规则缺少 any_of/all_of: {filename}: {rid}")
    roots = data.get("root_causes")
    if not isinstance(roots, list) or not roots:
        raise RecipeError(f"配方缺少 root_causes: {filename}")
    root_ids = []
    for root in roots:
        if not isinstance(root, dict):
            raise RecipeError(f"配方根因必须是对象: {filename}")
        rid = str(root.get("id") or "")
        if not re.fullmatch(r"R\d+", rid) or rid in root_ids:
            raise RecipeError(f"配方根因 ID 非法或重复: {filename}: {rid}")
        root_ids.append(rid)
        refs = set(re.findall(r"E\d+", " ".join(
            [str(x) for x in (root.get("all") or []) + (root.get("any") or [])]
            + [str(x) for x in (root.get("excludes") or [])])))
        missing = refs - set(rule_ids)
        if missing:
            raise RecipeError(f"配方根因引用不存在的规则: {filename}: {rid}: {sorted(missing)}")


@lru_cache(maxsize=1)
def _load_recipes() -> tuple[dict[str, Any], ...]:
    if not RECIPE_DIR.is_dir():
        return ()
    recipes: list[dict[str, Any]] = []
    for path in sorted(RECIPE_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RecipeError(f"配方不可读: {path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise RecipeError(f"配方必须是对象: {path.name}")
        if str(data.get("schema") or "") != RECIPE_SCHEMA:
            raise RecipeError(f"配方 schema 非法: {path.name}")
        rid = _one_line(data.get("id"))
        if not rid:
            raise RecipeError(f"配方缺少 id: {path.name}")
        if any(x.get("id") == rid for x in recipes):
            raise RecipeError(f"配方 id 重复: {rid}")
        _validate_recipe(data, path.name)
        data["_source_file"] = path.relative_to(RECIPE_DIR.parent.parent).as_posix()
        recipes.append(data)
    return tuple(recipes)


def list_recipes() -> list[dict[str, Any]]:
    """供 API/UI 展示的配方摘要，不返回规则正文。"""
    return [{
        "id": str(recipe.get("id") or ""),
        "name": str(recipe.get("name") or ""),
        "title": str(recipe.get("title") or ""),
        "skill_slug": str(recipe.get("skill_slug") or ""),
        "category": str(recipe.get("category") or ""),
        "version_scope": str(recipe.get("version_scope") or ""),
        "description": str(recipe.get("description") or ""),
        "verified": False,
        "declared_verified": bool(recipe.get("verified", False)),
        "sample_cases": dict(recipe.get("sample_cases") or {}),
        "source": str(recipe.get("_source_file") or ""),
    } for recipe in _load_recipes()]


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    wanted = _one_line(recipe_id)
    for recipe in _load_recipes():
        if recipe.get("id") == wanted:
            return dict(recipe)
    return None


def _trigger_match(recipe: dict[str, Any], text_low: str) -> bool:
    trigger = recipe.get("trigger") or {}
    all_terms = [str(x).lower() for x in _string_list(trigger.get("all_terms"))]
    any_terms = [str(x).lower() for x in _string_list(trigger.get("any_terms"))]
    if not all_terms and not any_terms:
        return False
    if any(term not in text_low for term in all_terms):
        return False
    if any_terms and not any(term in text_low for term in any_terms):
        return False
    return True


def recipe_matches(recipe: dict[str, Any], text: str) -> bool:
    return _trigger_match(recipe, str(text or "").lower())


def detect_recipes(text: str) -> list[dict[str, Any]]:
    """按配方 trigger 做显式候选匹配；返回值按匹配强度排序。"""
    low = str(text or "").lower()
    hits: list[tuple[int, dict[str, Any]]] = []
    for recipe in _load_recipes():
        if not _trigger_match(recipe, low):
            continue
        trigger = recipe.get("trigger") or {}
        score = len(_string_list(trigger.get("all_terms"))) + sum(
            1 for term in _string_list(trigger.get("any_terms")) if term.lower() in low)
        hits.append((score, recipe))
    hits.sort(key=lambda item: item[0], reverse=True)
    return [dict(recipe) for _, recipe in hits]


def _source_globs(sources: list[dict[str, Any]], key: str) -> list[str]:
    aliases = SOURCE_ALIASES.get(key, (key,))
    for source in sources:
        name = str(source.get("name") or "").lower()
        if any(alias in name for alias in aliases):
            globs = [str(x) for x in source.get("globs") or [] if str(x).strip()]
            if globs:
                return globs
    return list(SOURCE_FALLBACK.get(key) or ["**/*.log*", "**/*.txt"])


def _term_ok(rule: dict[str, Any], low: str, key: str) -> bool:
    all_terms = [str(x).lower() for x in _string_list(rule.get(key + "_all"))]
    any_terms = [str(x).lower() for x in _string_list(rule.get(key + "_any"))]
    if any(term not in low for term in all_terms):
        return False
    if any_terms and not any(term in low for term in any_terms):
        return False
    return True


def _patterns(value: Any, low: str) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            token = item.strip()
            if token:
                out.append(re.escape(token))
            continue
        if not isinstance(item, dict):
            continue
        if not _term_ok(item, low, "when"):
            continue
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        out.append(pattern if item.get("regex") else re.escape(pattern))
    return out


def _build_rule(recipe: dict[str, Any], rule: dict[str, Any], low: str,
                sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not _term_ok(rule, low, "when"):
        return None
    any_of = _patterns(rule.get("any_of"), low)
    all_of = _patterns(rule.get("all_of"), low)
    none_of = _patterns(rule.get("none_of"), low)
    if not any_of and not all_of:
        return None
    return {
        "id": str(rule.get("id") or "").strip(),
        "description": _one_line(rule.get("description")) or "领域配方证据",
        "globs": _source_globs(sources, str(rule.get("source") or "")),
        "all_of": all_of,
        "any_of": any_of,
        "none_of": none_of,
        "min_matches": max(1, int(rule.get("min_matches") or 1)),
        "verified": False,
        "recipe_id": str(recipe.get("id") or ""),
        "sample_cases": dict(rule.get("sample_cases") or recipe.get("sample_cases") or {}),
        "version_scope": str(recipe.get("version_scope") or ""),
        "source_excerpt": any_of[0] if any_of else all_of[0],
    }


def _build_condition(root: dict[str, Any], available: set[str]) -> str:
    all_refs = [str(x).strip() for x in _string_list(root.get("all"))]
    any_refs = [str(x).strip() for x in _string_list(root.get("any"))]
    if all_refs and any(ref not in available for ref in all_refs):
        return ""
    if any_refs and not any(ref in available for ref in any_refs):
        return ""
    parts: list[str] = []
    if all_refs:
        parts.append(" and ".join(all_refs))
    if any_refs:
        live = [ref for ref in any_refs if ref in available]
        parts.append("(" + " or ".join(live) + ")")
    expression = " and ".join(parts)
    if not expression:
        return ""
    for ref in _string_list(root.get("excludes")):
        if ref in available:
            expression += f" and not {ref}"
    return expression


def apply_recipe(recipe: dict[str, Any], text: str, sources: list[dict[str, Any]],
                 actions: list[str], selection: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """把配方编译为 FaultSpec 片段；不修改配方文件。"""
    low = str(text or "").lower()
    rules: list[dict[str, Any]] = []
    for rule in recipe.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        built = _build_rule(recipe, rule, low, sources)
        if built and built["id"]:
            rules.append(built)
    by_id = {rule["id"]: rule for rule in rules}
    roots: list[dict[str, Any]] = []
    fallback_actions = _string_list(recipe.get("default_actions")) or ["按现场作业指导书处理（待补充）"]
    for root in recipe.get("root_causes") or []:
        if not isinstance(root, dict):
            continue
        condition = _build_condition(root, set(by_id))
        if not condition:
            continue
        roots.append({
            "id": str(root.get("id") or "").strip(),
            "name": _one_line(root.get("name")) or "领域配方根因",
            "condition": condition,
            "mechanism": _one_line(root.get("mechanism")) or "待补充",
            "confidence": root.get("confidence") if root.get("confidence") in ("high", "medium", "low") else "medium",
            "actions": list(actions) if actions else list(fallback_actions),
            "prerequisites": [],
            "risks": "待补充",
            "rollback": "待补充",
            "verify": [],
            "escalation": "待补充",
            "recipe_id": str(recipe.get("id") or ""),
            "sample_cases": dict(root.get("sample_cases") or recipe.get("sample_cases") or {}),
            "version_scope": str(recipe.get("version_scope") or ""),
            "case_keywords": _string_list(root.get("case_keywords")),
        })
    meta = {
        "id": str(recipe.get("id") or ""),
        "name": str(recipe.get("name") or ""),
        "title": str(recipe.get("title") or ""),
        "skill_slug": str(recipe.get("skill_slug") or ""),
        "category": str(recipe.get("category") or ""),
        "version_scope": str(recipe.get("version_scope") or ""),
        "description": str(recipe.get("description") or ""),
        "aliases": _string_list(recipe.get("aliases")),
        "keywords": _string_list(recipe.get("keywords")),
        "verified": False,
        "declared_verified": bool(recipe.get("verified", False)),
        "selection": selection,
        "source": str(recipe.get("_source_file") or ""),
        "sample_cases": dict(recipe.get("sample_cases") or {}),
        "reasoning": dict(recipe.get("reasoning") or {}),
    }
    return rules, roots, meta
