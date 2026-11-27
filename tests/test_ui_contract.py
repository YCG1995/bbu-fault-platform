"""R1/R2/R3/R4 UI 契约：动作、表单、异步基座和新增面板都已接线。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_manifest_actions_and_forms_are_consistent():
    manifest = json.loads((ROOT / "tests/manifests/ui_actions.json").read_text(encoding="utf-8"))
    counts = manifest["counts"]
    assert counts["distinct_actions"] > 0 and counts["action_instances"] >= counts["distinct_actions"]
    assert counts["forms"] == 21
    ids = set(manifest["forms"])
    assert "login-form" in ids and "workshop" not in ids
    for action in manifest["actions"]:
        assert action["action"] and action["instances"] >= 1


def test_async_ui_base_and_new_panels_exist():
    core = _text("web/assets/js/core.js")
    for token in ("withBusy", "requestGate", "abortInflight", "AbortController", "setBusy"):
        assert token in core
    workshop = _text("web/assets/js/page_workshop.js")
    assert "资料盘点" in workshop and "发布覆盖不足" in workshop
    detail = _text("web/assets/js/page_task_detail.js")
    assert "来源覆盖" in detail and "站点证据矩阵" in detail


def test_workshop_create_supports_multi_file_and_folder_upload():
    workshop = _text("web/assets/js/page_workshop.js")
    assert 'id="create-files"' in workshop and 'multiple' in workshop
    assert 'id="create-folder"' in workshop and "webkitdirectory" in workshop
    assert "auto_cases" in workshop and "file_paths" in workshop
    assert "auto_cases" in _text("bbu/api.py")


def test_model_config_has_real_select_for_discovered_models():
    admin = _text("web/assets/js/page_admin.js")
    assert 'id="model-select"' in admin
    assert "syncModelSelect" in admin
    assert "modelSelect.addEventListener('change'" in admin


def test_list_responses_are_null_safe():
    workshop = _text("web/assets/js/page_workshop.js")
    traces = _text("web/assets/js/page_traces.js")
    task_detail = _text("web/assets/js/page_task_detail.js")
    assert "Array.isArray(data)" in workshop and "data?.items" in workshop
    assert "Array.isArray(data)" in traces and "data?.items" in traces
    assert "Array.isArray(data)" in task_detail and "data?.items" in task_detail


def test_responsive_breakpoints_cover_narrow_mobile():
    css = _text("web/assets/css/app.css")
    assert re.search(r"@media\s*\(max-width:\s*640px\)", css)
    assert "overflow-x" in css or "min-width: 0" in css


def test_browser_relay_frontend_and_self_check_are_wired():
    relay = _text("web/assets/js/relay.js")
    app = _text("web/assets/js/app.js")
    admin = _text("web/assets/js/page_admin.js")
    httpd = _text("bbu/httpd.py")
    assert (ROOT / "web/relay-check.html").is_file()
    for token in ("/api/relay/status", "/api/relay/calls", "/api/relay/release"):
        assert token in relay
    assert "sendBeacon" in relay and "AbortController" in relay
    assert "startRelay" in app and "relay-pill" in app
    assert "browser_relay" in admin and "relay-test" in admin
    assert "浏览器代理" in admin
    assert "transportBadge" in _text("web/assets/js/page_usage.js")
    assert "browser_calls" in _text("web/assets/js/page_usage.js")
    assert "connect-src 'self' https: http:" in httpd
