#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite High-Performance Query, Facet & Schema Engine for JLCPCB Parts
Executes server-side multi-select filtering, numeric range queries, full-text keyword search,
facet aggregation, and pagination in milliseconds even on 200,000+ components.
"""

import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from jlc_fetcher import parse_number_with_unit

ALL_DB_COLUMNS: List[Tuple[str, str]] = [
    ("lcsc_code", "TEXT PRIMARY KEY"),
    ("mfr_model", "TEXT"),
    ("brand", "TEXT"),
    ("package", "TEXT"),
    ("library_type", "TEXT"),
    ("pcba_type", "TEXT"),
    ("stock", "INTEGER"),
    ("price_1", "REAL"),
    ("price_10", "REAL"),
    ("price_100", "REAL"),
    ("price_tiers", "TEXT"),
    ("description", "TEXT"),
    ("type_channel", "TEXT"),
    ("config_number", "TEXT"),
    ("mos_config", "TEXT"),
    ("vds", "TEXT"),
    ("id_current", "TEXT"),
    ("rds_on", "TEXT"),
    ("vgs_th", "TEXT"),
    ("vgs_max", "TEXT"),
    ("qg", "TEXT"),
    ("ciss", "TEXT"),
    ("coss", "TEXT"),
    ("crss", "TEXT"),
    ("power_pd", "TEXT"),
    ("temp_range", "TEXT"),
    ("diode_config", "TEXT"),
    ("vr", "TEXT"),
    ("io_current", "TEXT"),
    ("vf", "TEXT"),
    ("vz", "TEXT"),
    ("vz_range", "TEXT"),
    ("tolerance", "TEXT"),
    ("ir_leakage", "TEXT"),
    ("trr", "TEXT"),
    ("ifsm", "TEXT"),
    ("zzt", "TEXT"),
    ("zzk", "TEXT"),
    ("resistance", "TEXT"),
    ("temp_coeff", "TEXT"),
    ("capacitance", "TEXT"),
    ("ch_count", "INTEGER"),
    ("vds_num", "REAL"),
    ("id_num", "REAL"),
    ("rdson_mohm", "REAL"),
    ("vgsth_num", "REAL"),
    ("vgs_max_num", "REAL"),
    ("qg_nc", "REAL"),
    ("ciss_pf", "REAL"),
    ("coss_pf", "REAL"),
    ("crss_pf", "REAL"),
    ("power_num", "REAL"),
    ("vr_num", "REAL"),
    ("io_num", "REAL"),
    ("vf_num", "REAL"),
    ("vz_num", "REAL"),
    ("ir_ua", "REAL"),
    ("trr_ns", "REAL"),
    ("ifsm_num", "REAL"),
    ("zzt_num", "REAL"),
    ("zzk_num", "REAL"),
    ("resistance_ohm", "REAL"),
    ("cap_pf", "REAL"),
    ("attributes_summary", "TEXT"),
    ("category_1st", "TEXT"),
    ("category_2nd", "TEXT"),
    ("category_id", "TEXT"),
    ("datasheet_url", "TEXT"),
    ("jlcpcb_url", "TEXT"),
    ("lcsc_url", "TEXT"),
    ("min_order", "INTEGER"),
    ("reel_qty", "INTEGER"),
    ("updated_at", "TEXT"),
]

COLUMN_NAMES = [c[0] for c in ALL_DB_COLUMNS]

# In-memory cache for category facets: key = (db_path, mtime, category_1st)
FACET_CACHE: Dict[Tuple[str, float, str], Dict[str, Any]] = {}

# Category-specific specification definitions shared between Backend SQL & Vue 3 Frontend
CATEGORY_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "Transistors/Thyristors": {
        "title": "Transistors/Thyristors (電晶體 / MOSFET) 專屬電氣規格",
        "specs": [
            {"id": "type_channel", "label": "通道類型 (Type 多選)", "colLabel": "通道/結構", "kind": "select", "dbCol": "type_channel", "showInTable": True},
            {"id": "config_number", "label": "通道數量 / 結構 (Number 多選)", "colLabel": "結構", "kind": "channel_count", "dbCol": "config_number", "showInTable": False},
            {"id": "mos_config", "label": "內部配置 (Configuration 多選)", "colLabel": "配置", "kind": "select", "dbCol": "mos_config", "showInTable": False},
            {"id": "vds", "label": "耐壓 Vds (V)", "colLabel": "Vds", "kind": "range", "dbCol": "vds", "numCol": "vds_num", "showInTable": True, "sortAscDefault": False},
            {"id": "id_cur", "label": "連續電流 Id (A)", "colLabel": "Id", "kind": "range", "dbCol": "id_current", "numCol": "id_num", "showInTable": True, "sortAscDefault": False},
            {"id": "rdson", "label": "導通內阻 Rds(on) (mΩ)", "colLabel": "Rds(on)", "kind": "range", "dbCol": "rds_on", "numCol": "rdson_mohm", "showInTable": True, "sortAscDefault": True},
            {"id": "vgsth", "label": "開啟閾值電壓 Vgs(th) (V)", "colLabel": "Vgs(th)", "kind": "range", "dbCol": "vgs_th", "numCol": "vgsth_num", "showInTable": True, "sortAscDefault": True},
            {"id": "vgsmax", "label": "最大閘源電壓 Vgs(max) (±V)", "colLabel": "Vgs(max)", "kind": "range", "dbCol": "vgs_max", "numCol": "vgs_max_num", "showInTable": True, "sortAscDefault": False},
            {"id": "qg", "label": "閘極電荷 Qg (nC)", "colLabel": "Qg", "kind": "range", "dbCol": "qg", "numCol": "qg_nc", "showInTable": True, "sortAscDefault": True},
            {"id": "ciss", "label": "輸入電容 Ciss (pF)", "colLabel": "Ciss", "kind": "range", "dbCol": "ciss", "numCol": "ciss_pf", "showInTable": True, "sortAscDefault": True},
            {"id": "coss", "label": "輸出電容 Coss (pF)", "colLabel": "Coss", "kind": "range", "dbCol": "coss", "numCol": "coss_pf", "showInTable": True, "sortAscDefault": True},
            {"id": "crss", "label": "反向電容 Crss (pF)", "colLabel": "Crss", "kind": "range", "dbCol": "crss", "numCol": "crss_pf", "showInTable": False, "sortAscDefault": True},
            {"id": "pd", "label": "最大功耗 Pd (W)", "colLabel": "Pd", "kind": "range", "dbCol": "power_pd", "numCol": "power_num", "showInTable": True, "sortAscDefault": False},
            {"id": "temp", "label": "工作溫度 (Operating Temp 多選)", "colLabel": "溫度", "kind": "select", "dbCol": "temp_range", "showInTable": True},
        ],
    },
    "Diodes": {
        "title": "Diodes (二極體) 專屬電氣規格",
        "specs": [
            {"id": "diode_cfg", "label": "二極體結構 / 數量 (Diode Config 多選)", "colLabel": "二極體結構", "kind": "channel_count", "dbCol": "diode_config", "showInTable": True},
            {"id": "diode_type", "label": "整流器類型 (Type 多選)", "colLabel": "類型", "kind": "select", "dbCol": "type_channel", "showInTable": False},
            {"id": "vr", "label": "反向耐壓 Vr (V)", "colLabel": "耐壓 Vr", "kind": "range", "dbCol": "vr", "numCol": "vr_num", "showInTable": True, "sortAscDefault": False},
            {"id": "io", "label": "整流電流 Io (A)", "colLabel": "電流 Io", "kind": "range", "dbCol": "io_current", "numCol": "io_num", "showInTable": True, "sortAscDefault": False},
            {"id": "vf", "label": "順向壓降 Vf (V)", "colLabel": "壓降 Vf", "kind": "range", "dbCol": "vf", "numCol": "vf_num", "showInTable": True, "sortAscDefault": True},
            {"id": "vz", "label": "齊納電壓 Vz(Nom) (V)", "colLabel": "齊納 Vz", "kind": "range", "dbCol": "vz", "numCol": "vz_num", "showInTable": True, "sortAscDefault": True},
            {"id": "vz_range", "label": "齊納電壓範圍 Vz(Range 多選)", "colLabel": "Vz 範圍", "kind": "select", "dbCol": "vz_range", "showInTable": False},
            {"id": "tol", "label": "齊納容差 (Tolerance 多選)", "colLabel": "容差", "kind": "select", "dbCol": "tolerance", "showInTable": False},
            {"id": "ir", "label": "反向漏電流 Ir (μA)", "colLabel": "漏電 Ir", "kind": "range", "dbCol": "ir_leakage", "numCol": "ir_ua", "showInTable": True, "sortAscDefault": True},
            {"id": "trr", "label": "反向恢復時間 trr (ns)", "colLabel": "恢復 trr", "kind": "range", "dbCol": "trr", "numCol": "trr_ns", "showInTable": True, "sortAscDefault": True},
            {"id": "ifsm", "label": "峰值浪湧電流 Ifsm (A)", "colLabel": "浪湧 Ifsm", "kind": "range", "dbCol": "ifsm", "numCol": "ifsm_num", "showInTable": True, "sortAscDefault": False},
            {"id": "zzt", "label": "動態阻抗 Zzt (Ω)", "colLabel": "阻抗 Zzt", "kind": "range", "dbCol": "zzt", "numCol": "zzt_num", "showInTable": False, "sortAscDefault": True},
            {"id": "zzk", "label": "拐點阻抗 Zzk (Ω)", "colLabel": "阻抗 Zzk", "kind": "range", "dbCol": "zzk", "numCol": "zzk_num", "showInTable": False, "sortAscDefault": True},
            {"id": "pd", "label": "最大功耗 Pd (W)", "colLabel": "功耗 Pd", "kind": "range", "dbCol": "power_pd", "numCol": "power_num", "showInTable": True, "sortAscDefault": False},
            {"id": "jtemp", "label": "工作結溫 (Junction Temp 多選)", "colLabel": "結溫", "kind": "select", "dbCol": "temp_range", "showInTable": True},
        ],
    },
    "Resistors": {
        "title": "Resistors (電阻器) 專屬電氣規格",
        "specs": [
            {"id": "res_val", "label": "阻值 Resistance (Ω)", "colLabel": "阻值 (R)", "kind": "range", "dbCol": "resistance", "numCol": "resistance_ohm", "showInTable": True, "sortAscDefault": True},
            {"id": "res_exact", "label": "常用標稱阻值 (Resistance 多選)", "colLabel": "阻值", "kind": "select", "dbCol": "resistance", "showInTable": False},
            {"id": "tol", "label": "容差精度 (Tolerance 多選)", "colLabel": "精度", "kind": "select", "dbCol": "tolerance", "showInTable": True},
            {"id": "pd", "label": "額定功率 Power (W)", "colLabel": "額定功率", "kind": "range", "dbCol": "power_pd", "numCol": "power_num", "showInTable": True, "sortAscDefault": False},
            {"id": "tcr", "label": "溫度係數 (TCR 多選)", "colLabel": "溫漂 TCR", "kind": "select", "dbCol": "temp_coeff", "showInTable": True},
            {"id": "vr", "label": "工作耐壓 Voltage (V)", "colLabel": "最大耐壓", "kind": "range", "dbCol": "vr", "numCol": "vr_num", "showInTable": True, "sortAscDefault": False},
            {"id": "res_type", "label": "電阻類型 (Type 多選)", "colLabel": "電阻類型", "kind": "select", "dbCol": "type_channel", "showInTable": True},
            {"id": "temp", "label": "工作溫度 (Operating Temp 多選)", "colLabel": "工作溫度", "kind": "select", "dbCol": "temp_range", "showInTable": True},
        ],
    },
    "Capacitors": {
        "title": "Capacitors (電容器) 專屬電氣規格",
        "specs": [
            {"id": "cap_val", "label": "容值 Capacitance (pF)", "colLabel": "電容值", "kind": "range", "dbCol": "capacitance", "numCol": "cap_pf", "showInTable": True, "sortAscDefault": True},
            {"id": "cap_exact", "label": "標稱電容值 (Capacitance 多選)", "colLabel": "容值", "kind": "select", "dbCol": "capacitance", "showInTable": False},
            {"id": "vr", "label": "額定耐壓 Voltage (V)", "colLabel": "額定耐壓", "kind": "range", "dbCol": "vr", "numCol": "vr_num", "showInTable": True, "sortAscDefault": False},
            {"id": "tcr", "label": "材質 / 溫度係數 (Dielectric 多選)", "colLabel": "材質", "kind": "select", "dbCol": "temp_coeff", "showInTable": True},
            {"id": "tol", "label": "容差精度 (Tolerance 多選)", "colLabel": "精度", "kind": "select", "dbCol": "tolerance", "showInTable": True},
            {"id": "temp", "label": "工作溫度 (Operating Temp 多選)", "colLabel": "工作溫度", "kind": "select", "dbCol": "temp_range", "showInTable": True},
        ],
    },
}


def compute_channel_or_unit_count(cfg_str: str) -> Optional[int]:
    if not cfg_str or cfg_str == "-" or cfg_str == "-、-":
        return None
    s = cfg_str.lower().strip()
    if s in ("standalone", "independent", "单", "1 independent"):
        return 1
    pair_matches = re.findall(r"(\d+)\s*pair", s)
    if pair_matches:
        return sum(int(m) * 2 for m in pair_matches)
    nums = re.findall(r"\d+", s)
    if not nums:
        return None
    return sum(int(n) for n in nums)


def parse_attrs_from_summary(summary_str: str) -> Dict[str, str]:
    res: Dict[str, str] = {}
    if not summary_str:
        return res
    for pair in summary_str.split(" ; "):
        if ":" in pair:
            k, v = pair.split(":", 1)
            k, v = k.strip(), v.strip()
            if k and v and v != "-":
                res[k] = v
    return res


def enrich_row_dict(r: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all Diode, Transistor, Resistor, Capacitor, and numeric fields are populated on a row dict."""
    attrs = parse_attrs_from_summary(r.get("attributes_summary") or "")

    def ga(*keys: str) -> str:
        for k in keys:
            if k in attrs:
                return attrs[k]
        return ""

    mos_config = r.get("mos_config") or ga("Configuration")
    diode_config = r.get("diode_config") or ga("Diode Configuration")
    vr = r.get("vr") or ga(
        "Voltage - DC Reverse(Vr)",
        "DC Reverse Voltage(Vr)",
        "Voltage-Supply(Max)",
        "Voltage Rating",
        "Voltage - Rated",
        "Rated Voltage",
    )
    io_current = r.get("io_current") or ga("Current - Rectified", "Current Rating")
    vf = r.get("vf") or ga("Voltage - Forward(Vf@If)")
    vz = r.get("vz") or ga("Zener Voltage(Nom)")
    vz_range = r.get("vz_range") or ga("Zener Voltage(Range)")
    tolerance = r.get("tolerance") or ga("Tolerance")
    ir_leakage = r.get("ir_leakage") or ga("Reverse Leakage Current (Ir)")
    trr = r.get("trr") or ga("Reverse Recovery Time (trr)")
    ifsm = r.get("ifsm") or ga("Non-Repetitive Peak Forward Surge Current")
    zzt = r.get("zzt") or ga("Impedance(Zzt)")
    zzk = r.get("zzk") or ga("Impedance(Zzk)")
    resistance = r.get("resistance") or ga("Resistance")
    temp_coeff = r.get("temp_coeff") or ga("Temperature Coefficient", "Temperature Coefficient / Dielectric")
    capacitance = r.get("capacitance") or ga("Capacitance")

    temp_range = r.get("temp_range") or ga("Operating Temperature", "Operating Junction Temperature Range")
    power_pd = r.get("power_pd") or ga("Pd - Power Dissipation", "Power Dissipation", "Power(Watts)", "Power - Max")
    type_channel = r.get("type_channel") or ga("Type", "Polarity")
    config_number = r.get("config_number") or ga("Number")

    ch_source = diode_config if r.get("category_1st") == "Diodes" else (config_number or diode_config)
    ch_count = compute_channel_or_unit_count(ch_source)

    def to_float(val: Any) -> Optional[float]:
        if val is None or val == "":
            return None
        try:
            return float(val)
        except ValueError:
            return None

    def to_int(val: Any, default: Optional[int] = None) -> Optional[int]:
        if val is None or val == "":
            return default
        try:
            return int(float(val))
        except ValueError:
            return default

    vds_num = to_float(r.get("vds_num"))
    if vds_num is None:
        vds_num = parse_number_with_unit(r.get("vds") or "", 1.0)

    id_num = to_float(r.get("id_num"))
    if id_num is None:
        id_num = parse_number_with_unit(r.get("id_current") or "", 1.0)

    rdson_mohm = to_float(r.get("rdson_mohm"))
    if rdson_mohm is None:
        rdson_mohm = parse_number_with_unit(r.get("rds_on") or "", 1000.0)

    vgsth_num = to_float(r.get("vgsth_num"))
    if vgsth_num is None:
        vgsth_num = parse_number_with_unit(r.get("vgs_th") or "", 1.0)

    vgs_max_num = to_float(r.get("vgs_max_num"))
    if vgs_max_num is None:
        vgs_max_num = parse_number_with_unit(r.get("vgs_max") or "", 1.0)

    qg_nc = to_float(r.get("qg_nc"))
    if qg_nc is None:
        qg_nc = parse_number_with_unit(r.get("qg") or "", 1e9)

    ciss_pf = to_float(r.get("ciss_pf"))
    if ciss_pf is None:
        ciss_pf = parse_number_with_unit(r.get("ciss") or "", 1e12)

    coss_pf = to_float(r.get("coss_pf"))
    if coss_pf is None:
        coss_pf = parse_number_with_unit(r.get("coss") or "", 1e12)

    crss_pf = to_float(r.get("crss_pf"))
    if crss_pf is None:
        crss_pf = parse_number_with_unit(r.get("crss") or "", 1e12)

    power_num = to_float(r.get("power_num"))
    if power_num is None:
        power_num = parse_number_with_unit(power_pd, 1.0)

    vr_num = parse_number_with_unit(vr, 1.0)
    io_num = parse_number_with_unit(io_current, 1.0)
    vf_num = parse_number_with_unit(vf, 1.0)
    vz_num = parse_number_with_unit(vz, 1.0)
    ir_ua = parse_number_with_unit(ir_leakage, 1e6)
    trr_ns = parse_number_with_unit(trr, 1e9)
    ifsm_num = parse_number_with_unit(ifsm, 1.0)
    zzt_num = parse_number_with_unit(zzt, 1.0)
    zzk_num = parse_number_with_unit(zzk, 1.0)
    resistance_ohm = parse_number_with_unit(resistance, 1.0)
    cap_pf = parse_number_with_unit(capacitance, 1e12)

    out = dict(r)
    out.update({
        "stock": to_int(r.get("stock"), 0),
        "price_1": to_float(r.get("price_1")),
        "price_10": to_float(r.get("price_10")),
        "price_100": to_float(r.get("price_100")),
        "min_order": to_int(r.get("min_order"), 1),
        "reel_qty": to_int(r.get("reel_qty"), None),
        "type_channel": type_channel,
        "config_number": config_number,
        "mos_config": mos_config,
        "power_pd": power_pd,
        "temp_range": temp_range,
        "diode_config": diode_config,
        "vr": vr,
        "io_current": io_current,
        "vf": vf,
        "vz": vz,
        "vz_range": vz_range,
        "tolerance": tolerance,
        "ir_leakage": ir_leakage,
        "trr": trr,
        "ifsm": ifsm,
        "zzt": zzt,
        "zzk": zzk,
        "resistance": resistance,
        "temp_coeff": temp_coeff,
        "capacitance": capacitance,
        "ch_count": ch_count,
        "vds_num": vds_num,
        "id_num": id_num,
        "rdson_mohm": rdson_mohm,
        "vgsth_num": vgsth_num,
        "vgs_max_num": vgs_max_num,
        "qg_nc": qg_nc,
        "ciss_pf": ciss_pf,
        "coss_pf": coss_pf,
        "crss_pf": crss_pf,
        "power_num": power_num,
        "vr_num": vr_num,
        "io_num": io_num,
        "vf_num": vf_num,
        "vz_num": vz_num,
        "ir_ua": ir_ua,
        "trr_ns": trr_ns,
        "ifsm_num": ifsm_num,
        "zzt_num": zzt_num,
        "zzk_num": zzk_num,
        "resistance_ohm": resistance_ohm,
        "cap_pf": cap_pf,
        "updated_at": r.get("updated_at") or time.strftime("%Y-%m-%d %H:%M"),
    })
    return out


