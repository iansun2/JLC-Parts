#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JLCPCB Parts Downloader & CSV Database Builder
Supports downloading components by JLCPCB Category URL, Category Name, or Keyword,
including full Description and parsed electrical attributes (Vds, Id, Rds(on), Vgs(th), etc.).
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

API_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin": "https://jlcpcb.com",
    "Referer": "https://jlcpcb.com/parts",
    "Connection": "close",
}

CSV_COLUMNS = [
    "lcsc_code",
    "mfr_model",
    "brand",
    "package",
    "library_type",
    "pcba_type",
    "stock",
    "price_1",
    "price_10",
    "price_100",
    "price_tiers",
    "description",
    "type_channel",
    "config_number",
    "vds",
    "id_current",
    "rds_on",
    "vgs_th",
    "vgs_max",
    "qg",
    "ciss",
    "coss",
    "crss",
    "power_pd",
    "temp_range",
    "vds_num",
    "id_num",
    "rdson_mohm",
    "vgsth_num",
    "vgs_max_num",
    "qg_nc",
    "ciss_pf",
    "coss_pf",
    "crss_pf",
    "power_num",
    "attributes_summary",
    "category_1st",
    "category_2nd",
    "category_id",
    "datasheet_url",
    "jlcpcb_url",
    "lcsc_url",
    "min_order",
    "reel_qty",
]


def parse_number_with_unit(val_str: str, base_multiplier: float = 1.0) -> Optional[float]:
    """
    Extract the primary numeric value (with SI prefix m, u/μ, n, p, k, M) from an attribute string.
    Example:
      '12mΩ@10V' with base_multiplier=1000 (to get mΩ) -> 12.0
      '0.012Ω@10V' with base_multiplier=1000 (to get mΩ) -> 12.0
      '60V' -> 60.0
      '83A' -> 83.0
      '350mA' -> 0.35
    """
    if not val_str or val_str == "-":
        return None
    # Remove @ condition part first (e.g. '@10V', '@4.5V,20A')
    main_part = val_str.split("@")[0].strip()
    # Find numbers with optional prefix
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*([mμunpkM]?)", main_part)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    prefix = m.group(2)
    scale = {
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "μ": 1e-6,
        "m": 1e-3,
        "": 1.0,
        "k": 1e3,
        "M": 1e6,
    }.get(prefix, 1.0)
    return round(abs(num) * scale * base_multiplier, 6)


