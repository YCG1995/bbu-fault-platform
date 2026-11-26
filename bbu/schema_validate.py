"""离线 JSON Schema 校验器。

只实现平台合同实际使用的 Draft-07 子集，但对类型、必填、额外字段、边界、
组合关键字和本地 $ref 做严格校验；不依赖 jsonschema 等第三方包。
"""
from __future__ import annotations

import math
import re
from typing import Any

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _is_type(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return isinstance(value, _TYPE_MAP.get(expected, object))


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _path(parent: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
        return f"{parent}.{key}"
    return f"{parent}[{key!r}]"


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def validate_schema(data: Any, schema: dict[str, Any], path: str = "$",
                    root: dict[str, Any] | None = None) -> list[str]:
    """返回错误列表；空列表表示通过。错误消息可直接作为模型修复提示。"""
    root = root or schema
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema 不是对象"]
    if "$ref" in schema:
        target = _resolve_ref(root, str(schema["$ref"]))
        if target is None:
            return [f"{path}: 无法解析 $ref {schema['$ref']}"]
        return validate_schema(data, target, path, root)

    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(data, str(t)) for t in types):
            return [f"{path}: 期望 {'/'.join(map(str, types))}，实际 {_json_type(data)}"]

    if "const" in schema and data != schema["const"]:
        errors.append(f"{path}: 值必须为 {schema['const']!r}")
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: 值 {data!r} 不在枚举 {schema['enum']} 中")

    if "allOf" in schema:
        for item in schema.get("allOf", []) if isinstance(schema.get("allOf"), list) else []:
            errors.extend(validate_schema(data, item, path, root))
    if "anyOf" in schema:
        branches = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else []
        if branches and not any(not validate_schema(data, item, path, root) for item in branches):
            errors.append(f"{path}: 不满足 anyOf 任一分支")
    if "oneOf" in schema:
        branches = schema.get("oneOf") if isinstance(schema.get("oneOf"), list) else []
        if branches and sum(1 for item in branches if not validate_schema(data, item, path, root)) != 1:
            errors.append(f"{path}: 必须且只能满足 oneOf 一个分支")
    if "not" in schema and isinstance(schema["not"], dict):
        if not validate_schema(data, schema["not"], path, root):
            errors.append(f"{path}: 命中了禁止的 not schema")

    if isinstance(data, dict):
        required = schema.get("required") or []
        for key in required if isinstance(required, list) else []:
            if key not in data:
                errors.append(f"{path}: 缺少必填字段 {key}")
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        patterns = schema.get("patternProperties") if isinstance(schema.get("patternProperties"), dict) else {}
        for key, value in data.items():
            matched = False
            if key in props:
                matched = True
                errors.extend(validate_schema(value, props[key], _path(path, key), root))
            for pattern, subschema in patterns.items():
                try:
                    if re.search(pattern, str(key)):
                        matched = True
                        errors.extend(validate_schema(value, subschema, _path(path, key), root))
                except re.error:
                    errors.append(f"{path}: patternProperties 表达式非法 {pattern}")
            if not matched:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    errors.append(f"{path}: 不允许额外字段 {key}")
                elif isinstance(additional, dict):
                    errors.extend(validate_schema(value, additional, _path(path, key), root))
        min_props = schema.get("minProperties")
        max_props = schema.get("maxProperties")
        if isinstance(min_props, int) and len(data) < min_props:
            errors.append(f"{path}: 至少需要 {min_props} 个字段")
        if isinstance(max_props, int) and len(data) > max_props:
            errors.append(f"{path}: 最多允许 {max_props} 个字段")

    if isinstance(data, list):
        items = schema.get("items")
        if isinstance(items, list):
            for idx, value in enumerate(data):
                if idx < len(items):
                    errors.extend(validate_schema(value, items[idx], _path(path, idx), root))
                elif schema.get("additionalItems") is False:
                    errors.append(f"{path}: 不允许额外数组项 {idx}")
        elif isinstance(items, dict):
            for idx, value in enumerate(data):
                errors.extend(validate_schema(value, items, _path(path, idx), root))
        if isinstance(schema.get("minItems"), int) and len(data) < schema["minItems"]:
            errors.append(f"{path}: 至少需要 {schema['minItems']} 项")
        if isinstance(schema.get("maxItems"), int) and len(data) > schema["maxItems"]:
            errors.append(f"{path}: 最多允许 {schema['maxItems']} 项")
        if schema.get("uniqueItems") is True:
            seen: list[str] = []
            for idx, value in enumerate(data):
                marker = repr(value)
                if marker in seen:
                    errors.append(f"{path}: 数组项重复（索引 {idx}）")
                seen.append(marker)

    if isinstance(data, str):
        if isinstance(schema.get("minLength"), int) and len(data) < schema["minLength"]:
            errors.append(f"{path}: 字符串至少 {schema['minLength']} 字符")
        if isinstance(schema.get("maxLength"), int) and len(data) > schema["maxLength"]:
            errors.append(f"{path}: 字符串最多 {schema['maxLength']} 字符")
        if isinstance(schema.get("pattern"), str):
            try:
                if not re.search(schema["pattern"], data):
                    errors.append(f"{path}: 不匹配模式 {schema['pattern']}")
            except re.error:
                errors.append(f"{path}: 正则模式非法 {schema['pattern']}")

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        for key, op, wording in (
            ("minimum", lambda a, b: a >= b, "小于最小值"),
            ("maximum", lambda a, b: a <= b, "大于最大值"),
            ("exclusiveMinimum", lambda a, b: a > b, "不大于排他下限"),
            ("exclusiveMaximum", lambda a, b: a < b, "不小于排他上限"),
        ):
            limit = schema.get(key)
            if isinstance(limit, (int, float)) and not isinstance(limit, bool):
                if not op(data, limit):
                    errors.append(f"{path}: {wording} {limit}")
        multiple = schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and multiple and not isinstance(multiple, bool):
            quotient = data / multiple
            if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                errors.append(f"{path}: 必须是 {multiple} 的倍数")
    return errors