def ensure_db_schema_and_migrate(db_path: str) -> None:
    """Ensure SQLite table has all typed columns & indexes, and migrate any un-enriched rows."""
    if not os.path.isfile(db_path):
        return
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        existing_info = cur.execute("PRAGMA table_info(parts)").fetchall()
        if not existing_info:
            col_defs = [f"{name} {ctype}" for name, ctype in ALL_DB_COLUMNS]
            cur.execute(f"CREATE TABLE parts ({', '.join(col_defs)})")
            conn.commit()
            return

        existing_cols = {row[1]: row[2].upper() for row in existing_info}
        if "updated_at" not in existing_cols:
            cur.execute("ALTER TABLE parts ADD COLUMN updated_at TEXT")
            default_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(db_path)))
            cur.execute("UPDATE parts SET updated_at = ? WHERE updated_at IS NULL OR updated_at = ''", (default_ts,))
            conn.commit()

        needs_rebuild = ("resistance_ohm" not in existing_cols) or (existing_cols.get("vds_num") == "TEXT")

        if not needs_rebuild:
            return

        print("[SQLite 升級] 正在將資料庫升級為原生 REAL/INTEGER 數值索引結構並解析全分類屬性...")
        t0 = time.perf_counter()
        conn.row_factory = sqlite3.Row
        old_rows = [dict(r) for r in conn.execute("SELECT * FROM parts").fetchall()]

        enriched_tuples = []
        for r in old_rows:
            er = enrich_row_dict(r)
            enriched_tuples.append(tuple(er.get(c) for c in COLUMN_NAMES))

        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS parts_new")
        col_defs = [f"{name} {ctype}" for name, ctype in ALL_DB_COLUMNS]
        cur.execute(f"CREATE TABLE parts_new ({', '.join(col_defs)})")
        placeholders = ", ".join(["?"] * len(COLUMN_NAMES))
        cur.executemany(
            f"INSERT OR REPLACE INTO parts_new ({', '.join(COLUMN_NAMES)}) VALUES ({placeholders})",
            enriched_tuples,
        )
        cur.execute("DROP TABLE parts")
        cur.execute("ALTER TABLE parts_new RENAME TO parts")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat1 ON parts(category_1st)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat1_cat2 ON parts(category_1st, category_2nd)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat1_stock ON parts(category_1st, stock DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pkg ON parts(category_1st, package)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib ON parts(category_1st, library_type)")
        conn.commit()
        FACET_CACHE.clear()
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"[SQLite 升級] 已完成 {len(enriched_tuples)} 筆元件數值化與索引建立 (耗時 {elapsed:.1f} ms)")


