#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JLCPCB BOM parser & lookup engine (stdlib only).

- Parses .xlsx (also JLC "BOM Matching" exports saved with an .xls extension) and .csv/.tsv BOMs.
- Detects the header row ("Designator" ...) and the LCSC / JLCPCB part-number column automatically.
- Looks every LCSC code up in the local SQLite DB; codes missing locally (or all codes, when
  live refresh is requested) are fetched live from the JLCPCB API. Live results are NOT written
  to the DB, so a BOM lookup never changes local data.
- Summarises Basic / Preferred / true Extended counts (per unique part and per placement).
"""

import csv
import io
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import jlc_fetcher

LCSC_RE = re.compile(r"^C\d{2,}$")
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EXTENDED_FEE_USD = 3.0  # JLCPCB Economic PCBA loading fee per unique (true) Extended part
LIVE_LOOKUP_WORKERS = 6
MAX_LIVE_LOOKUPS = 300


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------
def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return -1
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_xlsx(data: bytes) -> List[List[str]]:
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())

    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{XLSX_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{XLSX_NS}t")))

    # First sheet in workbook order
    sheet_path = None
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.get("Id"): r.get("Target") for r in rels}
        first = wb.find(f"{XLSX_NS}sheets/{XLSX_NS}sheet")
        if first is not None:
            target = rel_map.get(first.get(f"{REL_NS}id"), "")
            target = target.lstrip("/")
            sheet_path = target if target.startswith("xl/") else f"xl/{target}"
    except (KeyError, ET.ParseError):
        pass
    if not sheet_path or sheet_path not in names:
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise ValueError("xlsx 檔案中找不到工作表")
        sheet_path = sheets[0]

    rows: List[List[str]] = []
    for row in ET.fromstring(z.read(sheet_path)).iter(f"{XLSX_NS}row"):
        cells: Dict[int, str] = {}
        for pos, c in enumerate(row.findall(f"{XLSX_NS}c")):
            idx = _col_index(c.get("r", "")) if c.get("r") else pos
            t = c.get("t")
            v = c.find(f"{XLSX_NS}v")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{XLSX_NS}t"))
            else:
                val = v.text if v is not None and v.text is not None else ""
            cells[idx] = val
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
        else:
            rows.append([])
    return rows


def parse_csv_bytes(data: bytes) -> List[List[str]]:
    text = None
    for enc in ("utf-8-sig", "utf-16", "big5", "gbk", "latin-1"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("無法辨識 CSV 編碼")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    return [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def read_table(data: bytes, filename: str) -> List[List[str]]:
    if data[:2] == b"PK":
        return parse_xlsx(data)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("不支援舊版 Excel 97-2003 (.xls) 二進位格式，請另存為 .xlsx 或 .csv 後再上傳")
    return parse_csv_bytes(data)


# ---------------------------------------------------------------------------
# Header / column detection
# ---------------------------------------------------------------------------
def _norm(h: str) -> str:
    return re.sub(r"[\s_#:：.()\-]+", "", (h or "").strip().lower())


def _find_header_row(rows: List[List[str]]) -> int:
    for i, r in enumerate(rows[:40]):
        normed = [_norm(c) for c in r]
        if "designator" in normed or "designators" in normed or "reference" in normed or "references" in normed:
            return i
    # Fallback: first row containing an "lcsc"/"jlcpcb part" header
    for i, r in enumerate(rows[:40]):
        if any("lcsc" in _norm(c) or "jlcpcbpart" in _norm(c) for c in r):
            return i
    raise ValueError("找不到 BOM 標題列（需包含 Designator 或 LCSC / JLCPCB Part # 欄位）")


def _first_col(header: List[str], *candidates: str) -> Optional[int]:
    normed = [_norm(h) for h in header]
    for cand in candidates:
        for i, h in enumerate(normed):
            if h == cand:
                return i
    return None


def _detect_lcsc_col(header: List[str], body: List[List[str]]) -> Optional[int]:
    best, best_ratio = None, 0.0
    width = max([len(header)] + [len(r) for r in body]) if body else len(header)
    for ci in range(width):
        vals = [(r[ci] if ci < len(r) else "").strip() for r in body]
        nonempty = [v for v in vals if v]
        if not nonempty:
            continue
        hits = sum(1 for v in nonempty if LCSC_RE.match(v))
        ratio = hits / len(body)
        hname = _norm(header[ci]) if ci < len(header) else ""
        if "lcsc" in hname or "jlcpcbpart" in hname or "supplierpart" in hname:
            ratio += 0.05  # prefer an explicitly named column on ties
        if hits and ratio > best_ratio:
            best, best_ratio = ci, ratio
    return best


def _cell(r: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(r):
        return ""
    return (r[idx] or "").strip()


def _to_float(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def extract_bom_rows(rows: List[List[str]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    hi = _find_header_row(rows)
    header = rows[hi]
    body = [r for r in rows[hi + 1:] if any((c or "").strip() for c in r)]
    meta = [" ".join(c.strip() for c in r if c and c.strip()) for r in rows[:hi]]
    meta = [m for m in meta if m]

    c_des = _first_col(header, "designator", "designators", "reference", "references", "refdes")
    c_comment = _first_col(header, "comment", "value", "val")
    c_fp = _first_col(header, "footprint", "package")
    c_lcsc = _detect_lcsc_col(header, body)
    c_lib = _first_col(header, "libtype", "librarytype")
    c_qty = _first_col(header, "qty", "quantity")
    c_total = _first_col(header, "totalprice$", "totalprice", "totalpriceusd")
    c_warn = _first_col(header, "warning", "warnings")
    c_match = _first_col(header, "matches", "matchstatus")
    c_mpn = _first_col(header, "part", "mpn", "manufacturerpart", "manufacturerpartnumber")
    c_mfr = _first_col(header, "manufacturer", "mfr")

    items: List[Dict[str, Any]] = []
    for r in body:
        designator = _cell(r, c_des)
        lcsc = _cell(r, c_lcsc).upper()
        if not LCSC_RE.match(lcsc):
            lcsc = ""
        des_list = [d for d in re.split(r"[,\s;]+", designator) if d]
        mpn = _cell(r, c_mpn)
        if mpn.lower() == "no part selected":
            mpn = ""
        items.append({
            "designator": designator,
            "designator_count": len(des_list),
            "comment": _cell(r, c_comment),
            "footprint": _cell(r, c_fp),
            "lcsc_code": lcsc,
            "bom_lib_type": _cell(r, c_lib),
            "bom_qty": _to_float(_cell(r, c_qty)),
            "bom_total_price": _to_float(_cell(r, c_total)),
            "warning": _cell(r, c_warn),
            "match_status": _cell(r, c_match),
            "bom_mpn": mpn,
            "bom_manufacturer": _cell(r, c_mfr),
        })
    return items, meta


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def lookup_local(db_path: str, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not codes:
        return out
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            for i in range(0, len(codes), 500):
                chunk = codes[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(f"SELECT * FROM parts WHERE lcsc_code IN ({ph})", chunk):
                    out[row["lcsc_code"]] = dict(row)
    except sqlite3.Error:
        pass
    return out


def lookup_live_one(code: str) -> Optional[Dict[str, Any]]:
    try:
        _, items = jlc_fetcher.fetch_page({"currentPage": 1, "pageSize": 10, "keyword": code}, retries=2)
    except Exception:
        return None
    for it in items:
        row = jlc_fetcher.parse_component_item(it)
        if (row.get("lcsc_code") or "").upper() == code:
            return row
    return None


def lookup_live(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    codes = codes[:MAX_LIVE_LOOKUPS]
    out: Dict[str, Dict[str, Any]] = {}
    if not codes:
        return out
    with ThreadPoolExecutor(max_workers=LIVE_LOOKUP_WORKERS) as ex:
        for code, row in zip(codes, ex.map(lookup_live_one, codes)):
            if row:
                out[code] = row
    return out


def classify(lib: str) -> str:
    l = (lib or "").strip().lower()
    if l == "basic" or l == "base":
        return "Basic"
    if l == "preferred":
        return "Preferred"
    if l == "extended" or l == "expand":
        return "Extended"
    return "Unknown"


def analyze_bom(data: bytes, filename: str, db_path: str, live: str = "missing") -> Dict[str, Any]:
    """live: 'none' | 'missing' (default) | 'all'."""
    rows = read_table(data, filename)
    items, meta = extract_bom_rows(rows)

    codes = sorted({it["lcsc_code"] for it in items if it["lcsc_code"]})
    local = lookup_local(db_path, codes)
    if live == "all":
        live_targets = codes
    elif live == "missing":
        live_targets = [c for c in codes if c not in local]
    else:
        live_targets = []
    live_rows = lookup_live(live_targets)

    for it in items:
        code = it["lcsc_code"]
        part, source = None, ""
        if code and code in live_rows:
            part, source = live_rows[code], "live"
        elif code and code in local:
            part, source = local[code], "local"
        it["part"] = part
        it["source"] = source
        if not code:
            it["lib_class"] = "Unmatched"
        elif part:
            it["lib_class"] = classify(part.get("library_type"))
        else:
            it["lib_class"] = classify(it["bom_lib_type"]) if it["bom_lib_type"] else "Unknown"
            it["source"] = "bom"
        bom_cls = classify(it["bom_lib_type"]) if it["bom_lib_type"] else ""
        # JLC BOM exports label Preferred parts as "Extended"; flag those so they aren't counted as extended
        it["relabeled"] = bool(bom_cls and part and bom_cls != it["lib_class"])
        # Qty explicitly 0 in a JLC matching export = line not ordered / not placed
        it["excluded"] = it["bom_qty"] is not None and it["bom_qty"] == 0

    # Summary: unique parts and placements per class
    classes = ["Basic", "Preferred", "Extended", "Unknown", "Unmatched"]
    unique: Dict[str, set] = {c: set() for c in classes}
    placements: Dict[str, int] = {c: 0 for c in classes}
    unmatched_lines = 0
    excluded_lines = 0
    for it in items:
        if it["excluded"]:
            excluded_lines += 1
            continue
        cls = it["lib_class"]
        if cls == "Unmatched":
            unmatched_lines += 1
        else:
            unique[cls].add(it["lcsc_code"])
        placements[cls] += it["designator_count"]

    relabeled_codes = sorted({it["lcsc_code"] for it in items if it["relabeled"] and not it["excluded"]})
    summary = {
        "lines": len(items),
        "unique_parts": sum(len(unique[c]) for c in classes if c != "Unmatched"),
        "unique": {c: len(unique[c]) for c in classes if c != "Unmatched"},
        "placements": placements,
        "unmatched_lines": unmatched_lines,
        "excluded_lines": excluded_lines,
        "basic_pref_unique": len(unique["Basic"]) + len(unique["Preferred"]),
        "relabeled_count": len(relabeled_codes),
        "live_looked_up": len(live_targets),
        "live_found": len(live_rows),
        "missing_codes": sorted(c for c in codes if c not in local and c not in live_rows),
        "extended_fee_estimate": len(unique["Extended"]) * EXTENDED_FEE_USD,
        "extended_fee_per_part": EXTENDED_FEE_USD,
    }
    return {"filename": filename, "meta": meta, "items": items, "summary": summary}
