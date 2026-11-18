#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JLCPCB Offline Parts Search Engine - Local Web Server & SQLite Query Engine
Architecture:
  - Backend: Python ThreadingHTTPServer + Native SQLite REAL/INTEGER Indexed Query Engine (db_engine.py)
  - Download Queue: Thread-safe Multi-URL Queue Manager
  - Frontend: Local Offline Vue 3 (static/vue.global.prod.js + static/index.html)
"""

import argparse
import csv
import importlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Dict, List

import db_engine
import jlc_fetcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "jlc_parts.db")
DEFAULT_CSV_PATH = os.path.join(DATA_DIR, "jlc_parts_db.csv")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# Global Multi-URL Download Queue State
DOWNLOAD_QUEUE: List[Dict[str, Any]] = []
QUEUE_WORKER_RUNNING = False
COMPLETED_REVISION = 0
NEXT_JOB_ID = 1
DOWNLOAD_LOCK = threading.Lock()


def ensure_default_sqlite_db() -> None:
    """Ensure data/jlc_parts.db exists, is populated, and upgraded to native REAL/INTEGER schema."""
    jlc_fetcher.init_sqlite_db(DEFAULT_DB_PATH)
    try:
        with sqlite3.connect(DEFAULT_DB_PATH) as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        if cnt == 0 and os.path.isfile(DEFAULT_CSV_PATH):
            print("[初始化] 正在將現有 CSV 匯入 SQLite 資料庫 (data/jlc_parts.db)...")
            imported = jlc_fetcher.import_csv_to_sqlite(DEFAULT_CSV_PATH, DEFAULT_DB_PATH)
            print(f"[初始化] 已匯入 {imported} 筆元件至 SQLite。")
        db_engine.ensure_db_schema_and_migrate(DEFAULT_DB_PATH)
    except Exception as e:
        print(f"[Warning] SQLite init check error: {e}", file=sys.stderr)


def list_databases() -> List[Dict[str, Any]]:
    """List available SQLite (.db) and CSV (.csv) database files in data/."""
    files_info = []
    if not os.path.exists(DATA_DIR):
        return files_info

    all_fnames = sorted(
        os.listdir(DATA_DIR),
        key=lambda x: (0 if x.lower().endswith(".db") else 1, x.lower()),
    )
    for fname in all_fnames:
        lower = fname.lower()
        if not (lower.endswith(".db") or lower.endswith(".sqlite") or lower.endswith(".csv")):
            continue
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        stat = os.stat(fpath)
        row_count = 0
        if lower.endswith(".db") or lower.endswith(".sqlite"):
            try:
                with sqlite3.connect(fpath) as conn:
                    row_count = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
            except Exception:
                pass
        else:
            try:
                with open(fpath, "rb") as bf:
                    row_count = max(0, sum(buf.count(b"\n") for buf in iter(lambda: bf.read(1024 * 1024), b"")) - 1)
            except Exception:
                pass
        files_info.append({
            "filename": fname,
            "type": "sqlite" if (lower.endswith(".db") or lower.endswith(".sqlite")) else "csv",
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "row_count": row_count,
            "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return files_info


def resolve_sqlite_path_for_file(fname: str) -> str:
    """If user selected a .csv file, ensure it is imported into a corresponding .db or default .db for SQL queries."""
    fname = os.path.basename(fname or "jlc_parts.db")
    fpath = os.path.join(DATA_DIR, fname)
    if fname.lower().endswith(".db") or fname.lower().endswith(".sqlite"):
        db_engine.ensure_db_schema_and_migrate(fpath)
        return fpath
    if fname == "jlc_parts_db.csv" and os.path.isfile(DEFAULT_DB_PATH):
        return DEFAULT_DB_PATH
    custom_db = fpath + ".sqlite"
    if not os.path.isfile(custom_db) or os.path.getmtime(fpath) > os.path.getmtime(custom_db):
        jlc_fetcher.import_csv_to_sqlite(fpath, custom_db)
        db_engine.ensure_db_schema_and_migrate(custom_db)
    return custom_db


def get_queue_status_snapshot() -> Dict[str, Any]:
    with DOWNLOAD_LOCK:
        queue_copy = [dict(j) for j in DOWNLOAD_QUEUE]
        running_job = next((j for j in queue_copy if j["status"] == "running"), None)
        queued_count = sum(1 for j in queue_copy if j["status"] == "queued")
        completed_count = sum(1 for j in queue_copy if j["status"] == "completed")
        return {
            "worker_running": QUEUE_WORKER_RUNNING,
            "queued_count": queued_count,
            "completed_count": completed_count,
            "completed_revision": COMPLETED_REVISION,
            "current_job": running_job,
            "queue": queue_copy,
        }


def _queue_worker_loop() -> None:
    global QUEUE_WORKER_RUNNING, COMPLETED_REVISION
    try:
        importlib.reload(jlc_fetcher)
        importlib.reload(db_engine)
    except Exception:
        pass

    while True:
        with DOWNLOAD_LOCK:
            next_job = next((j for j in DOWNLOAD_QUEUE if j["status"] == "queued"), None)
            if not next_job:
                QUEUE_WORKER_RUNNING = False
                return
            next_job["status"] = "running"
            next_job["started_at"] = time.strftime("%H:%M:%S")
            job_id = next_job["id"]
            target = next_job["url"]
            fname = next_job["filename"]
            out_path = os.path.join(DATA_DIR, fname)
            in_stock_only = next_job["in_stock_only"]
            library_filter = next_job["library_filter"]
            pcba_filter = next_job["pcba_filter"]
            max_items = next_job["max_items"]
            merge_existing = next_job["merge_existing"]

        def progress_cb(info: Dict[str, Any]):
            with DOWNLOAD_LOCK:
                for j in DOWNLOAD_QUEUE:
                    if j["id"] == job_id:
                        j["page"] = info.get("page", j.get("page", 0))
                        j["total_pages"] = info.get("total_pages", j.get("total_pages", 1))
                        j["fetched"] = info.get("fetched_this_run", info.get("fetched", j.get("fetched", 0)))
                        j["target_count"] = info.get("target_count", j.get("target_count", 0))
                        j["total_available"] = info.get("total_available", j.get("total_available", 0))
                        if info.get("label"):
                            j["label"] = info["label"]
                        if info.get("total_in_csv") is not None:
                            j["total_in_db"] = info["total_in_csv"]
                        break

        try:
            summary = jlc_fetcher.download_parts_to_csv(
                target=target,
                output_csv=out_path,
                in_stock_only=in_stock_only,
                library_filter=library_filter,
                pcba_filter=pcba_filter,
                max_items=max_items,
                merge_existing=merge_existing,
                progress_callback=progress_cb,
            )
            db_target = out_path if out_path.lower().endswith((".db", ".sqlite")) else DEFAULT_DB_PATH
            db_engine.ensure_db_schema_and_migrate(db_target)
            with DOWNLOAD_LOCK:
                for j in DOWNLOAD_QUEUE:
                    if j["id"] == job_id:
                        j["status"] = "completed"
                        j["fetched"] = summary.get("fetched_this_run", j.get("fetched", 0))
                        j["total_in_db"] = summary.get("total_in_csv", 0)
                        j["finished_at"] = time.strftime("%H:%M:%S")
                        break
                COMPLETED_REVISION += 1
        except Exception as e:
            with DOWNLOAD_LOCK:
                for j in DOWNLOAD_QUEUE:
                    if j["id"] == job_id:
                        j["status"] = "error"
                        j["error"] = str(e)
                        j["finished_at"] = time.strftime("%H:%M:%S")
                        break


def enqueue_downloads(params: Dict[str, Any]) -> Dict[str, Any]:
    """Add one or more URLs/categories to the download queue and start worker if idle."""
    global QUEUE_WORKER_RUNNING, NEXT_JOB_ID

    raw_urls = params.get("urls")
    if isinstance(raw_urls, list):
        url_list = [str(u).strip() for u in raw_urls if str(u).strip()]
    else:
        raw_text = str(params.get("urls") or params.get("url") or "").strip()
        # Split by newline or semicolon
        url_list = [u.strip() for u in re.split(r"[\r\n;]+", raw_text) if u.strip()]

    if not url_list:
        return {"ok": False, "error": "請至少輸入一個分類網址或關鍵字。"}

    fname = (params.get("filename") or "jlc_parts.db").strip()
    if not (fname.lower().endswith(".db") or fname.lower().endswith(".sqlite") or fname.lower().endswith(".csv")):
        fname += ".db"
    fname = os.path.basename(fname)

    in_stock_only = bool(params.get("in_stock_only", True))
    library_filter = params.get("library_filter", "all")
    pcba_filter = params.get("pcba_filter", "all")
    max_items = int(params.get("max_items", 5000))
    merge_existing = bool(params.get("merge_existing", True))

    cat_map = jlc_fetcher.load_local_category_map()

    added_jobs = []
    with DOWNLOAD_LOCK:
        for u in url_list:
            # Fast preview label from local map if possible
            preview_label = u
            clean_u = u.split("?")[0].split("#")[0]
            m_id = re.search(r"_(\d+)$", clean_u)
            if m_id and m_id.group(1) in cat_map:
                preview_label = f"{cat_map[m_id.group(1)]['name']} ({m_id.group(1)})"
            elif u.isdigit() and u in cat_map:
                preview_label = f"{cat_map[u]['name']} ({u})"

            job = {
                "id": NEXT_JOB_ID,
                "url": u,
                "label": preview_label,
                "filename": fname,
                "in_stock_only": in_stock_only,
                "library_filter": library_filter,
                "pcba_filter": pcba_filter,
                "max_items": max_items,
                "merge_existing": merge_existing,
                "status": "queued",  # queued | running | completed | error
                "page": 0,
                "total_pages": 1,
                "fetched": 0,
                "target_count": max_items,
                "total_available": 0,
                "total_in_db": 0,
                "error": "",
                "added_at": time.strftime("%H:%M:%S"),
                "started_at": "",
                "finished_at": "",
            }
            NEXT_JOB_ID += 1
            DOWNLOAD_QUEUE.append(job)
            added_jobs.append(job)

        should_start_worker = not QUEUE_WORKER_RUNNING
        if should_start_worker:
            QUEUE_WORKER_RUNNING = True

    if should_start_worker:
        t = threading.Thread(target=_queue_worker_loop, daemon=True)
        t.start()

    return {"ok": True, "added_count": len(added_jobs), "state": get_queue_status_snapshot()}


def control_download_queue(params: Dict[str, Any]) -> Dict[str, Any]:
    """Remove a queued job, clear all pending jobs, or clear finished job history."""
    action = params.get("action")
    job_id = params.get("job_id")
    with DOWNLOAD_LOCK:
        if action == "remove" and job_id is not None:
            DOWNLOAD_QUEUE[:] = [
                j for j in DOWNLOAD_QUEUE if not (j["id"] == int(job_id) and j["status"] == "queued")
            ]
        elif action == "clear_pending":
            DOWNLOAD_QUEUE[:] = [j for j in DOWNLOAD_QUEUE if j["status"] != "queued"]
        elif action == "clear_history":
            DOWNLOAD_QUEUE[:] = [j for j in DOWNLOAD_QUEUE if j["status"] in ("queued", "running")]
    return {"ok": True, "state": get_queue_status_snapshot()}


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class JLCRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        if "/api/download/status" in self.path or "/api/query" in self.path:
            return
        super().log_message(fmt, *args)

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/files":
            self.send_json({"files": list_databases()})
            return

        if path == "/api/download/status":
            self.send_json(get_queue_status_snapshot())
            return

        if path == "/api/categories":
            fname = os.path.basename((qs.get("file") or ["jlc_parts.db"])[0])
            db_path = resolve_sqlite_path_for_file(fname)
            try:
                res = jlc_fetcher.get_sqlite_categories(db_path)
                res["filename"] = fname
                res["exists"] = os.path.isfile(db_path)
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e), "total": 0, "categories": [], "catalog": []}, status=500)
            return

        if path == "/api/facets":
            fname = os.path.basename((qs.get("file") or ["jlc_parts.db"])[0])
            cat1 = (qs.get("category_1st") or [""])[0].strip()
            db_path = resolve_sqlite_path_for_file(fname)
            try:
                res = db_engine.get_facets_for_category(db_path, cat1)
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        if path == "/vue.global.prod.js":
            vue_path = os.path.join(STATIC_DIR, "vue.global.prod.js")
            if os.path.isfile(vue_path):
                with open(vue_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(content)
                return

        if path in ("/", "/index.html"):
            index_path = os.path.join(STATIC_DIR, "index.html")
            if os.path.isfile(index_path):
                with open(index_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            params = json.loads(raw)
        except Exception:
            params = {}

        if parsed.path == "/api/query":
            fname = os.path.basename(params.get("file") or "jlc_parts.db")
            db_path = resolve_sqlite_path_for_file(fname)
            try:
                res = db_engine.execute_parts_query(db_path, params, export_all=False)
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e), "rows": []}, status=500)
            return

        if parsed.path == "/api/export":
            fname = os.path.basename(params.get("file") or "jlc_parts.db")
            db_path = resolve_sqlite_path_for_file(fname)
            try:
                res = db_engine.execute_parts_query(db_path, params, export_all=True)
                rows = res.get("rows") or []
                sio = io.StringIO()
                export_cols = [
                    "lcsc_code", "mfr_model", "brand", "package", "library_type", "pcba_type", "stock",
                    "price_1", "price_10", "price_100", "price_tiers", "description",
                    "type_channel", "config_number", "vds", "id_current", "rds_on", "vgs_th",
                    "vgs_max", "qg", "ciss", "coss", "crss", "power_pd", "temp_range",
                    "diode_config", "vr", "io_current", "vf", "vz", "ir_leakage", "trr", "ifsm",
                    "resistance", "temp_coeff", "capacitance",
                    "attributes_summary", "category_1st", "category_2nd", "datasheet_url", "jlcpcb_url",
                ]
                writer = csv.DictWriter(sio, fieldnames=export_cols, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                csv_bytes = ("\ufeff" + sio.getvalue()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(csv_bytes)))
                self.send_header("Content-Disposition", f'attachment; filename="jlc_filtered_{len(rows)}_parts.csv"')
                self.end_headers()
                self.wfile.write(csv_bytes)
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/download":
            res = enqueue_downloads(params)
            self.send_json(res, status=200 if res.get("ok") else 400)
            return

        if parsed.path == "/api/download/control":
            res = control_download_queue(params)
            self.send_json(res, status=200)
            return

        self.send_error(404, "Not Found")


def main():
    parser = argparse.ArgumentParser(description="JLCPCB Offline Parts Search Web Server")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    ensure_default_sqlite_db()

    server = ThreadedHTTPServer((args.host, args.port), JLCRequestHandler)
    url = f"http://{args.host}:{args.port}"
    print("=" * 62)
    print(f"  JLCPCB 離線元件搜尋器 (Vue 3 + SQLite 引擎): {url}")
    print(f"  SQLite 資料庫: {DEFAULT_DB_PATH}")
    print("  按 Ctrl+C 可停止伺服器")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n伺服器已關閉。")
        server.server_close()


if __name__ == "__main__":
    main()
