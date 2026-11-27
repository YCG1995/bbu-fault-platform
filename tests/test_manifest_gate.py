"""1.28.0 清单门禁：路由、工具、CLI、UI 动作必须与源码实时一致。

运行：python3 -m pytest tests/test_manifest_gate.py -q
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = ROOT / "tests" / "manifests"


def load(name: str) -> dict:
    return json.loads((MANIFEST_DIR / name).read_text(encoding="utf-8"))


def api_routes() -> list[tuple[str, str, str, int]]:
    """从 bbu/api.py AST 提取 (method, template, handler, line)。"""
    src = (ROOT / "bbu/api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    pat = re.compile(r"\(\?P<(.+?)>")

    def template(p: str) -> str:
        out: list[str] = []
        i = 0
        while True:
            m = pat.search(p, i)
            if not m:
                out.append(p[i:])
                break
            out.append(p[i:m.start()])
            name, j, depth = m.group(1), m.end(), 1
            while j < len(p) and depth:
                if p[j] == "(":
                    depth += 1
                elif p[j] == ")":
                    depth -= 1
                j += 1
            out.append("{" + name + "}")
            i = j
        return "".join(out)

    result = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add" and len(node.args) >= 2):
            continue
        try:
            method = ast.literal_eval(node.args[0])
            pattern = ast.literal_eval(node.args[1])
        except Exception:
            continue
        if not (isinstance(method, str) and isinstance(pattern, str)):
            continue
        if not (pattern.startswith("/api") or pattern == "/mcp"):
            continue
        handler = ""
        if len(node.args) > 2:
            arg = node.args[2]
            if isinstance(arg, ast.Name):
                handler = arg.id
            else:
                try:
                    handler = str(ast.literal_eval(arg))
                except Exception:
                    handler = ""
        result.append((method, template(pattern), handler, node.lineno))
    return result


def test_rest_manifest_covers_every_route():
    manifest = load("rest_routes.json")
    actual = api_routes()
    assert len(actual) == 211, f"源码路由数量变化：{len(actual)}"
    assert manifest["total"] == len(manifest["routes"]) == 211
    assert {(r["method"], r["path"]) for r in manifest["routes"]} == {
        (m, path) for m, path, _, _ in actual
    }
    assert len({(r["method"], r["path"]) for r in manifest["routes"]}) == 211


def test_rest_manifest_params_and_cases():
    manifest = load("rest_routes.json")
    all_cases: set[str] = set()
    for r in manifest["routes"]:
        assert r["id"] and r["handler"], r
        assert r["auth"] in ("public", "user", "admin"), r
        assert r["case_ids"], r
        assert not (set(r["case_ids"]) & all_cases), r["id"]
        all_cases.update(r["case_ids"])
        path_names = set(re.findall(r"\{([^}]+)\}", r["path"]))
        listed = {p["name"] for p in r["params"] if p.get("in") == "path"}
        assert listed == path_names, (r["path"], listed, path_names)
        for p in r["params"]:
            assert p.get("in") in ("path", "query", "body", "file", "header"), (r["id"], p)
            assert p.get("name") and "required" in p, (r["id"], p)


def test_negative_matrix_has_core_codes():
    manifest = load("rest_routes.json")
    codes = {code for r in manifest["routes"] for code in r.get("expected_errors", [])}
    for code in (400, 401, 403, 404, 409, 413, 429, 502, 504):
        assert code in codes, f"缺少错误码覆盖：{code}；实际 {sorted(codes)}"


def test_api_smoke_expected_routes_match_manifest():
    sys.path.insert(0, str(ROOT / "tests"))
    import api_full_smoke  # noqa: E402

    manifest = load("rest_routes.json")
    norm = lambda p: re.sub(r"\{[^}]+\}", "{id}", p)
    routes = {(r["method"], norm(r["path"])) for r in manifest["routes"]}
    expected = {(m, norm(p)) for m, p in (item.split(" ", 1) for item in api_full_smoke.EXPECTED_ROUTES)}
    assert len(expected) == 211
    assert expected == routes, sorted(expected ^ routes)[:10]


def test_assistant_tools_manifest():
    manifest = load("tool_specs.json")
    sys.path.insert(0, str(ROOT))
    from bbu.assistant_tools import ToolRegistry

    registry = ToolRegistry(None, None, None)
    assert manifest["counts"]["assistant_tools"] == len(manifest["assistant_tools"]) == 23
    assert len(registry._tools) == 23
    assert {t["name"] for t in manifest["assistant_tools"]} == set(registry._tools)
    for t in manifest["assistant_tools"]:
        assert t["title"] and t["description"]
        assert t["scope"] in ("user", "admin")
        assert t["risk"] in ("read", "medium", "high")
        assert t["mcp"] in ("read", "write", "never")
        schema = t["schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert isinstance(schema.get("properties"), dict)
        for key in schema.get("required", []):
            assert key in schema["properties"], (t["name"], key)


def test_mcp_method_manifest():
    manifest = load("tool_specs.json")
    source = (ROOT / "bbu/mcp_server.py").read_text(encoding="utf-8")
    assert manifest["counts"]["mcp_methods"] == len(manifest["mcp_methods"]) == 14
    for method in manifest["mcp_methods"]:
        if method.startswith("notifications/"):
            assert method in manifest["mcp_methods"]
        else:
            assert f'"{method}"' in source, f"MCP 方法未在源码出现：{method}"


def test_cli_manifest_files_exist():
    manifest = load("cli_options.json")
    assert manifest["count"] == len(manifest["scripts"])
    assert {s["kind"] for s in manifest["scripts"]} == {
        "meta-script", "platform-tool", "deploy-shell"
    }
    for item in manifest["scripts"]:
        path = ROOT / item["path"]
        assert path.is_file(), item
        assert path.stat().st_size > 0, item


def test_ui_manifest_matches_static_source():
    manifest = load("ui_actions.json")
    files = sorted((ROOT / "web").rglob("*.js")) + sorted((ROOT / "web").rglob("*.html"))
    acts: list[str] = []
    buttons = 0
    modals = 0
    forms: list[str] = []
    for f in files:
        text = f.read_text(errors="ignore")
        acts += re.findall(r'data-act=["\']([^"\']+)["\']', text)
        buttons += len(re.findall(r"<button\b", text))
        modals += len(re.findall(r"\bmodal\(", text))
        forms += re.findall(r'<form\b[^>]*\bid=["\']([^"\']+)["\']', text)
    counts = manifest["counts"]
    assert counts["distinct_actions"] == len(set(acts))
    assert counts["action_instances"] == len(acts)
    assert counts["buttons_all"] == buttons
    assert counts["modal_calls"] == modals
    assert counts["forms"] == len(set(forms))
    assert {a["action"] for a in manifest["actions"]} == set(acts)
    assert set(manifest["forms"]) == set(forms)


def test_version_is_1_30_5():
    from bbu import __version__

    assert __version__ == "1.30.5"
