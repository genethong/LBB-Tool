"""
LBB Validation Module — Phase 2
Validates battery replacement claims submitted by operating countries.

Workflow per LBB row (one row = one claimed battery fault at a specific site):

  1. Specific Incident Check
     - Find the NetEco outage event closest to the reported Mains Fail Time
       Match tolerance: ≤5 min exact, ≤30 min loose (accounts for 15-min NMS polling interval)
     - Compare Actual Backup Time vs Considered BB Modality (BBUT SLA)
     - Check whether Avg Discharge Power exceeded Rectifier Design Load
       (overload = short backup may be load-driven, not battery degradation)

  2. Historical Performance Check  (90-day window, split into 30d + full-90d views)
     - Fetch all outage events at the site for the past 90 days
     - Filter to the claiming MNO's tenant system
     - Surface top 3 longest backup events per window
     - If even the best events never reached SLA → battery is consistently weak
     - If strong events exist → the reported incident may be an anomaly

Rows with no Mains Fail Time are silently skipped (capacity upgrades, not battery faults).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd

log = logging.getLogger("neteco_tool")

_BDT = timezone(timedelta(hours=6))   # Bangladesh Standard Time = UTC+6

# ── Incident search window ────────────────────────────────────────────────────
# The window around Mains Fail Time to search for the matching outage event.
# Pre:  12 h before MFT — provides AC baseline AND captures any prior outage
#       within the recharge concern window (see RECHARGE_MIN_HRS below) at
#       zero extra API cost.
# Post:  6 h after MFT — sufficient for 95%+ of outages (most ≤ 4–5 h).
# Total 18 h stays inside the 23 h API chunk limit → exactly 1 API call.
INCIDENT_PRE_MS  = 12 * 3_600_000  # 12 h before reported MFT
INCIDENT_POST_MS =  6 * 3_600_000  #  6 h after  reported MFT

# ── Recharge check ────────────────────────────────────────────────────────────
# Minimum gap between end of a prior outage and start of the next one for the
# battery to be considered fully recharged.
# Lithium batteries charged at 0.2C: 1 / 0.2C = 5 h to fully recharge from
# full depth of discharge.  Sites here use lithium at ≥ 0.2C, so 5 h is the
# threshold.  Flag if gap < RECHARGE_MIN_HRS.
RECHARGE_MIN_HRS = 5

# ── MNO name → canonical tenant key (matches TENANT_COLS in analysis.py) ────
_MNO_KEY_MAP = {
    "robi axiata": "Robi",
    "robi":        "Robi",
    "grameenphone":"GP",
    "grameen":     "GP",
    "gp":          "GP",
    "banglalink":  "BL",
    "bl":          "BL",
    "teletalk":    "TBL",
    "tbl":         "TBL",
    "non-mno":     "Non-MNO",
    "non mno":     "Non-MNO",
}

# ── Match tolerances ─────────────────────────────────────────────────────────
# Reported MFT and NetEco outage start are typically within 5 min of each other
# (same NMS alarm or field report).  15 min = 1 polling interval as outer bound.
# Events further than 15 min are NOT treated as a match — likely a different outage.
MATCH_TIGHT_MS = 5  * 60 * 1000   # ≤5 min  → exact match
MATCH_LOOSE_MS = 15 * 60 * 1000   # ≤15 min → loose match (1 polling interval)

# ── Template column aliases (flexible matching for user-uploaded files) ───────
_COL_ALIASES = {
    "easi_site_id":      ["easi site id", "easi_site_id", "site id", "site_id", "easi"],
    "customer_site_ref": ["customer site reference", "customer_site_reference",
                          "customer site ref", "site ref", "site reference", "site code"],
    "mno_name":          ["mno name for fault", "mno name", "mno_name",
                          "claiming mno", "operator", "mnо"],
    "connected_tenants": ["connected tenant", "connected tenants",
                          "tenants", "tenant"],
    "mains_fail_time":   ["mains fail time", "mains_fail_time",
                          "outage time", "outage date", "fail time"],
    "design_load_kw":    ["rectifier design load", "design load", "design_load",
                          "rectifier load"],
    "bbut_sla_hrs":      ["considered bb modality", "considered bb",
                          "bbut", "bb modality", "backup modality", "sla hrs",
                          "backup hours", "modality"],
}


# ══════════════════════════════════════════════════════════════════════════════
# Excel date-safe reader
# ══════════════════════════════════════════════════════════════════════════════

def _read_excel_dayfirst(file_obj) -> "pd.DataFrame":
    """
    Read an Excel file using openpyxl and correct the MM/DD ↔ DD/MM ambiguity.

    Root cause: Excel auto-converts typed dates using the system locale.  When
    the locale is MM/DD (US) and the user types DD/MM dates, Excel stores the
    wrong serial — e.g. "10/02/2026" (10 Feb) gets stored as 2 Oct 2026.
    This is detectable: those cells carry the number_format "m/d/yy h:mm".

    Fix: for every datetime cell whose number_format starts with "m/d", swap
    day ↔ month.  Cells where day > 12 were never auto-converted (Excel could
    not create a valid month), so they land as plain text strings — those are
    handled correctly by the MM/DD-last fallback in _parse_mft.

    For files where the locale was already DD/MM (format "d/m/yy h:mm"), the
    datetime value is already correct — no swap is applied.
    """
    import openpyxl
    from datetime import datetime as _dt
    from io import BytesIO

    # Read the raw bytes first so openpyxl gets a proper seekable buffer.
    # Flask's FileStorage / Werkzeug streams lack seekable(), which openpyxl needs.
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    raw_bytes = file_obj.read() if callable(getattr(file_obj, "read", None)) else file_obj
    buf = BytesIO(raw_bytes) if not isinstance(raw_bytes, (bytes, bytearray)) else BytesIO(raw_bytes)

    wb  = openpyxl.load_workbook(buf, data_only=True)
    ws  = wb.active

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=False))
    headers    = [c.value for c in header_row]

    data_rows = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        record: dict = {}
        for cell, hdr in zip(row, headers):
            val = cell.value
            # Detect and correct MM/DD-stored datetimes.
            # number_format "m/d/yy h:mm" (or variants like "m/d/yyyy h:mm")
            # always begins with 'm/' when the date was parsed in MM/DD mode.
            # DD/MM locales produce "d/m/yy" (starts with 'd/').
            if isinstance(val, _dt):
                nf = str(cell.number_format).lower()
                if nf.startswith("m/d") or nf.startswith("mm/dd"):
                    # Day and month are swapped — swap them back.
                    # val.day is the original month the user typed (≤12 guaranteed,
                    # otherwise Excel would not have accepted it as a date).
                    try:
                        val = val.replace(month=val.day, day=val.month)
                    except ValueError:
                        # Theoretically impossible given the above guarantee,
                        # but be defensive: leave the value as-is.
                        pass
            record[hdr] = val
        data_rows.append(record)

    return pd.DataFrame(data_rows, columns=headers)


# ══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ══════════════════════════════════════════════════════════════════════════════

def parse_lbb_upload(file_obj) -> tuple[list[dict], list[str]]:
    """
    Parse an uploaded Excel or CSV file into a list of validated LBB row dicts.
    Rows without a Mains Fail Time are silently skipped.

    Returns (rows, warnings):
        rows     — list of normalised dicts ready for run_lbb_validation()
        warnings — human-readable messages about skipped/invalid rows
    """
    warnings: list[str] = []

    # ── Read file ────────────────────────────────────────────────────────────
    try:
        fname = getattr(file_obj, "filename", "") or ""
        if fname.lower().endswith(".csv"):
            df = pd.read_csv(file_obj)
        else:
            df = _read_excel_dayfirst(file_obj)
    except Exception as exc:
        return [], [f"Cannot read file: {exc}"]

    if df.empty:
        return [], ["Uploaded file is empty."]

    # ── Match columns flexibly ───────────────────────────────────────────────
    # Normalise: strip quotes, whitespace, newlines; lower-case
    raw_cols = list(df.columns)
    norm_cols = [str(c).strip().strip('"').lower().replace("_", " ").replace("\n", " ")
                 for c in raw_cols]

    col_map: dict[str, str] = {}   # field → original column name
    for field, aliases in _COL_ALIASES.items():
        for alias in aliases:
            for orig, norm in zip(raw_cols, norm_cols):
                if alias in norm:
                    col_map[field] = orig
                    break
            if field in col_map:
                break

    missing_required = [f for f in ("easi_site_id", "mains_fail_time", "bbut_sla_hrs")
                        if f not in col_map]
    if missing_required:
        return [], [
            f"Required columns not found: {', '.join(missing_required)}. "
            "Please use the LBB Validation Template."
        ]

    # ── Parse rows ───────────────────────────────────────────────────────────
    rows: list[dict] = []
    for idx, row in df.iterrows():
        row_num = idx + 2   # Excel row number (1-indexed + header)

        # Skip rows with no Mains Fail Time (capacity upgrade, not battery fault)
        mft_raw = row.get(col_map["mains_fail_time"])
        if _is_blank(mft_raw):
            warnings.append(
                f"Row {row_num}: No Mains Fail Time — skipped "
                "(likely capacity upgrade, not battery degradation)"
            )
            continue

        mft_ms = _parse_mft(mft_raw)
        if mft_ms is None:
            warnings.append(f"Row {row_num}: Cannot parse Mains Fail Time '{mft_raw}' — skipped")
            continue

        site_dn     = _str_cell(row, col_map.get("easi_site_id", ""))
        site_ref    = _str_cell(row, col_map.get("customer_site_ref", ""))
        mno_name    = _str_cell(row, col_map.get("mno_name", ""))
        conn_ten    = _str_cell(row, col_map.get("connected_tenants", ""))
        design_kw   = _float_cell(row, col_map.get("design_load_kw", ""))
        bbut_hrs    = _float_cell(row, col_map.get("bbut_sla_hrs", ""))

        if not site_dn:
            warnings.append(f"Row {row_num}: No EASI Site ID — skipped")
            continue

        tenant_key = _mno_to_tenant_key(mno_name)

        rows.append({
            "site_dn":            site_dn,
            "site_ref":           site_ref or site_dn,
            "mno_name":           mno_name,
            "tenant_key":         tenant_key,
            "connected_tenants":  conn_ten,
            "mains_fail_ms":      mft_ms,
            "mains_fail_bdt":     _fmt_bdt(mft_ms),
            "design_load_kw":     design_kw,
            "bbut_sla_hrs":       bbut_hrs,
        })

    if not rows:
        warnings.append("No valid rows found after filtering — nothing to validate.")

    return rows, warnings


def run_lbb_validation(lbb_rows: list[dict], client, ne_tree_mod,
                       mode: str = "quick", hist_days: int = 30) -> dict:
    """
    Main entry point.  Processes sites in parallel (4 workers).

    mode = "quick"  — incident check only (1 API chunk per site, fast for 300+ sites)
    mode = "full"   — incident + last `hist_days` days of history (use for spot checks)
    hist_days       — history window in days, only used when mode="full"
    """
    from analysis import run_backup_analysis
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Pre-resolve site DNs (local DB, no API) — fast
    resolved:   list[tuple[dict, str, str]] = []
    unresolved: list[dict] = []

    for row in lbb_rows:
        node = ne_tree_mod.find_site_by_code(row["site_dn"])
        if not node:
            unresolved.append({
                **_base_result(row),
                "error": (
                    f"Site '{row['site_dn']}' not found in local NE tree. "
                    "Refresh the NE tree or re-upload the CM-MO file."
                ),
            })
        else:
            resolved.append((row, node["dn"], node.get("name", row["site_ref"])))

    def _process(row, actual_dn, actual_name):
        result = _base_result(row)
        result["site_name"] = actual_name
        try:
            result["incident"] = _validate_incident(
                row, actual_dn, client, ne_tree_mod, run_backup_analysis
            )
            if mode == "full":
                now_ms  = _now_ms()
                h_start = now_ms - hist_days * 86_400_000
                log.info("LBB historical %dd: %s", hist_days, actual_name)
                hist    = run_backup_analysis([actual_dn], h_start, now_ms,
                                              client, ne_tree_mod)
                evts    = _filter_by_tenant(hist.get("rows", []), row["tenant_key"])
                result["hist_30d"] = _top3_by_backup(evts)
        except Exception as exc:
            log.exception("LBB error for %s: %s", row["site_ref"], exc)
            result["error"] = str(exc)
        return result

    # Keep original upload order after parallel completion
    order   = {id(r[0]): i for i, r in enumerate(resolved)}
    partial: list[tuple[int, dict]] = []

    if resolved:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_process, row, dn, name): row
                       for row, dn, name in resolved}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    partial.append((order[id(row)], fut.result()))
                except Exception as exc:
                    partial.append((order[id(row)], {**_base_result(row), "error": str(exc)}))

    partial.sort(key=lambda x: x[0])
    results = unresolved + [r for _, r in partial]

    def _status(r): return (r.get("incident") or {}).get("status", "")
    confirmed     = sum(1 for r in results if _status(r) == "CONFIRMED")
    not_conf      = sum(1 for r in results if _status(r) == "NOT_CONFIRMED")
    inconclusive  = sum(1 for r in results if _status(r) == "INCONCLUSIVE")
    no_data       = len(results) - confirmed - not_conf - inconclusive

    return {
        "results":   results,
        "count":     len(results),
        "mode":      mode,
        "hist_days": hist_days if mode == "full" else None,
        "summary": (
            f"Validated {len(results)} site(s) — "
            f"✅ {confirmed} confirmed  ❌ {not_conf} not confirmed  "
            f"🔍 {inconclusive} inconclusive  ⚠️ {no_data} no data/error"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Incident validation
# ══════════════════════════════════════════════════════════════════════════════

def _validate_incident(row: dict, actual_dn: str,
                       client, ne_tree_mod, run_backup_analysis) -> dict:
    """
    Search for an outage event near the reported Mains Fail Time and validate
    the battery backup claim.

    Search window: MFT − INCIDENT_PRE_MS (12 h)  to  MFT + INCIDENT_POST_MS (6 h).
    Total 18 h stays under the 23 h API chunk limit → exactly 1 API call.

    The extended 12 h pre-window serves two purposes at zero extra cost:
      1. Captures the AC baseline needed by the backup analyser.
      2. Exposes any prior outage within the recharge concern window
         (RECHARGE_MIN_HRS = 10 h) so the recharge gap can be calculated
         without an additional API call.
    """
    mft_ms     = row["mains_fail_ms"]
    tenant_key = row["tenant_key"]
    bbut_hrs   = row["bbut_sla_hrs"]
    design_kw  = row["design_load_kw"]

    search_start = mft_ms - INCIDENT_PRE_MS    # 1 h before reported MFT
    search_end   = mft_ms + INCIDENT_POST_MS   # 6 h after  reported MFT

    log.info("LBB Incident: %s — window %s → %s",
             row["site_ref"], _fmt_bdt(search_start), _fmt_bdt(search_end))

    ana_result  = run_backup_analysis(
        [actual_dn], search_start, search_end, client, ne_tree_mod
    )
    all_rows    = ana_result.get("rows", [])
    tenant_rows = _filter_by_tenant(all_rows, tenant_key)

    # ── Fallback: match by Customer Site Reference ───────────────────────────
    # Some sites in NetEco are named by Customer Site Reference (e.g. "JHSDR02")
    # rather than a standard tenant code.  If the primary tenant-key filter
    # returns nothing, try matching against the uploaded site_ref instead.
    site_ref_used = False
    if not tenant_rows and row.get("site_ref"):
        fallback_rows = _filter_by_tenant(all_rows, row["site_ref"])
        if fallback_rows:
            tenant_rows  = fallback_rows
            site_ref_used = True
            log.info(
                "LBB: tenant key '%s' matched nothing for %s — "
                "fell back to site_ref '%s' (%d row(s))",
                tenant_key, row["site_ref"], row["site_ref"], len(tenant_rows),
            )

    if not tenant_rows:
        window_str = (
            f"Search window: {_fmt_bdt(search_start)} → {_fmt_bdt(search_end)} BDT"
        )
        if not all_rows:
            # No events at all in the window — likely wrong date/time or window too narrow
            note = (
                f"No outage events found at this site in the search window. "
                f"{window_str}. "
                "Possible causes: (1) reported Mains Fail Time is incorrect — "
                "check the date in the upload file; "
                "(2) the outage started more than 1 h before the reported MFT — "
                "the event may fall just outside the window; "
                "(3) the NE DN could not be matched — verify the EASI Site ID."
            )
        else:
            # Events exist but none match tenant key or site reference
            found_tenants = sorted({
                str(r.get("Tenant Name") or r.get("Tenant Code") or "Unknown")
                for r in all_rows
            })
            note = (
                f"{len(all_rows)} outage event(s) found in the window, but none "
                f"match tenant '{tenant_key or row['mno_name']}' or site ref "
                f"'{row.get('site_ref', '')}'. "
                f"Tenants found: {', '.join(found_tenants)}. "
                f"{window_str}. "
                "Verify the EASI Site ID and MNO Name in your upload file."
            )
        return {
            "status":          "NO_DATA",
            "status_label":    "⚠️ No Data",
            "note":            note,
            "search_window":   f"{_fmt_bdt(search_start)} → {_fmt_bdt(search_end)}",
            "all_event_count": len(all_rows),
        }

    # ── Find closest event by outage start time ──────────────────────────────
    def _delta(r):
        t = _event_start_ms(r)
        return abs(t - mft_ms) if t else 10 ** 12

    closest   = min(tenant_rows, key=_delta)
    delta_ms  = _delta(closest)
    delta_min = round(delta_ms / 60_000, 1)

    if delta_ms > MATCH_LOOSE_MS:
        return {
            "status":         "NO_MATCH",
            "status_label":   "⚠️ No Match",
            "note":           (
                f"Closest outage found is {delta_min:.0f} min from the reported time — "
                f"exceeds the {MATCH_LOOSE_MS // 60_000}-min loose-match tolerance. "
                "Cannot confirm this specific incident. "
                f"Search window: {_fmt_bdt(search_start)} → {_fmt_bdt(search_end)} BDT."
            ),
            "closest_outage_bdt": closest.get("Outage Start (BDT)", ""),
            "delta_min":          delta_min,
            "search_window":      f"{_fmt_bdt(search_start)} → {_fmt_bdt(search_end)}",
        }

    match_quality = "Exact (≤5 min)" if delta_ms <= MATCH_TIGHT_MS \
                    else f"Loose ({delta_min:.0f} min offset)"
    if site_ref_used:
        match_quality += f" [matched via site ref '{row['site_ref']}']"

    # ── Extract metrics from the matched event ───────────────────────────────
    actual_min  = _to_float(closest.get("Actual Backup Time (min)"))
    actual_hrs  = round(actual_min / 60, 2) if actual_min is not None else None
    outage_min  = _to_float(closest.get("Outage Duration (min)"))
    outage_hrs  = round(outage_min / 60, 2) if outage_min is not None else None
    avg_kw      = _to_float(closest.get("Avg Discharge Power - Total (kW)"))
    min_volt    = _to_float(closest.get("Min Batt Voltage (V)"))
    gen_before      = str(closest.get("Gen Before LLVD", "")).strip().lower()
    llvd_trig       = str(closest.get("LLVD Triggered", "")).strip().lower()
    initial_soc_pct = _to_float(closest.get("Initial SoC (%)"))

    # Load check
    if avg_kw is not None and design_kw:
        if avg_kw > design_kw:
            load_check = f"⚠️ Overloaded — {avg_kw:.2f} kW > {design_kw:.2f} kW design"
        else:
            load_check = f"✅ Within design — {avg_kw:.2f} kW ≤ {design_kw:.2f} kW"
    else:
        load_check = "N/A"

    # ── Verdict logic (first-principles order) ───────────────────────────────
    #
    # Priority:
    #   1. No backup data at all                                   → NO_DATA
    #
    #   2. Short outage (outage < BBUT SLA) — two sub-cases:
    #      a. Min voltage ≤ LLVD_NEAR_V (~48.5 V)
    #         Battery was already at/near exhaustion when AC was restored.
    #         The short outage does NOT exonerate the battery — CONFIRMED.
    #      b. Min voltage > LLVD_NEAR_V (well above threshold)
    #         Battery still had meaningful capacity; the outage simply ended
    #         before the battery was tested.  Cannot confirm degradation.
    #         → INCONCLUSIVE  (manual review / discharge test required)
    #      c. No voltage data
    #         Cannot determine state of charge at outage end → INCONCLUSIVE.
    #
    #   3. Outage ≥ BBUT SLA AND actual backup < SLA               → CONFIRMED
    #      Outage long enough to be a real test; battery ran short.
    #
    #   4. Outage ≥ BBUT SLA AND actual backup ≥ SLA               → NOT CONFIRMED
    #      Battery met the requirement in this event.
    #
    # Caveat: generator arriving before LLVD artificially ends backup time —
    # flag this regardless of verdict.

    # Voltage threshold: at or below this value the battery is considered
    # near-exhausted (LLVD region ≈ 47.5–48 V; 48.5 V gives a small margin).
    LLVD_NEAR_V = 48.5

    # Generator-before-LLVD caveat (re-used across multiple verdicts)
    gen_caveat = (
        " ⚠️ Note: generator was connected before LLVD — this may have "
        "shortened the recorded backup time; actual battery capacity could "
        "be higher than reported."
    ) if gen_before in ("yes", "true", "1") else ""

    if actual_hrs is None:
        verdict       = "NO_DATA"
        verdict_label = "⚠️ No Data"
        verdict_note  = "Backup time could not be determined for this event."

    elif bbut_hrs and outage_hrs is not None and outage_hrs < bbut_hrs:
        # ── Short outage: use voltage to decide ──────────────────────────────
        short_prefix = (
            f"Outage lasted only {outage_hrs:.2f} h "
            f"(< SLA {bbut_hrs:.0f} h). "
        )

        if min_volt is not None and min_volt <= LLVD_NEAR_V:
            # Voltage already near/at LLVD despite the short outage →
            # battery was effectively exhausted: valid claim.
            verdict       = "CONFIRMED"
            verdict_label = "✅ Claim Confirmed"
            verdict_note  = (
                short_prefix
                + f"However, battery voltage dropped to {min_volt:.1f} V "
                f"(≤ {LLVD_NEAR_V} V — near LLVD threshold of ~47.5 V), "
                "indicating the battery was already at/near exhaustion when "
                "AC was restored. Claim is valid despite the short outage."
                + gen_caveat
            )
        else:
            # Voltage was still healthy, or unknown — cannot confirm.
            if min_volt is not None:
                volt_note = (
                    f" Battery voltage remained at {min_volt:.1f} V "
                    f"(> {LLVD_NEAR_V} V) — battery still had capacity remaining."
                )
            else:
                volt_note = (
                    " No voltage data available to assess battery state of charge "
                    "at the time AC was restored."
                )
            verdict       = "INCONCLUSIVE"
            verdict_label = "🔍 Inconclusive — Short Outage"
            verdict_note  = (
                short_prefix
                + "Grid was restored before the battery could be fully tested."
                + volt_note
                + " Manual review recommended — request a longer outage record "
                "or a scheduled discharge test."
            )

    elif bbut_hrs and actual_hrs < bbut_hrs:
        # Long enough outage; battery genuinely ran short.
        verdict       = "CONFIRMED"
        verdict_label = "✅ Claim Confirmed"
        verdict_note  = (
            f"Outage lasted {outage_hrs:.2f} h (≥ SLA), "
            f"but battery only backed up {actual_hrs:.2f} h "
            f"(< SLA {bbut_hrs:.0f} h)."
            + gen_caveat
        )

    else:
        verdict       = "NOT_CONFIRMED"
        verdict_label = "❌ Not Confirmed"
        verdict_note  = (
            f"Battery backed up {actual_hrs:.2f} h ≥ SLA {bbut_hrs:.0f} h — "
            "the battery met the backup requirement in this event."
        )

    # ── Recharge gap check ───────────────────────────────────────────────────
    # The pre-window already covers 12 h before MFT, so any prior outage within
    # the recharge concern window is already present in tenant_rows / all_rows.
    # Find the most recent event whose end time precedes the matched incident's
    # start, then calculate how long the battery had to recharge.
    recharge = _check_recharge_gap(
        prior_candidates  = [r for r in tenant_rows if r is not closest],
        incident_start_ms = _event_start_ms(closest) or mft_ms,
        initial_soc_pct   = initial_soc_pct,
        bbut_sla_hrs      = bbut_hrs,
    )

    return {
        "status":              verdict,
        "status_label":        verdict_label,
        "note":                verdict_note,
        "matched_outage_bdt":  closest.get("Outage Start (BDT)", ""),
        "outage_end_bdt":      closest.get("Outage End (BDT)", ""),
        "delta_min":           delta_min,
        "match_quality":       match_quality,
        "outage_duration_min": outage_min,
        "outage_duration_hrs": outage_hrs,
        "actual_backup_min":   actual_min,
        "actual_backup_hrs":   actual_hrs,
        "avg_discharge_kw":    avg_kw,
        "load_check":          load_check,
        "generator_detected":  closest.get("Generator Detected", ""),
        "gen_before_llvd":     closest.get("Gen Before LLVD", ""),
        "llvd_triggered":      closest.get("LLVD Triggered", ""),
        "min_batt_volt":       min_volt,
        "batt_volt_at_llvd":   _to_float(closest.get("Batt Voltage at LLVD (V)")),
        "initial_soc_pct":     initial_soc_pct,
        "llvd_method":         closest.get("LLVD Detection Method", ""),
        "result_label":        closest.get("Result", ""),
        "recharge":            recharge,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Recharge gap helper
# ══════════════════════════════════════════════════════════════════════════════

def _check_recharge_gap(prior_candidates: list[dict],
                        incident_start_ms: int,
                        initial_soc_pct: Optional[float] = None,
                        bbut_sla_hrs: Optional[float] = None) -> dict:
    """
    4-tier SoC-aware recharge gap assessment.

    Tier 1 — BMS SoC direct (most accurate):
        initial_soc_pct is available from the BMS signal on the incident event.
        Tells us exactly what charge level the battery had when the outage began —
        regardless of whether that was caused by a recharge gap, chronic undercharge,
        or capacity fade.  A SoC ≥ 95% means the battery entered the outage fully
        charged; recharge gap is not the issue.

    Tier 2 — DoD-proportional estimate (no SoC signal):
        Estimate how much the prior outage discharged the battery as a fraction of
        the BBUT SLA, then calculate whether the gap was long enough to recover that
        DoD at the 0.2C charge rate (= RECHARGE_MIN_HRS for 100 % DoD).

    Tier 3 — Gap only (no SoC, no reliable DoD data):
        Simple threshold: flag if gap < RECHARGE_MIN_HRS.

    Tier 4 — No prior outage in the look-back window:
        Battery had 12 h+ to recharge; any charge deficit has a different cause.
    """

    # ── Tier 4 / Tier 1 (no prior outage) ───────────────────────────────────
    if not prior_candidates:
        if initial_soc_pct is not None:
            # BMS data available even without a prior outage in window
            fully_charged = initial_soc_pct >= 95
            return {
                "found":           False,
                "tier":            1,
                "initial_soc_pct": initial_soc_pct,
                "flag":            not fully_charged and initial_soc_pct < 90,
                "note": (
                    f"No prior outage in the {INCIDENT_PRE_MS // 3_600_000} h look-back window. "
                    f"BMS confirms battery was at {initial_soc_pct:.0f}% SoC at incident start — "
                    + (
                        "fully charged. Recharge gap is not a contributing factor."
                        if fully_charged else
                        "battery was not at full charge. This is not a recharge gap issue — "
                        "likely chronic undercharge or capacity fade. Review charging system settings."
                    )
                ),
            }
        return {
            "found": False,
            "tier":  4,
            "flag":  False,
            "note": (
                f"No prior outage detected within the {INCIDENT_PRE_MS // 3_600_000} h "
                "look-back window — battery had sufficient time to recharge."
            ),
        }

    # ── Find most recent prior outage that ended before the incident ─────────
    def _end_ms(r) -> Optional[int]:
        return _parse_bdt_str(r.get("Outage End (BDT)", ""))

    candidates_before = [
        r for r in prior_candidates
        if (_end_ms(r) or 0) < incident_start_ms
    ]

    if not candidates_before:
        return {
            "found": False,
            "tier":  4,
            "flag":  False,
            "note":  "No completed prior outage found before this incident.",
        }

    prior = max(candidates_before, key=lambda r: _event_start_ms(r) or 0)
    prior_end_ms     = _end_ms(prior)
    prior_start_bdt  = prior.get("Outage Start (BDT)", "—")
    prior_end_bdt    = prior.get("Outage End (BDT)", "—")
    prior_backup_min = _to_float(prior.get("Actual Backup Time (min)"))
    prior_backup_hrs = round(prior_backup_min / 60, 2) if prior_backup_min is not None else None

    if prior_end_ms is None:
        # Prior outage found but end time unknown — limited assessment
        base = {
            "found":            True,
            "prior_start_bdt":  prior_start_bdt,
            "prior_end_bdt":    prior_end_bdt,
            "prior_backup_hrs": prior_backup_hrs,
            "gap_hrs":          None,
        }
        if initial_soc_pct is not None:
            base["tier"] = 1
            base["initial_soc_pct"] = initial_soc_pct
            base["flag"] = initial_soc_pct < 90
            base["note"] = (
                f"Prior outage detected but end time unknown. "
                f"BMS shows battery was at {initial_soc_pct:.0f}% SoC at incident start "
                f"({'fully charged' if initial_soc_pct >= 95 else 'partially charged'})."
            )
        else:
            base["tier"] = 3
            base["flag"] = False
            base["note"] = "Prior outage detected but end time could not be determined — recharge gap unknown."
        return base

    gap_ms  = incident_start_ms - prior_end_ms
    gap_hrs = round(gap_ms / 3_600_000, 1)

    base = {
        "found":            True,
        "prior_start_bdt":  prior_start_bdt,
        "prior_end_bdt":    prior_end_bdt,
        "prior_backup_hrs": prior_backup_hrs,
        "gap_hrs":          gap_hrs,
    }

    # ── Tier 1: BMS SoC directly available ──────────────────────────────────
    if initial_soc_pct is not None:
        fully_charged  = initial_soc_pct >= 95
        shortage_pct   = max(0.0, 100 - initial_soc_pct)
        extra_hrs_need = round(shortage_pct / 100 * RECHARGE_MIN_HRS, 1)

        base["tier"]           = 1
        base["initial_soc_pct"] = initial_soc_pct

        if fully_charged:
            base["flag"] = False
            base["note"] = (
                f"BMS confirms battery entered this incident at {initial_soc_pct:.0f}% SoC — "
                f"fully charged. Prior outage ended {gap_hrs:.1f} h earlier; "
                "sufficient recharge time."
            )
        elif initial_soc_pct >= 90:
            # Nearly full — flag only informational
            base["flag"] = False
            base["note"] = (
                f"Battery was at {initial_soc_pct:.0f}% SoC at incident start — nearly fully "
                f"charged (gap: {gap_hrs:.1f} h since prior outage). "
                "Minor shortfall unlikely to significantly affect backup time."
            )
        else:
            # Meaningfully short of full charge
            base["flag"] = True
            base["note"] = (
                f"⚠️ BMS shows battery was only at {initial_soc_pct:.0f}% SoC at incident start "
                f"(gap: {gap_hrs:.1f} h since prior outage). "
                f"Full recharge at 0.2C requires {RECHARGE_MIN_HRS} h; "
                f"battery was {shortage_pct:.0f}% short — approximately {extra_hrs_need:.1f} h of "
                "charging missed. Insufficient recharge is a contributing factor."
            )
        return base

    # ── Tier 2: Estimate DoD from prior outage duration vs BBUT SLA ─────────
    if bbut_sla_hrs and prior_backup_hrs is not None and prior_backup_hrs > 0:
        prior_dod_pct   = min(100.0, prior_backup_hrs / bbut_sla_hrs * 100)
        needed_hrs      = round(prior_dod_pct / 100 * RECHARGE_MIN_HRS, 1)
        gap_sufficient  = gap_hrs >= needed_hrs

        base["tier"]                = 2
        base["prior_dod_est"]       = round(prior_dod_pct, 1)
        base["needed_recharge_hrs"] = needed_hrs
        base["flag"]                = not gap_sufficient

        if gap_sufficient:
            base["note"] = (
                f"Prior outage discharged ~{prior_dod_pct:.0f}% of capacity "
                f"(backup {prior_backup_hrs:.1f} h vs {bbut_sla_hrs:.0f} h SLA). "
                f"Needed ~{needed_hrs:.1f} h to recharge; gap was {gap_hrs:.1f} h. "
                "✅ Sufficient recharge time — recharge gap is not a contributing factor."
            )
        else:
            base["note"] = (
                f"⚠️ Prior outage discharged ~{prior_dod_pct:.0f}% of capacity "
                f"(backup {prior_backup_hrs:.1f} h vs {bbut_sla_hrs:.0f} h SLA). "
                f"Needed ~{needed_hrs:.1f} h to recharge; only {gap_hrs:.1f} h available. "
                "Battery likely started this incident in a partially discharged state."
            )
        return base

    # ── Tier 3: Gap only (no SoC, no reliable DoD estimate) ─────────────────
    flag = gap_hrs < RECHARGE_MIN_HRS
    base["tier"] = 3
    base["flag"] = flag
    if flag:
        base["note"] = (
            f"⚠️ Only {gap_hrs:.1f} h of recharge time since the previous outage "
            f"(ended {prior_end_bdt}). "
            f"Minimum recommended: {RECHARGE_MIN_HRS} h for full recharge at 0.2C. "
            "The battery may have started this outage in a partially discharged state — "
            "short backup could reflect insufficient recharge rather than battery degradation."
        )
    else:
        base["note"] = (
            f"Sufficient recharge time: {gap_hrs:.1f} h since previous outage "
            f"(ended {prior_end_bdt}) — battery should have been fully recharged."
        )
    return base


# ══════════════════════════════════════════════════════════════════════════════
# Historical helpers
# ══════════════════════════════════════════════════════════════════════════════

def _top3_by_backup(events: list[dict]) -> list[dict]:
    """Return the top 3 events sorted by longest Actual Backup Time."""
    def _key(r):
        v = _to_float(r.get("Actual Backup Time (min)"))
        return v if v is not None else -1.0

    top = sorted(events, key=_key, reverse=True)[:3]
    out = []
    for i, r in enumerate(top, 1):
        bt_min = _to_float(r.get("Actual Backup Time (min)"))
        out.append({
            "rank":             i,
            "outage_start":     r.get("Outage Start (BDT)", ""),
            "backup_min":       bt_min,
            "backup_hrs":       round(bt_min / 60, 2) if bt_min is not None else None,
            "avg_discharge_kw": _to_float(r.get("Avg Discharge Power - Total (kW)")),
            "min_batt_volt":    _to_float(r.get("Min Batt Voltage (V)")),
            "generator":        r.get("Generator Detected", ""),
            "result":           r.get("Result", ""),
        })
    return out


def _filter_by_tenant(rows: list[dict], tenant_key: str) -> list[dict]:
    """Keep rows whose Tenant Name or Tenant Code contains the claiming tenant key."""
    if not tenant_key:
        return rows   # no filter — return all
    tk = tenant_key.lower()
    return [
        r for r in rows
        if tk in str(r.get("Tenant Name", "")).lower()
        or tk in str(r.get("Tenant Code", "")).lower()
    ]


def _event_start_ms(row: dict) -> Optional[int]:
    """Parse 'Outage Start (BDT)' field back to epoch milliseconds."""
    return _parse_bdt_str(row.get("Outage Start (BDT)", ""))


# ══════════════════════════════════════════════════════════════════════════════
# Excel export helpers
# ══════════════════════════════════════════════════════════════════════════════

def flatten_for_excel(validation_result: dict) -> tuple[list, list, list]:
    """
    Flatten nested validation results into three flat lists for Excel export:
      (incident_rows, hist_30d_rows, hist_90d_rows)
    """
    inc_rows:  list[dict] = []
    h30_rows:  list[dict] = []
    h90_rows:  list[dict] = []

    for r in validation_result.get("results", []):
        base = {
            "Site":               r.get("site_name") or r.get("site", ""),
            "EASI Site ID":       r.get("site_dn", ""),
            "Claiming MNO":       r.get("mno_name", ""),
            "Connected Tenants":  r.get("connected_tenants", ""),
            "Reported Outage":    r.get("reported_outage", ""),
            "BBUT SLA (hrs)":     r.get("bbut_sla_hrs", ""),
            "Design Load (kW)":   r.get("design_load_kw", ""),
        }

        inc = r.get("incident") or {}
        inc_rows.append({
            **base,
            "Verdict":              inc.get("status_label", r.get("error", "ERROR")),
            "Verdict Note":         inc.get("note", ""),
            "Matched Outage (BDT)": inc.get("matched_outage_bdt", ""),
            "Time Delta (min)":     inc.get("delta_min", ""),
            "Match Quality":        inc.get("match_quality", ""),
            "Outage Duration (min)":inc.get("outage_duration_min", ""),
            "Actual Backup (min)":  inc.get("actual_backup_min", ""),
            "Actual Backup (hrs)":  inc.get("actual_backup_hrs", ""),
            "Avg Discharge (kW)":   inc.get("avg_discharge_kw", ""),
            "Load Check":           inc.get("load_check", ""),
            "Generator Detected":   inc.get("generator_detected", ""),
            "LLVD Triggered":       inc.get("llvd_triggered", ""),
            "Min Batt Voltage (V)": inc.get("min_batt_volt", ""),
            "LLVD Detection Method":inc.get("llvd_method", ""),
        })

        for ev in r.get("hist_30d", []):
            h30_rows.append({**base, **_flatten_hist_event(ev)})
        for ev in r.get("hist_90d", []):
            h90_rows.append({**base, **_flatten_hist_event(ev)})

    return inc_rows, h30_rows, h90_rows


def _flatten_hist_event(ev: dict) -> dict:
    return {
        "Rank":              ev.get("rank", ""),
        "Outage Start (BDT)":ev.get("outage_start", ""),
        "Backup (min)":      ev.get("backup_min", ""),
        "Backup (hrs)":      ev.get("backup_hrs", ""),
        "Avg Discharge (kW)":ev.get("avg_discharge_kw", ""),
        "Min Batt Volt (V)": ev.get("min_batt_volt", ""),
        "Generator":         ev.get("generator", ""),
        "Result":            ev.get("result", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Low-level helpers
# ══════════════════════════════════════════════════════════════════════════════

def _base_result(row: dict) -> dict:
    return {
        "site":             row["site_ref"],
        "site_name":        row["site_ref"],
        "site_dn":          row["site_dn"],
        "mno_name":         row["mno_name"],
        "connected_tenants":row["connected_tenants"],
        "reported_outage":  row["mains_fail_bdt"],
        "bbut_sla_hrs":     row["bbut_sla_hrs"],
        "design_load_kw":   row["design_load_kw"],
        "incident":         None,
        "hist_30d":         [],
        "hist_90d":         [],
        "error":            None,
    }


def _mno_to_tenant_key(mno_name: str) -> str:
    s = (mno_name or "").strip().lower()
    for alias, key in _MNO_KEY_MAP.items():
        if alias in s:
            return key
    return ""


def _parse_mft(raw) -> Optional[int]:
    """Parse a Mains Fail Time cell value → epoch ms (BDT assumed)."""
    if _is_blank(raw):
        return None
    # pandas Timestamp or Python datetime
    if hasattr(raw, "to_pydatetime"):
        raw = raw.to_pydatetime()
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=_BDT)
        return int(raw.timestamp() * 1000)
    s = str(raw).strip()
    for fmt in (
        # ISO first — unambiguous
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
        # DD/MM before MM/DD — Bangladesh standard date format.
        # When day > 12 the DD/MM pattern fails and we fall through to MM/DD.
        "%d/%m/%Y %H:%M",    "%d/%m/%y %H:%M",
        "%m/%d/%Y %H:%M",    "%m/%d/%y %H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=_BDT)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return None


def _parse_bdt_str(s: str) -> Optional[int]:
    """Parse '2025-09-09 16:15 BDT' back to epoch ms."""
    if not s:
        return None
    try:
        clean = s.replace(" BDT", "").strip()
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M").replace(tzinfo=_BDT)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _fmt_bdt(ms) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=_BDT).strftime("%Y-%m-%d %H:%M BDT")
    except Exception:
        return str(ms)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _is_blank(v) -> bool:
    if v is None:
        return True
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return True
    except Exception:
        pass
    return str(v).strip().lower() in ("", "nan", "nat", "none")


def _str_cell(row, col: str) -> str:
    if not col:
        return ""
    v = row.get(col, "")
    return "" if _is_blank(v) else str(v).strip()


def _float_cell(row, col: str) -> Optional[float]:
    if not col:
        return None
    v = row.get(col)
    return _to_float(v)


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        return None if f != f else f   # NaN → None
    except (TypeError, ValueError):
        return None