def get_facets_for_category(db_path: str, category_1st: str) -> Dict[str, Any]:
    """
    Compute multi-select option lists and counts for a given category_1st using fast SQL GROUP BY,
    cached in memory by (db_path, mtime, category_1st).
    """
    if not os.path.isfile(db_path):
        return {"schema": {"title": "", "specs": []}, "commonFacets": {}, "specFacets": {}}

    mtime = os.path.getmtime(db_path)
    cache_key = (os.path.abspath(db_path), mtime, category_1st or "all")
    if cache_key in FACET_CACHE:
        return FACET_CACHE[cache_key]

    where_cat = "WHERE category_1st = ?" if (category_1st and category_1st != "all") else ""
    params = [category_1st] if (category_1st and category_1st != "all") else []

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        def group_counts(col: str) -> List[Dict[str, Any]]:
            cond = f"{where_cat} {'AND' if where_cat else 'WHERE'} {col} IS NOT NULL AND {col} != '' AND {col} != '-'"
            rows = cur.execute(
                f"SELECT {col}, COUNT(*) as cnt FROM parts {cond} GROUP BY {col} ORDER BY cnt DESC",
                params,
            ).fetchall()
            return [{"value": str(r[0]), "label": str(r[0]), "count": int(r[1])} for r in rows]

        common_facets = {
            "package": group_counts("package"),
            "brand": group_counts("brand"),
            "library": group_counts("library_type"),
            "pcba": group_counts("pcba_type"),
            "cat2": group_counts("category_2nd"),
        }

        schema_cat = category_1st
        if not schema_cat or schema_cat == "all":
            first_row = cur.execute("SELECT category_1st FROM parts LIMIT 1").fetchone()
            schema_cat = first_row[0] if first_row else "Transistors/Thyristors"

        schema_obj = CATEGORY_SCHEMAS.get(schema_cat, CATEGORY_SCHEMAS["Transistors/Thyristors"])
        spec_facets: Dict[str, List[Dict[str, Any]]] = {}

        for spec in schema_obj["specs"]:
            if spec["kind"] == "range":
                continue
            col = spec["dbCol"]
            raw_opts = group_counts(col)
            if spec["kind"] == "channel_count":
                cond_ch = f"{where_cat} {'AND' if where_cat else 'WHERE'} ch_count IS NOT NULL"
                ch_rows = cur.execute(
                    f"SELECT CASE WHEN ch_count >= 3 THEN 3 ELSE ch_count END as cg, COUNT(*) FROM parts {cond_ch} GROUP BY cg",
                    params,
                ).fetchall()
                ch_map = {int(r[0]): int(r[1]) for r in ch_rows if r[0] is not None}
                combined = []
                if ch_map.get(1, 0) > 0:
                    combined.append({"value": "cnt:1", "label": "1 單元 / Single", "count": ch_map[1], "group": "依總通道 / 單元數"})
                if ch_map.get(2, 0) > 0:
                    combined.append({"value": "cnt:2", "label": "2 單元 / Dual", "count": ch_map[2], "group": "依總通道 / 單元數"})
                if ch_map.get(3, 0) > 0:
                    combined.append({"value": "cnt:3+", "label": "3 單元以上 (≥3)", "count": ch_map[3], "group": "依總通道 / 單元數"})
                for o in raw_opts:
                    if o["value"] != "-、-":
                        combined.append({
                            "value": f"cfg:{o['value']}",
                            "label": o["label"],
                            "count": o["count"],
                            "group": "依具體結構選項",
                        })
                spec_facets[spec["id"]] = combined
            else:
                spec_facets[spec["id"]] = raw_opts

    res = {
        "schema": schema_obj,
        "commonFacets": common_facets,
        "specFacets": spec_facets,
    }
    FACET_CACHE[cache_key] = res
    return res