def load_local_category_map() -> Dict[str, Dict[str, str]]:
    """Load the 923 official JLCPCB category IDs -> exact names map from data/jlc_category_map.json."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    map_path = os.path.join(base_dir, "data", "jlc_category_map.json")
    if os.path.isfile(map_path):
        try:
            with open(map_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def resolve_jlcpcb_url_or_category(
    target: str,
) -> Dict[str, Any]:
    """
    Resolve a JLCPCB category URL (e.g. https://jlcpcb.com/parts/2nd/Capacitors/Capacitor_Networks_Arrays_2151?spm=search),
    a category ID (e.g. '2151'), a category name, or a keyword into exact API filter parameters.
    """
    target = target.strip()
    result = {
        "firstSortName": None,
        "secondSortName": None,
        "keyword": None,
        "category_id": "",
        "label": target,
    }

    cat_map = load_local_category_map()

    # Check if user passed a pure numeric category ID (e.g. '2151' or '2954')
    if target.isdigit() and target in cat_map:
        entry = cat_map[target]
        result["category_id"] = target
        if "/1st/" in entry["href"]:
            result["firstSortName"] = entry["name"]
        else:
            result["secondSortName"] = entry["name"]
        result["label"] = entry["name"]
        return result

    if target.startswith("http://") or target.startswith("https://") or "/parts/" in target:
        clean_url = target.split("?")[0].split("#")[0]
        id_match = re.search(r"_(\d+)(?:[/?#]|$)", clean_url)
        if id_match:
            cid = id_match.group(1)
            result["category_id"] = cid
            # 1) Instant 0ms lookup from local 923-category dictionary
            if cid in cat_map:
                entry = cat_map[cid]
                if "/1st/" in entry["href"]:
                    result["firstSortName"] = entry["name"]
                else:
                    result["secondSortName"] = entry["name"]
                result["label"] = entry["name"]
                return result

        # 2) If not in local map, fetch page JSON-LD BreadcrumbList with retries
        if clean_url.startswith("http"):
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        clean_url,
                        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]},
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        html = resp.read().decode("utf-8", errors="ignore")
                    ld_matches = re.findall(
                        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        html,
                        flags=re.DOTALL,
                    )
                    for ld_raw in ld_matches:
                        try:
                            ld_obj = json.loads(ld_raw)
                            if isinstance(ld_obj, dict) and ld_obj.get("@type") == "BreadcrumbList":
                                elements = ld_obj.get("itemListElement") or []
                                for el in elements:
                                    pos = el.get("position")
                                    raw_name = el.get("name") or ""
                                    name = re.sub(
                                        r"\s*\|\s*JLCPCB.*$",
                                        "",
                                        str(raw_name),
                                        flags=re.IGNORECASE,
                                    ).strip()
                                    if pos == 3 and name:
                                        result["firstSortName"] = name
                                    elif pos == 4 and name:
                                        result["secondSortName"] = name
                        except Exception:
                            pass
                    if result["secondSortName"] or result["firstSortName"]:
                        break
                except Exception:
                    time.sleep(0.8 * (attempt + 1))

        # 3) Fallback to URL slug matching against cat_map or slug formatting
        if not result["secondSortName"] and not result["firstSortName"]:
            path = urllib.parse.urlparse(clean_url).path
            for entry in cat_map.values():
                if entry.get("href") == path:
                    if "/1st/" in path:
                        result["firstSortName"] = entry["name"]
                    else:
                        result["secondSortName"] = entry["name"]
                    break

        if not result["secondSortName"] and not result["firstSortName"]:
            path = urllib.parse.urlparse(clean_url).path
            parts = [p for p in path.split("/") if p]
            if "2nd" in parts:
                idx = parts.index("2nd")
                if idx + 2 < len(parts):
                    raw_2nd = re.sub(r"_\d+$", "", parts[idx + 2])
                    result["secondSortName"] = raw_2nd.replace("_", " ")
                    result["firstSortName"] = parts[idx + 1].replace("_", "/")
            elif "1st" in parts:
                idx = parts.index("1st")
                if idx + 1 < len(parts):
                    raw_1st = re.sub(r"_\d+$", "", parts[idx + 1])
                    result["firstSortName"] = raw_1st.replace("_", "/")

        result["label"] = result["secondSortName"] or result["firstSortName"] or "JLCPCB_Parts"
        return result

    # Default: treat as secondSortName first, and caller can fallback to firstSortName / keyword
    result["secondSortName"] = target
    result["label"] = target
    return result


def parse_component_item(item: Dict[str, Any], default_cat_id: str = "") -> Dict[str, Any]:
    """Convert a single JLCPCB API component dict into a flat CSV row dict."""
    lcsc_code = item.get("componentCode") or ""
    mfr_model = item.get("componentModelEn") or item.get("erpComponentName") or ""
    brand = item.get("componentBrandEn") or ""
    package = item.get("componentSpecificationEn") or ""

    lib_raw = (item.get("componentLibraryType") or "").lower()
    pref_flag = bool(item.get("preferredComponentFlag"))
    if lib_raw == "base":
        library_type = "Basic"
    elif pref_flag:
        library_type = "Preferred"
    else:
        library_type = "Extended"

    prod_type = item.get("componentProductType")
    if prod_type == 1:
        pcba_type = "Economic Only"
    elif prod_type == 2:
        pcba_type = "Standard Only"
    else:
        pcba_type = "Economic & Standard"

    stock = int(item.get("stockCount") or 0)

    # Prices
    prices = item.get("componentPrices") or item.get("buyComponentPrices") or []
    price_1 = ""
    price_10 = ""
    price_100 = ""
    tier_strs = []

    if prices:
        sorted_prices = sorted(prices, key=lambda x: x.get("startNumber", 0))
        for p in sorted_prices:
            start_n = p.get("startNumber", 1)
            val = p.get("productPrice")
            if val is not None:
                tier_strs.append(f"{start_n}+: ${val:.4f}")
                if start_n <= 1 and price_1 == "":
                    price_1 = round(float(val), 4)
                if start_n <= 10:
                    price_10 = round(float(val), 4)
                if start_n <= 100:
                    price_100 = round(float(val), 4)
        if price_1 == "" and sorted_prices[0].get("productPrice") is not None:
            price_1 = round(float(sorted_prices[0]["productPrice"]), 4)
    elif item.get("initialPrice") is not None:
        price_1 = round(float(item["initialPrice"]), 4)

    description = (item.get("describe") or "").strip()

    # Parse attributes array
    attrs_list = item.get("attributes") or []
    attr_map: Dict[str, str] = {}
    attr_pairs: List[str] = []
    for a in attrs_list:
        if not isinstance(a, dict):
            continue
        k = (a.get("attribute_name_en") or "").strip()
        v = (a.get("attribute_value_name") or "").strip()
        if k and v:
            attr_map[k] = v
            if v != "-":
                attr_pairs.append(f"{k}: {v}")

    def get_attr(*keys: str) -> str:
        for k in keys:
            if k in attr_map and attr_map[k] != "-":
                return attr_map[k]
            for ak, av in attr_map.items():
                if ak.lower() == k.lower() and av != "-":
                    return av
        return ""

    type_channel = get_attr("Type", "Polarity", "Channel Type", "FET Type")
    config_number = get_attr("Number", "Configuration")
    vds = get_attr("Drain to Source Voltage", "Drain to Source Voltage (Vdss)", "Vds")
    id_current = get_attr(
        "Current - Continuous Drain(Id)",
        "Current - Continuous Drain (Id) @ 25°C",
        "Id",
    )
    rds_on = get_attr("RDS(on)", "Rds(on) (Max) @ Id, Vgs", "Drain to Source On Resistance")
    vgs_th = get_attr("Gate Threshold Voltage (Vgs(th))", "Vgs(th) (Max) @ Id", "Vgs(th)")
    vgs_max = get_attr("Vgs", "Gate to Source Voltage", "Vgs (Max)")
    qg = get_attr("Gate Charge(Qg)", "Gate Charge (Qg) (Max) @ Vgs")
    ciss = get_attr("Input Capacitance(Ciss)", "Input Capacitance (Ciss) (Max) @ Vds")
    coss = get_attr("Output Capacitance(Coss)")
    crss = get_attr("Reverse Transfer Capacitance (Crss@Vds)", "Reverse Transfer Capacitance(Crss)")
    power_pd = get_attr("Pd - Power Dissipation", "Power Dissipation (Max)", "Power - Max")
    temp_range = get_attr("Operating Temperature")

    # Numeric extractions for fast parametric filtering
    vds_num = parse_number_with_unit(vds, 1.0)
    id_num = parse_number_with_unit(id_current, 1.0)
    rdson_mohm = parse_number_with_unit(rds_on, 1000.0)  # convert Ω -> mΩ
    vgsth_num = parse_number_with_unit(vgs_th, 1.0)
    vgs_max_num = parse_number_with_unit(vgs_max, 1.0)
    qg_nc = parse_number_with_unit(qg, 1e9)  # convert C -> nC
    ciss_pf = parse_number_with_unit(ciss, 1e12)  # convert F -> pF
    coss_pf = parse_number_with_unit(coss, 1e12)  # convert F -> pF
    crss_pf = parse_number_with_unit(crss, 1e12)  # convert F -> pF
    power_num = parse_number_with_unit(power_pd, 1.0)

    # Note: In JLCPCB API response item, 'secondSortName' is the 1st-level parent name
    # and 'firstSortName' or 'componentTypeEn' is the 2nd-level subcategory name.
    cat_1st = item.get("secondSortName") or ""
    cat_2nd = item.get("componentTypeEn") or item.get("firstSortName") or ""

    url_suffix = item.get("urlSuffix") or ""
    jlcpcb_url = f"https://jlcpcb.com/partdetail/{url_suffix}" if url_suffix else f"https://jlcpcb.com/parts/componentSearch?searchTxt={lcsc_code}"
    lcsc_url = item.get("lcscGoodsUrl") or ""
    datasheet_url = item.get("dataManualUrl") or item.get("dataManualOfficialLink") or ""

    return {
        "lcsc_code": lcsc_code,
        "mfr_model": mfr_model,
        "brand": brand,
        "package": package,
        "library_type": library_type,
        "pcba_type": pcba_type,
        "stock": stock,
        "price_1": price_1 if price_1 != "" else "",
        "price_10": price_10 if price_10 != "" else "",
        "price_100": price_100 if price_100 != "" else "",
        "price_tiers": " | ".join(tier_strs),
        "description": description,
        "type_channel": type_channel,
        "config_number": config_number,
        "vds": vds,
        "id_current": id_current,
        "rds_on": rds_on,
        "vgs_th": vgs_th,
        "vgs_max": vgs_max,
        "qg": qg,
        "ciss": ciss,
        "coss": coss,
        "crss": crss,
        "power_pd": power_pd,
        "temp_range": temp_range,
        "vds_num": vds_num if vds_num is not None else "",
        "id_num": id_num if id_num is not None else "",
        "rdson_mohm": rdson_mohm if rdson_mohm is not None else "",
        "vgsth_num": vgsth_num if vgsth_num is not None else "",
        "vgs_max_num": vgs_max_num if vgs_max_num is not None else "",
        "qg_nc": qg_nc if qg_nc is not None else "",
        "ciss_pf": ciss_pf if ciss_pf is not None else "",
        "coss_pf": coss_pf if coss_pf is not None else "",
        "crss_pf": crss_pf if crss_pf is not None else "",
        "power_num": power_num if power_num is not None else "",
        "attributes_summary": " ; ".join(attr_pairs),
        "category_1st": cat_1st,
        "category_2nd": cat_2nd,
        "category_id": default_cat_id,
        "datasheet_url": datasheet_url,
        "jlcpcb_url": jlcpcb_url,
        "lcsc_url": lcsc_url,
        "min_order": item.get("minPurchaseNum") or 1,
        "reel_qty": item.get("encapsulationNumber") or "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


def fetch_page(payload: Dict[str, Any], retries: int = 5) -> Tuple[int, List[Dict[str, Any]]]:
    """Fetch a single page from JLCPCB selectSmtComponentList/v2 API."""
    body = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                if data.get("code") != 200:
                    raise RuntimeError(f"API returned code {data.get('code')}: {data.get('message')}")
                page_info = (data.get("data") or {}).get("componentPageInfo") or {}
                total = int(page_info.get("total") or 0)
                items = page_info.get("list") or []
                return total, items
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch page after {retries} retries: {last_err}")


def init_sqlite_db(db_path: str) -> None:
    """Ensure SQLite database table `parts` and indexes exist."""
    import db_engine
    out_dir = os.path.dirname(os.path.abspath(db_path))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        col_defs = [f"{name} {ctype}" for name, ctype in db_engine.ALL_DB_COLUMNS]
        cur.execute(f"CREATE TABLE IF NOT EXISTS parts ({', '.join(col_defs)})")
        existing_cols = {
            row[1] for row in cur.execute("PRAGMA table_info(parts)").fetchall()
        }
        for name, ctype in db_engine.ALL_DB_COLUMNS:
            if name not in existing_cols:
                cur.execute(f"ALTER TABLE parts ADD COLUMN {name} {ctype}")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat1 ON parts(category_1st)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat1_cat2 ON parts(category_1st, category_2nd)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat1_stock ON parts(category_1st, stock DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pkg ON parts(category_1st, package)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib ON parts(category_1st, library_type)")
        conn.commit()


def upsert_rows_to_sqlite(rows: List[Dict[str, Any]], db_path: str) -> int:
    """Upsert a list of component dicts into SQLite database and return total row count."""
    import db_engine
    init_sqlite_db(db_path)
    placeholders = ", ".join(["?"] * len(db_engine.COLUMN_NAMES))
    col_names = ", ".join(db_engine.COLUMN_NAMES)
    sql = f"INSERT OR REPLACE INTO parts ({col_names}) VALUES ({placeholders})"
    tuples = []
    for r in rows:
        er = db_engine.enrich_row_dict(r)
        tuples.append(tuple(er.get(c) for c in db_engine.COLUMN_NAMES))
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        if tuples:
            cur.executemany(sql, tuples)
            conn.commit()
        total = cur.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    db_engine.FACET_CACHE.clear()
    return int(total)


def import_csv_to_sqlite(csv_path: str, db_path: str) -> int:
    """Import all rows from a CSV file into the SQLite database."""
    if not os.path.isfile(csv_path):
        return 0
    rows = []
    with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("lcsc_code"):
                rows.append(r)
    return upsert_rows_to_sqlite(rows, db_path)


def get_sqlite_categories(db_path: str) -> Dict[str, Any]:
    """
    Return:
    - `categories`: downloaded category_1st and category_2nd counts and last_updated from SQLite
    - `catalog`: full 55 main categories & 868 subcategories from JLCPCB all-electronic-components,
      merged with SQLite counts and last_updated, with downloaded categories sorted first and
      un-downloaded categories (downloaded=False, count=0) placed at the bottom.
    """
    local_map = load_local_category_map()
    name_to_url: Dict[str, str] = {}
    slug_to_c1: Dict[str, Dict[str, Any]] = {}
    c1_by_name: Dict[str, Dict[str, Any]] = {}

    # 1) Parse all 1st-level categories from official JLCPCB map
    for cid, entry in local_map.items():
        nm = (entry.get("name") or "").strip()
        href = entry.get("href") or ""
        if not nm or not href:
            continue
        full_url = f"https://jlcpcb.com{href}"
        name_to_url[nm.lower()] = full_url
        if "/parts/1st/" in href:
            slug = re.sub(r"_\d+$", "", href.split("/parts/1st/")[1])
            c1_obj = {
                "category_1st": nm,
                "category_id": cid,
                "url": full_url,
                "sub_defs": {},  # c2_name -> {category_2nd, category_id, url}
            }
            slug_to_c1[slug] = c1_obj
            c1_by_name[nm] = c1_obj

    # 2) Attach all 2nd-level subcategories to their parent 1st-level category
    for cid, entry in local_map.items():
        nm = (entry.get("name") or "").strip()
        href = entry.get("href") or ""
        if not nm or "/parts/2nd/" not in href:
            continue
        full_url = f"https://jlcpcb.com{href}"
        parent_slug = href.split("/parts/2nd/")[1].split("/")[0]
        if parent_slug in slug_to_c1:
            slug_to_c1[parent_slug]["sub_defs"][nm] = {
                "category_2nd": nm,
                "category_id": cid,
                "url": full_url,
            }

    total = 0
    rows = []
    fallback_ts = ""
    if os.path.isfile(db_path):
        import db_engine
        db_engine.ensure_db_schema_and_migrate(db_path)
        init_sqlite_db(db_path)
        fallback_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(db_path)))
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            total = cur.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
            rows = cur.execute(
                """
                SELECT category_1st, category_2nd, COUNT(*) as cnt, MAX(updated_at) as last_up
                FROM parts
                GROUP BY category_1st, category_2nd
                ORDER BY category_1st ASC, cnt DESC
                """
            ).fetchall()

    # 3) Build downloaded-only `categories` and record DB stats per (c1, c2)
    cat_map: Dict[str, Dict[str, Any]] = {}
    db_stats: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for c1, c2, cnt, last_up in rows:
        c1_name = (c1 or "未分類").strip() or "未分類"
        c2_name = (c2 or "未分類").strip() or "未分類"
        ts = (last_up or "").strip() or fallback_ts

        db_stats.setdefault(c1_name, {})[c2_name] = {
            "count": int(cnt),
            "last_updated": ts,
        }

        if c1_name not in cat_map:
            cat_map[c1_name] = {
                "category_1st": c1_name,
                "count": 0,
                "last_updated": ts,
                "url": name_to_url.get(c1_name.lower(), c1_name),
                "subcategories": [],
            }
        cat_map[c1_name]["count"] += int(cnt)
        if ts > cat_map[c1_name]["last_updated"]:
            cat_map[c1_name]["last_updated"] = ts
        cat_map[c1_name]["subcategories"].append({
            "category_2nd": c2_name,
            "count": int(cnt),
            "last_updated": ts,
            "url": name_to_url.get(c2_name.lower(), c2_name),
        })

    categories = sorted(cat_map.values(), key=lambda x: x["count"], reverse=True)

    # 4) Ensure any DB category not in official map is also included in c1_by_name
    for c1_name, c2_dict in db_stats.items():
        if c1_name not in c1_by_name:
            c1_by_name[c1_name] = {
                "category_1st": c1_name,
                "category_id": "",
                "url": name_to_url.get(c1_name.lower(), c1_name),
                "sub_defs": {},
            }
        for c2_name in c2_dict:
            if c2_name not in c1_by_name[c1_name]["sub_defs"]:
                c1_by_name[c1_name]["sub_defs"][c2_name] = {
                    "category_2nd": c2_name,
                    "category_id": "",
                    "url": name_to_url.get(c2_name.lower(), c2_name),
                }

    # 5) Build full `catalog` (all 55 main categories + 868 subcategories)
    catalog: List[Dict[str, Any]] = []
    for c1_name, c1_def in c1_by_name.items():
        c2_stats_map = db_stats.get(c1_name, {})
        sub_list: List[Dict[str, Any]] = []
        c1_total_cnt = 0
        c1_last_up = ""
        downloaded_sub_cnt = 0

        for c2_name, sdef in c1_def["sub_defs"].items():
            st = c2_stats_map.get(c2_name)
            cnt = st["count"] if st else 0
            last_up = st["last_updated"] if st else ""
            is_dl = cnt > 0
            if is_dl:
                c1_total_cnt += cnt
                downloaded_sub_cnt += 1
                if last_up > c1_last_up:
                    c1_last_up = last_up
            sub_list.append({
                "category_2nd": c2_name,
                "category_id": sdef.get("category_id", ""),
                "count": cnt,
                "downloaded": is_dl,
                "last_updated": last_up,
                "url": sdef.get("url", c2_name),
            })

        # Sort subcategories: downloaded first (by count DESC), then un-downloaded at bottom (alphabetically)
        sub_list.sort(key=lambda s: (0 if s["downloaded"] else 1, -s["count"], s["category_2nd"].lower()))

        catalog.append({
            "category_1st": c1_name,
            "category_id": c1_def.get("category_id", ""),
            "count": c1_total_cnt,
            "downloaded": c1_total_cnt > 0,
            "downloaded_sub_count": downloaded_sub_cnt,
            "total_sub_count": len(sub_list),
            "last_updated": c1_last_up,
            "url": c1_def.get("url", c1_name),
            "subcategories": sub_list,
        })

    # Sort main categories: downloaded first (by count DESC), then un-downloaded at bottom (alphabetically)
    catalog.sort(key=lambda c: (0 if c["downloaded"] else 1, -c["count"], c["category_1st"].lower()))

    return {
        "total": total,
        "categories": categories,
        "catalog": catalog,
    }


def query_sqlite_parts(
    db_path: str,
    category_1st: Optional[str] = None,
    category_2nd: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch parts from SQLite filtered by category_1st and/or category_2nd, ordered by stock DESC."""
    if not os.path.isfile(db_path):
        return []
    init_sqlite_db(db_path)
    where_clauses = []
    params: List[Any] = []
    if category_1st and category_1st != "all":
        where_clauses.append("category_1st = ?")
        params.append(category_1st)
    if category_2nd and category_2nd != "all":
        where_clauses.append("category_2nd = ?")
        params.append(category_2nd)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"SELECT {', '.join(CSV_COLUMNS)} FROM parts {where_sql} ORDER BY stock DESC"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    return rows


def save_rows_to_csv(rows_by_code: Dict[str, Dict[str, Any]], output_csv: str) -> int:
    """Write rows_by_code sorted by stock descending to output_csv."""
    out_dir = os.path.dirname(os.path.abspath(output_csv))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    sorted_rows = sorted(
        rows_by_code.values(),
        key=lambda r: int(r.get("stock") or 0),
        reverse=True,
    )

    with open(output_csv, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_rows)
    return len(sorted_rows)


def download_parts_to_csv(
    target: str = "https://jlcpcb.com/parts/2nd/Transistors_Thyristors/MOSFETs_2954",
    output_csv: str = "data/mosfets_2954.csv",
    in_stock_only: bool = True,
    library_filter: str = "all",  # 'all', 'base', 'preferred'
    pcba_filter: str = "all",     # 'all', 'economic', 'standard'
    max_items: int = 5000,        # 0 = unlimited (all available)
    page_size: int = 200,
    merge_existing: bool = True,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Download JLCPCB parts matching `target` and save/merge into `output_csv`.
    """
    resolved = resolve_jlcpcb_url_or_category(target)
    cat_id = resolved.get("category_id") or ""

    base_payload: Dict[str, Any] = {
        "currentPage": 1,
        "pageSize": page_size,
        "sortMode": "STOCK_SORT",
        "sortASC": "DESC",
    }

    if resolved.get("secondSortName"):
        base_payload["secondSortName"] = resolved["secondSortName"]
    elif resolved.get("firstSortName"):
        base_payload["firstSortName"] = resolved["firstSortName"]
    elif resolved.get("keyword"):
        base_payload["keyword"] = resolved["keyword"]

    if in_stock_only:
        base_payload["presaleType"] = "stock"

    if library_filter == "base":
        base_payload["componentLibraryType"] = "base"
    elif library_filter == "preferred":
        base_payload["preferredComponentFlag"] = True

    if pcba_filter == "economic":
        base_payload["pcbAType"] = 1
    elif pcba_filter == "standard":
        base_payload["pcbAType"] = 2

    # Test page 1
    total_available, first_items = fetch_page(base_payload)

    # Smart fallback sequence if initial category field matched 0 items:
    # 1) Try switching between secondSortName and firstSortName
    if total_available == 0 and "secondSortName" in base_payload:
        val = base_payload.pop("secondSortName")
        base_payload["firstSortName"] = val
        total_available, first_items = fetch_page(base_payload)
        if total_available == 0:
            # 2) Fallback to keyword search
            base_payload.pop("firstSortName", None)
            base_payload["keyword"] = val
            total_available, first_items = fetch_page(base_payload)
    elif total_available == 0 and "firstSortName" in base_payload:
        val = base_payload.pop("firstSortName")
        base_payload["secondSortName"] = val
        total_available, first_items = fetch_page(base_payload)
        if total_available == 0:
            base_payload.pop("secondSortName", None)
            base_payload["keyword"] = val
            total_available, first_items = fetch_page(base_payload)

    target_count = total_available if (max_items <= 0 or max_items > total_available) else max_items
    total_pages = (target_count + page_size - 1) // page_size if target_count > 0 else 0

    is_sqlite_target = output_csv.lower().endswith(".db") or output_csv.lower().endswith(".sqlite")
    db_path = output_csv if is_sqlite_target else os.path.join(os.path.dirname(os.path.abspath(output_csv)), "jlc_parts.db")
    init_sqlite_db(db_path)

    rows_by_code: Dict[str, Dict[str, Any]] = {}

    # Load existing CSV only if target is a .csv file and merge_existing is enabled
    if not is_sqlite_target and merge_existing and os.path.exists(output_csv):
        try:
            with open(output_csv, mode="r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    code = r.get("lcsc_code")
                    if code:
                        rows_by_code[code] = r
        except Exception as e:
            print(f"[Warning] Could not read existing CSV for merge: {e}", file=sys.stderr)

    fetched_count = 0
    new_or_updated = 0

    def process_items(items_batch: List[Dict[str, Any]]) -> bool:
        nonlocal fetched_count, new_or_updated
        batch_rows = []
        keep = True
        for item in items_batch:
            if max_items > 0 and fetched_count >= max_items:
                keep = False
                break
            stock_val = int(item.get("stockCount") or 0)
            if in_stock_only and stock_val <= 0:
                keep = False
                break
            row = parse_component_item(item, default_cat_id=cat_id)
            code = row["lcsc_code"]
            if code:
                rows_by_code[code] = row
                batch_rows.append(row)
                new_or_updated += 1
            fetched_count += 1
        if batch_rows:
            upsert_rows_to_sqlite(batch_rows, db_path)
        return keep

    if first_items:
        keep_going = process_items(first_items)
    else:
        keep_going = False

    if progress_callback:
        progress_callback({
            "status": "running",
            "page": 1,
            "total_pages": max(1, total_pages),
            "fetched": fetched_count,
            "target_count": target_count,
            "total_available": total_available,
            "label": resolved.get("label", ""),
        })

    current_page = 2
    try:
        while keep_going and current_page <= total_pages:
            if max_items > 0 and fetched_count >= max_items:
                break
            page_payload = dict(base_payload)
            page_payload["currentPage"] = current_page
            _, page_items = fetch_page(page_payload)
            if not page_items:
                break
            keep_going = process_items(page_items)
            if progress_callback:
                progress_callback({
                    "status": "running",
                    "page": current_page,
                    "total_pages": total_pages,
                    "fetched": fetched_count,
                    "target_count": target_count,
                    "total_available": total_available,
                    "label": resolved.get("label", ""),
                })
            current_page += 1
            time.sleep(0.25)  # Polite delay to avoid connection resets
    finally:
        if is_sqlite_target:
            with sqlite3.connect(db_path) as conn:
                total_saved = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        else:
            total_saved = save_rows_to_csv(rows_by_code, output_csv)

    summary = {
        "status": "completed",
        "label": resolved.get("label", ""),
        "category_id": cat_id,
        "total_available": total_available,
        "fetched_this_run": fetched_count,
        "total_in_csv": total_saved,
        "output_csv": os.path.abspath(output_csv),
    }
    if progress_callback:
        progress_callback(summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="JLCPCB Parts CSV Database Downloader")
    parser.add_argument(
        "--url",
        "-u",
        default="https://jlcpcb.com/parts/2nd/Transistors_Thyristors/MOSFETs_2954",
        help="JLCPCB category URL, category name (e.g. MOSFETs), or search keyword",
    )
    parser.add_argument(
        "--out",
        "-o",
        default="data/jlc_parts_db.csv",
        help="Output CSV file path (default: data/jlc_parts_db.csv)",
    )
    parser.add_argument(
        "--max",
        "-m",
        type=int,
        default=5000,
        help="Max items to download (0 for all available in-stock items, default: 5000)",
    )
    parser.add_argument(
        "--all-stock",
        action="store_true",
        help="Include 0-stock items as well (default: only In-Stock items)",
    )
    parser.add_argument(
        "--lib",
        choices=["all", "base", "preferred"],
        default="all",
        help="Filter by library type: all, base (Basic), preferred (Preferred)",
    )
    parser.add_argument(
        "--pcba",
        choices=["all", "economic", "standard"],
        default="all",
        help="Filter by PCBA type: all, economic, standard",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing CSV instead of merging by LCSC code",
    )
    args = parser.parse_args()

    def cli_progress(info: Dict[str, Any]):
        if info.get("status") == "running":
            print(
                f"\r[下載中] {info['label']} - 分頁 {info['page']}/{info['total_pages']} | "
                f"已抓取: {info['fetched']}/{info['target_count']} 筆 (總符合: {info['total_available']} 筆)",
                end="",
                flush=True,
            )
        elif info.get("status") == "completed":
            print(
                f"\n[完成] 已儲存至 {info['output_csv']} | "
                f"本次抓取: {info['fetched_this_run']} 筆 | CSV 資料庫總筆數: {info['total_in_csv']} 筆"
            )

    download_parts_to_csv(
        target=args.url,
        output_csv=args.out,
        in_stock_only=not args.all_stock,
        library_filter=args.lib,
        pcba_filter=args.pcba,
        max_items=args.max,
        merge_existing=not args.overwrite,
        progress_callback=cli_progress,
    )


if __name__ == "__main__":
    main()
