#!/usr/bin/env python3
"""离线物理清理已归档知识库。

安全约束：
1. 默认库拒绝清理；
2. 知识库必须已归档；
3. 必须提供与该库 manifest.kb_id 一致、计数完整的 *.kbbackup.zip；
4. 必须同时传 --expected-name 与库名精确匹配，并加 --yes。

保留 kb_case_revisions 与 kb_audit_logs（append-only 审计），只清文档/案例/
草稿/审核/导入记录/索引/术语/版本和已无其他库引用的源文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Any


def _jsonl_count(data: bytes) -> int:
    return sum(1 for line in data.decode("utf-8").splitlines() if line.strip())


def _validate_backup(path: Path, kb_id: int) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("备份文件不存在")
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            docs_raw = zf.read("documents.jsonl")
            cases_raw = zf.read("cases.jsonl")
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ValueError(f"备份包无法读取：{exc}") from exc
    if manifest.get("format") != "bbu-kb-backup":
        raise ValueError("不是 bbu-kb-backup 备份包")
    if int(manifest.get("kb_id") or 0) != kb_id:
        raise ValueError("备份包的 kb_id 与目标库不一致")
    docs = _jsonl_count(docs_raw)
    cases = _jsonl_count(cases_raw)
    if docs != int(manifest.get("documents") or 0):
        raise ValueError("备份包 documents 计数不一致")
    if cases != int(manifest.get("cases") or 0):
        raise ValueError("备份包 cases 计数不一致")
    return manifest


def purge_library(db_path: str | Path, kb_id: int, expected_name: str,
                  backup_file: str | Path, data_root: str | Path, *,
                  dry_run: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise ValueError("数据库不存在")
    backup_file = Path(backup_file)
    manifest = _validate_backup(backup_file, kb_id)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        lib = conn.execute("SELECT * FROM kb_libraries WHERE id=?", (kb_id,)).fetchone()
        if not lib:
            raise ValueError("知识库不存在")
        if lib["name"] != expected_name:
            raise ValueError("--expected-name 与库名不匹配")
        if int(lib["is_default"] or 0):
            raise ValueError("默认知识库不可清理")
        if lib["status"] != "archived" or not lib["deleted_at"]:
            raise ValueError("知识库必须先归档再清理")
        counts = {
            "documents": conn.execute("SELECT COUNT(*) FROM kb_documents WHERE kb_id=?", (kb_id,)).fetchone()[0],
            "cases": conn.execute("SELECT COUNT(*) FROM kb_cases WHERE kb_id=?", (kb_id,)).fetchone()[0],
            "drafts": conn.execute("SELECT COUNT(*) FROM kb_drafts WHERE kb_id=?", (kb_id,)).fetchone()[0],
            "reviews": conn.execute("SELECT COUNT(*) FROM kb_reviews WHERE kb_id=?", (kb_id,)).fetchone()[0],
            "revisions": conn.execute("SELECT COUNT(*) FROM kb_case_revisions WHERE case_id IN "
                                      "(SELECT id FROM kb_cases WHERE kb_id=?)", (kb_id,)).fetchone()[0],
        }
        if docs := int(manifest.get("documents") or 0):
            if counts["documents"] != docs:
                raise ValueError("当前库文档数与备份 manifest 不一致；请先重新备份")
        if cases_count := int(manifest.get("cases") or 0):
            if counts["cases"] != cases_count:
                raise ValueError("当前库案例数与备份 manifest 不一致；请先重新备份")
        if dry_run:
            return {"ok": True, "dry_run": True, "kb_id": kb_id, "counts": counts}
        paths = [r["stored_path"] for r in conn.execute(
            "SELECT f.stored_path FROM kb_import_files f "
            "JOIN kb_import_jobs j ON j.id=f.job_id WHERE j.kb_id=? AND f.stored_path<>''",
            (kb_id,)).fetchall()]
        with conn:
            conn.execute("DELETE FROM kb_case_usage_daily WHERE case_id IN "
                         "(SELECT id FROM kb_cases WHERE kb_id=?)", (kb_id,))
            conn.execute("DELETE FROM kb_case_tags WHERE case_id IN "
                         "(SELECT id FROM kb_cases WHERE kb_id=?)", (kb_id,))
            conn.execute("DELETE FROM kb_doc_versions WHERE doc_id IN "
                         "(SELECT id FROM kb_documents WHERE kb_id=?)", (kb_id,))
            conn.execute("DELETE FROM kb_drafts WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_reviews WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_import_files WHERE job_id IN "
                         "(SELECT id FROM kb_import_jobs WHERE kb_id=?)", (kb_id,))
            conn.execute("DELETE FROM kb_import_jobs WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_cases WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_documents WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_terms WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_library_index_meta WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_stats_daily WHERE dim_key=?", (f"kb:{kb_id}",))
            conn.execute("UPDATE kb_libraries SET status='deleted',deleted_at=?,updated_at=? WHERE id=?",
                         (lib["deleted_at"], lib["deleted_at"], kb_id))
        # 只删除已无任何库引用的源文件，避免误删同 SHA 的共享附件。
        root = Path(data_root).resolve()
        removed = 0
        for raw in paths:
            path = Path(raw)
            try:
                resolved = path.resolve()
                if os.path.commonpath([str(root), str(resolved)]) != str(root):
                    continue
            except (OSError, ValueError):
                continue
            still = conn.execute("SELECT 1 FROM kb_import_files WHERE stored_path=? LIMIT 1",
                                  (raw,)).fetchone()
            if not still and resolved.is_file():
                try:
                    resolved.unlink()
                    removed += 1
                except OSError:
                    pass
        return {"ok": True, "dry_run": False, "kb_id": kb_id, "counts": counts,
                "files_removed": removed, "manifest": manifest}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线物理清理已归档知识库")
    parser.add_argument("--db", required=True, help="bbu.db 路径")
    parser.add_argument("--kb-id", required=True, type=int)
    parser.add_argument("--expected-name", required=True, help="必须与库名完全一致")
    parser.add_argument("--backup-file", required=True, help="清理前导出的 *.kbbackup.zip")
    parser.add_argument("--data-root", default="", help="数据根目录，默认取 db 所在目录")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="确认物理清理，不可恢复")
    args = parser.parse_args(argv)
    if not args.yes and not args.dry_run:
        print("拒绝执行：物理清理不可恢复，请先 --dry-run 核对，再加 --yes。", file=sys.stderr)
        return 2
    data_root = Path(args.data_root) if args.data_root else Path(args.db).resolve().parent
    try:
        result = purge_library(args.db, args.kb_id, args.expected_name,
                               args.backup_file, data_root, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