def build_sql_where_and_params(filters: Dict[str, Any]) -> Tuple[str, List[Any], Dict[str, Any]]:
    """Translate frontend filter payload into parameterized SQL WHERE clause."""
    clauses: List[str] = []
    params: List[Any] = []

    cat1 = filters.get("category_1st")
    if cat1 and cat1 != "all":
        clauses.append("category_1st = ?")
        params.append(cat1)

    def add_in_clause(col: str, values: Optional[List[str]]) -> None:
        if not values:
            return
        placeholders = ", ".join(["?"] * len(values))
        clauses.append(f"{col} IN ({placeholders})")
        params.extend(values)

    add_in_clause("package", filters.get("package"))
    add_in_clause("brand", filters.get("brand"))
    add_in_clause("library_type", filters.get("library"))
    add_in_clause("pcba_type", filters.get("pcba"))
    add_in_clause("category_2nd", filters.get("cat2"))

    ds = filters.get("datasheet")
    if ds == "has_pdf":
        clauses.append("datasheet_url IS NOT NULL AND datasheet_url != ''")
    elif ds == "no_pdf":
        clauses.append("(datasheet_url IS NULL OR datasheet_url = '')")

    lcsc_q = (filters.get("lcsc_code") or "").strip()
    if lcsc_q:
        clauses.append("lcsc_code LIKE ?")
        params.append(f"%{lcsc_q}%")

    model_q = (filters.get("mfr_model") or "").strip()
    if model_q:
        clauses.append("mfr_model LIKE ?")
        params.append(f"%{model_q}%")

    desc_q = (filters.get("description") or "").strip()
    if desc_q:
        for tok in desc_q.split():
            clauses.append("(description LIKE ? OR attributes_summary LIKE ?)")
            params.extend([f"%{tok}%", f"%{tok}%"])

    kw_q = (filters.get("keyword") or "").strip()
    if kw_q:
        for tok in kw_q.split():
            if tok.startswith("-") and len(tok) > 1:
                ex = f"%{tok[1:]}%"
                clauses.append(
                    "NOT (lcsc_code LIKE ? OR mfr_model LIKE ? OR brand LIKE ? OR package LIKE ? OR description LIKE ? OR attributes_summary LIKE ?)"
                )
                params.extend([ex, ex, ex, ex, ex, ex])
            else:
                inc = f"%{tok}%"
                clauses.append(
                    "(lcsc_code LIKE ? OR mfr_model LIKE ? OR brand LIKE ? OR package LIKE ? OR description LIKE ? OR attributes_summary LIKE ?)"
                )
                params.extend([inc, inc, inc, inc, inc, inc])

    def add_range(col: str, min_v: Any, max_v: Any) -> None:
        if min_v is not None and min_v != "":
            try:
                clauses.append(f"{col} >= ?")
                params.append(float(min_v))
            except ValueError:
                pass
        if max_v is not None and max_v != "":
            try:
                clauses.append(f"{col} <= ?")
                params.append(float(max_v))
            except ValueError:
                pass

    ranges = filters.get("ranges") or {}
    common_range_map = {
        "stock": "stock",
        "price_1": "price_1",
        "price_10": "price_10",
        "price_100": "price_100",
        "min_order": "min_order",
        "reel_qty": "reel_qty",
    }
    for rk, col in common_range_map.items():
        if rk in ranges:
            add_range(col, ranges[rk].get("min"), ranges[rk].get("max"))

    schema_cat = cat1 if (cat1 and cat1 in CATEGORY_SCHEMAS) else "Transistors/Thyristors"
    schema_obj = CATEGORY_SCHEMAS.get(schema_cat, CATEGORY_SCHEMAS["Transistors/Thyristors"])
    spec_map = {s["id"]: s for s in schema_obj["specs"]}

    spec_ranges = filters.get("specRanges") or {}
    for sid, r_obj in spec_ranges.items():
        spec = spec_map.get(sid)
        if spec and spec.get("numCol"):
            add_range(spec["numCol"], r_obj.get("min"), r_obj.get("max"))

    spec_selects = filters.get("specSelects") or {}
    for sid, sel_list in spec_selects.items():
        if not sel_list:
            continue
        spec = spec_map.get(sid)
        if not spec:
            continue
        col = spec["dbCol"]
        if spec["kind"] == "channel_count":
            or_parts = []
            cfg_vals = []
            for v in sel_list:
                if v == "cnt:1":
                    or_parts.append("ch_count = 1")
                elif v == "cnt:2":
                    or_parts.append("ch_count = 2")
                elif v == "cnt:3+":
                    or_parts.append("ch_count >= 3")
                elif v.startswith("cfg:"):
                    cfg_vals.append(v[4:])
            if cfg_vals:
                ph = ", ".join(["?"] * len(cfg_vals))
                or_parts.append(f"{col} IN ({ph})")
                params.extend(cfg_vals)
            if or_parts:
                clauses.append(f"({' OR '.join(or_parts)})")
        else:
            add_in_clause(col, sel_list)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params, schema_obj


def execute_parts_query(db_path: str, filters: Dict[str, Any], export_all: bool = False) -> Dict[str, Any]:
    """Execute paginated SQLite query and return counts + page rows."""
    if not os.path.isfile(db_path):
        return {"total_in_category": 0, "filtered_count": 0, "basic_pref_count": 0, "rows": []}

    t0 = time.perf_counter()
    where_sql, params, schema_obj = build_sql_where_and_params(filters)

    cat1 = filters.get("category_1st")
    cat_where = "WHERE category_1st = ?" if (cat1 and cat1 != "all") else ""
    cat_params = [cat1] if (cat1 and cat1 != "all") else []

    sort_field = filters.get("sortField") or "stock"
    if sort_field not in COLUMN_NAMES:
        sort_field = "stock"
    sort_asc = bool(filters.get("sortAsc", False))
    dir_sql = "ASC" if sort_asc else "DESC"
    order_sql = f"ORDER BY CASE WHEN {sort_field} IS NULL OR {sort_field} = '' THEN 1 ELSE 0 END ASC, {sort_field} {dir_sql}"

    page = max(1, int(filters.get("page") or 1))
    page_size = max(1, min(1000, int(filters.get("pageSize") or 100)))
    offset = (page - 1) * page_size

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        total_in_cat = cur.execute(f"SELECT COUNT(*) FROM parts {cat_where}", cat_params).fetchone()[0]

        stat_row = cur.execute(
            f"""
            SELECT
                COUNT(*),
                SUM(CASE WHEN library_type IN ('Basic', 'Preferred') THEN 1 ELSE 0 END)
            FROM parts
            {where_sql}
            """,
            params,
        ).fetchone()
        filtered_count = int(stat_row[0] or 0)
        basic_pref_count = int(stat_row[1] or 0)

        if export_all:
            rows = [dict(r) for r in cur.execute(f"SELECT * FROM parts {where_sql} {order_sql}", params).fetchall()]
        else:
            rows = [
                dict(r)
                for r in cur.execute(
                    f"SELECT * FROM parts {where_sql} {order_sql} LIMIT ? OFFSET ?",
                    params + [page_size, offset],
                ).fetchall()
            ]

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "total_in_category": total_in_cat,
        "filtered_count": filtered_count,
        "basic_pref_count": basic_pref_count,
        "rows": rows,
        "query_ms": elapsed_ms,
    }
