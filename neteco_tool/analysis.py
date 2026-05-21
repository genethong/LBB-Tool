"""
Phase 2 — Battery Backup Analysis  (rewritten)
Per-outage analysis using statistical data only.

Algorithm:
  1. Fetch statistic time-series for:
       AC Input Distribution : Active Power #30008,  AC Frequency #30007
       Battery Group         : Charge/Discharge Power #30024, Current #30003, Voltage #30002
  2. Detect data interval per signal (mode of time gaps, ignoring large outage gaps).
  3. Build AC timeline by filling gaps between consecutive AC slots with OUTAGE markers.
     Each AC slot is classified as GRID (freq 49-51 Hz) or GENERATOR (freq outside range).
  4. Group OUTAGE slots (and embedded GENERATOR slots) into discrete outage events.
     An event starts on the first OUTAGE slot and ends when GRID state resumes.
  5. For each outage event:
       a. Confirm battery was discharging (validate real outage).
       b. Detect LLVD:
            Primary  : battery charge/discharge power drops from >1 kW to ≤1 kW.
            Fallback : battery current drops >70 % vs the preceding slot.
       c. If no LLVD detected but a generator slot appeared during the event (AC came
          back as GENERATOR before LLVD threshold) → record as "Gen Before LLVD" and
          set backup time = outage start → first generator arrival.
       d. If no LLVD and no generator → grid restored before battery was depleted →
          record as "AC Restored (No LLVD)" with backup time = full outage duration.
       e. Compute actual backup time (min), min voltage during outage, voltage at
          LLVD/backup end, detection method.
  6. Produce one result row per outage event with BDT timestamps.
"""
from __future__ import annotations
import bisect
import logging
import re
from datetime import datetime, timezone, timedelta
from statistics import mode as _stat_mode
from typing import Optional

log = logging.getLogger("neteco_tool")

# ── Bangladesh Standard Time ───────────────────────────────────────────────
_BDT = timezone(timedelta(hours=6))

# ── Signal IDs ─────────────────────────────────────────────────────────────
# AC Input Distribution
SIG_AC_POWER     = 30008   # Active Power (kW)
SIG_AC_FREQ      = 30007   # AC Frequency (Hz)
SIG_AC_VOLT_L1   = 30001   # Phase L1 Voltage (V)  — statistic
SIG_AC_VOLT_L2   = 30002   # Phase L2 Voltage (V)  — statistic
SIG_AC_VOLT_L3   = 30003   # Phase L3 Voltage (V)  — statistic
# Battery Group
SIG_BATT_POWER   = 30024   # Charge/Discharge Power (kW)
SIG_BATT_CURRENT = 30003   # Current (A)   — same numeric ID as SIG_AC_VOLT_L3 but under Battery NE
SIG_BATT_VOLTAGE = 30002   # Voltage (V)   — same numeric ID as SIG_AC_VOLT_L2 but under Battery NE
# Battery SoC signals — used for recharge assessment in LBB Validation
# #30017 on Battery Group (NE type 32772)    = Remaining Capacity Percent (%)
# #1075916831 on BackBatt/Controller NEs     = Remaining Capacity Percent (%) — fallback
# #1075916851 on Battery Group               = Total Remaining Capacity (Ah)
# Fetch all three; use the first one that returns data.
SIG_BATT_SOC_PCT  = 30017       # Remaining Capacity % — Battery Group
SIG_BATT_SOC_ALT  = 1075916831  # Remaining Capacity % — BackBatt / Controller
SIG_BATT_CAP_AH   = 1075916851  # Total Remaining Capacity (Ah)
# Genset (under "Genset" NE type — historical statistic, suitable for outage matching)
SIG_GENSET_STAT     = 30012  # Running State  (0=Invalid / 1=Stopped / 2=Running)
GENSET_NE_TYPE      = "Genset"
GENSET_RUNNING_VAL  = 2      # ENUM value meaning "Running"
# Branch Group (Intelligent Rect per-tenant DC load tracking)
SIG_BRANCH_POWER    = 30002  # Total DC Load Power (kW) per Branch Group NE
BRANCH_NE_TYPE      = "Branch Group"
# Canonical tenant output columns (fixed order in report)
TENANT_COLS = ["Robi", "GP", "BL", "TBL", "Non-MNO", "Others"]
# Operator-code → canonical column key  (case-insensitive keys handled in lookup)
_TENANT_KEY_MAP = {
    "ROBI01": "Robi", "ROBI": "Robi",
    "GP01":   "GP",   "GP":   "GP",
    "BL01":   "BL",   "BL":   "BL",
    "TBL01":  "TBL",  "TBL":  "TBL",
    "NON-MNO": "Non-MNO", "NON_MNO": "Non-MNO",
}
# Branch Group suffixes that are auxiliary (Cooling, RMS, etc.) → "Others"
_BRANCH_AUX = frozenset({"RMS", "Cooling", "Cooling_Others", "Others"})

# ── Thresholds ─────────────────────────────────────────────────────────────
GRID_FREQ_LOW          = 49.0    # Hz  — below this → generator
GRID_FREQ_HIGH         = 51.0    # Hz  — above this → generator (gensets typically run >51 Hz)
PHASE_VOLTAGE_MIN      = 100.0   # V   — below this a phase is considered absent
                                  #       Grid = 3 phases live; Generator = 1 phase only
LLVD_POWER_KW          = 1.0     # kW  — battery discharge power below this → LLVD triggered
LLVD_CURRENT_DROP_PCT  = 0.70    # 70 % drop in current → LLVD (fallback)
BATT_DISCHARGING_POWER = 0.5     # kW  — minimum to confirm battery is discharging
BATT_DISCHARGING_CURR  = 5.0     # A   — minimum current to confirm discharging
LLVD_VOLT_MAX          = 48.5    # V   — voltage must be ≤ this to confirm fallback LLVD
                                  #       (battery approaching 48–47.5 V LLVD threshold)
DEFAULT_INTERVAL_MS    = 900_000  # 15 min fallback if interval cannot be detected
API_WINDOW_MS          = 23 * 3600 * 1000   # stay under 24 h API limit
MAX_DNS_PER_CALL       = 50


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def run_backup_analysis(site_dns: list, start_ms: int, end_ms: int,
                        client, ne_tree_mod) -> dict:
    """
    Main entry point.  For every site, detect outage events and measure battery
    backup duration.  Returns a dict ready for display and Excel export.
    """
    results: list = []
    errors:  list = []

    for site_dn in site_dns:
        try:
            _analyse_site(site_dn, start_ms, end_ms, client, ne_tree_mod, results, errors)
        except Exception as exc:
            errors.append(f"{site_dn}: unhandled error — {exc}")
            log.exception("Battery analysis unhandled error for %s", site_dn)

    summary = (
        f"Analysed {len(site_dns)} site(s) — "
        f"{len(results)} outage event(s) detected in the selected period."
    )
    if errors:
        summary += f"  ({len(errors)} error(s) — see server log.)"

    return {
        "columns": _result_columns(),
        "rows":    results,
        "summary": summary,
        "count":   len(results),
        "errors":  errors,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Per-site analysis  (groups NEs by tenant, delegates to _analyse_tenant_system)
# ══════════════════════════════════════════════════════════════════════════════

def _analyse_site(site_dn, start_ms, end_ms, client, ne_tree_mod, results, errors):
    from collections import defaultdict

    site_node = ne_tree_mod.get_ne_by_dn(site_dn)
    site_name = site_node["name"] if site_node else site_dn

    # ── 1. Resolve NE lists ──────────────────────────────────────────────────
    batt_map   = ne_tree_mod.get_dns_by_type_under_sites([site_dn], ["Battery Group"])
    batt_nes   = batt_map.get("Battery Group", [])[:MAX_DNS_PER_CALL]

    ac_map     = ne_tree_mod.get_dns_by_type_under_sites([site_dn], ["AC Input Distribution"])
    ac_nes     = ac_map.get("AC Input Distribution", [])[:MAX_DNS_PER_CALL]

    genset_map = ne_tree_mod.get_dns_by_type_under_sites([site_dn], [GENSET_NE_TYPE])
    genset_dns = [ne["dn"] for ne in genset_map.get(GENSET_NE_TYPE, [])[:MAX_DNS_PER_CALL]]

    if not batt_nes:
        errors.append(f"{site_name}: no Battery Group NEs — skipped")
        return
    if not ac_nes:
        errors.append(f"{site_name}: no AC Input Distribution NEs — skipped")
        return

    # ── 2. Resolve tenant for every NE and group ─────────────────────────────
    all_ne_dns = [ne["dn"] for ne in batt_nes + ac_nes]
    ctx_map    = ne_tree_mod.get_ne_context_bulk(all_ne_dns)

    tenant_batt_dns: dict[str, list[str]] = defaultdict(list)
    tenant_ac_dns:   dict[str, list[str]] = defaultdict(list)
    tenant_names:    dict[str, str]       = {}   # tenant_code → human-readable name

    for ne in batt_nes:
        dn  = ne["dn"]
        ctx = ctx_map.get(dn, {})
        tc  = ctx.get("tenant") or "—"
        tenant_batt_dns[tc].append(dn)
        if tc not in tenant_names:
            tenant_names[tc] = ctx.get("tenant_name") or ""

    for ne in ac_nes:
        dn  = ne["dn"]
        ctx = ctx_map.get(dn, {})
        tc  = ctx.get("tenant") or "—"
        tenant_ac_dns[tc].append(dn)
        if tc not in tenant_names:
            tenant_names[tc] = ctx.get("tenant_name") or ""

    log.info("%s: %d tenant system(s) found: %s",
             site_name, len(tenant_batt_dns), list(tenant_batt_dns))

    # ── 3. Resolve Branch Group NEs (Intelligent Rect per-tenant DC load) ───
    # Branch Group NEs are site-wide (not per-tenant-system) and track how much
    # DC load each operator (Robi / GP / BL / …) draws from the shared rectifier.
    # NE name pattern:  Branch Group[N]-TENANTCODE  or  SITE_…_Branch Group[N]-TENANTCODE
    branch_map = ne_tree_mod.get_dns_by_type_under_sites([site_dn], [BRANCH_NE_TYPE])
    branch_by_tenant: dict[str, list[str]] = {k: [] for k in TENANT_COLS}
    for ne in branch_map.get(BRANCH_NE_TYPE, [])[:MAX_DNS_PER_CALL]:
        key = _branch_tenant_key(ne["name"])
        branch_by_tenant[key].append(ne["dn"])

    # Fetch Branch Group power for the full window (once, shared across all outage events)
    branch_power_by_tenant: dict[str, list[tuple[int, float]]] = {}
    all_branch_dns = [dn for dns in branch_by_tenant.values() for dn in dns]
    if all_branch_dns:
        try:
            br_raw = _fetch_chunked(client, all_branch_dns, [SIG_BRANCH_POWER],
                                    start_ms, end_ms)
            branch_power_by_tenant = _extract_branch_by_tenant(br_raw, branch_by_tenant)
            log.info("%s: Branch Group data fetched — tenants with data: %s",
                     site_name,
                     [k for k, v in branch_power_by_tenant.items() if v])
        except Exception as exc:
            log.warning("%s: Branch Group fetch failed — %s", site_name, exc)

    # ── 4. Fetch genset running-state once (genset is site-wide / shared) ────
    # Statistic #30012: 0=Invalid / 1=Stopped / 2=Running
    genset_running_sorted: list[int] = []
    if genset_dns:
        try:
            gen_raw = _fetch_chunked(client, genset_dns, [SIG_GENSET_STAT], start_ms, end_ms)
            gen_s   = _extract_series(gen_raw, SIG_GENSET_STAT)
            genset_running_sorted = sorted(
                t for t, v in gen_s if v is not None and round(v) == GENSET_RUNNING_VAL
            )
            log.info("%s: Genset NE found (%d), %d running slot(s) in window",
                     site_name, len(genset_dns), len(genset_running_sorted))
        except Exception as exc:
            log.warning("%s: genset statistic fetch failed — %s", site_name, exc)

    # ── 4. Analyse each tenant system independently ───────────────────────────
    all_ac_dns   = [ne["dn"] for ne in ac_nes]   # fallback if tenant has no matching AC NEs
    batt_ne_name = {ne["dn"]: ne["name"] for ne in batt_nes}

    for tc, b_dns in tenant_batt_dns.items():
        # Branch Group NEs belong to a specific rectifier system.
        # A site can have both an Intelligent Rect (shared) and a dedicated single-tenant rect.
        # Only pass Branch Group data to the system whose Battery Group NE contains
        # "Intelligent" in its name — i.e. it is the shared intelligent rectifier.
        # For dedicated single-tenant systems (e.g. CMBRM1_…_Int_GP01_Battery Group),
        # Branch Group data from the intelligent rect must NOT be applied.
        is_intelligent = any(
            "intelligent" in batt_ne_name.get(dn, "").lower()
            for dn in b_dns
        )
        branch_data = branch_power_by_tenant if is_intelligent else {}

        if is_intelligent:
            # Build tenant name from Branch Groups that have NEs, in canonical order,
            # excluding auxiliary "Others" bucket.
            # e.g. Robi + BL branches → "Robi, BL"
            op_names = [k for k in TENANT_COLS if k != "Others" and branch_by_tenant.get(k)]
            tn = ", ".join(op_names) if op_names else tenant_names.get(tc, "")
        else:
            tn = tenant_names.get(tc, "") or ""
            if not tn:
                # Try canonical map on tenant code (e.g. "GP01" → "GP")
                tn = _TENANT_KEY_MAP.get(tc) or _TENANT_KEY_MAP.get(tc.upper()) or ""
            if not tn:
                # Last resort: extract from Battery Group NE name
                # (e.g. "CMBRM1_Huawei01_Int_GP01_Battery Group" → "GP")
                for dn in b_dns:
                    derived = _tenant_key_from_ne_name(batt_ne_name.get(dn, ""))
                    if derived:
                        tn = derived
                        break

        a_dns = tenant_ac_dns.get(tc) or all_ac_dns
        _analyse_tenant_system(
            site_dn, site_name,
            tc, tn,
            b_dns, a_dns, genset_running_sorted,
            branch_data,
            start_ms, end_ms, client, results, errors,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Per-tenant-system analysis
# ══════════════════════════════════════════════════════════════════════════════

def _analyse_tenant_system(
    site_dn, site_name,
    tenant_code, tenant_name,
    batt_dns, ac_dns, genset_running_sorted,
    branch_power_by_tenant,
    start_ms, end_ms, client, results, errors,
):
    label = f"{site_name} / {tenant_code}"

    # ── 1. Fetch signal data ─────────────────────────────────────────────────
    try:
        ac_raw   = _fetch_chunked(client, ac_dns,
                                   [SIG_AC_POWER, SIG_AC_FREQ,
                                    SIG_AC_VOLT_L1, SIG_AC_VOLT_L2, SIG_AC_VOLT_L3],
                                   start_ms, end_ms)
        batt_raw = _fetch_chunked(client, batt_dns,
                                   [SIG_BATT_POWER, SIG_BATT_CURRENT, SIG_BATT_VOLTAGE,
                                    SIG_BATT_SOC_PCT, SIG_BATT_SOC_ALT, SIG_BATT_CAP_AH],
                                   start_ms, end_ms)
    except Exception as exc:
        errors.append(f"{label}: data fetch error — {exc}")
        return

    if not ac_raw:
        errors.append(f"{label}: no AC data returned — skipped")
        return

    # ── 2. Build time-series ─────────────────────────────────────────────────
    ac_power_s   = _extract_series(ac_raw,   SIG_AC_POWER)
    ac_freq_s    = _extract_series(ac_raw,   SIG_AC_FREQ)
    ac_volt_l1_s = _extract_series(ac_raw,   SIG_AC_VOLT_L1)
    ac_volt_l2_s = _extract_series(ac_raw,   SIG_AC_VOLT_L2)
    ac_volt_l3_s = _extract_series(ac_raw,   SIG_AC_VOLT_L3)
    batt_power_s = _extract_series(batt_raw, SIG_BATT_POWER)
    batt_curr_s  = _extract_series(batt_raw, SIG_BATT_CURRENT)
    batt_volt_s  = _extract_series(batt_raw, SIG_BATT_VOLTAGE)

    # SoC series: prefer Battery Group % signal (#30017); fall back to
    # BackBatt/Controller alt (#1075916831).  Both return % directly.
    _batt_soc_pct_s = _extract_series(batt_raw, SIG_BATT_SOC_PCT)
    _batt_soc_alt_s = _extract_series(batt_raw, SIG_BATT_SOC_ALT)
    batt_soc_s      = _batt_soc_pct_s if _batt_soc_pct_s else _batt_soc_alt_s
    log.info("%s: SoC series: %d pt(s) (primary=%d, alt=%d)",
             label, len(batt_soc_s), len(_batt_soc_pct_s), len(_batt_soc_alt_s))

    if not ac_power_s:
        errors.append(f"{label}: AC power series empty — skipped")
        return

    # ── 3. Detect intervals ───────────────────────────────────────────────────
    ac_interval   = _detect_interval(ac_power_s)   or DEFAULT_INTERVAL_MS
    batt_interval = _detect_interval(batt_power_s) or DEFAULT_INTERVAL_MS
    gap_tolerance = ac_interval * 1.5

    log.info("%s: AC interval=%dmin (%d slots), Batt interval=%dmin (%d slots)",
             label, ac_interval // 60_000, len(ac_power_s),
             batt_interval // 60_000, len(batt_power_s))

    # ── 4. Build AC timeline ──────────────────────────────────────────────────
    ac_freq_by_t = dict(ac_freq_s)
    ac_l1_by_t   = dict(ac_volt_l1_s)
    ac_l2_by_t   = dict(ac_volt_l2_s)
    ac_l3_by_t   = dict(ac_volt_l3_s)

    timeline: list[tuple[int, str]] = []
    prev_t = ac_power_s[0][0]
    prev_state = _classify_ac_slot(
        prev_t,
        ac_freq_by_t.get(prev_t),
        ac_l1_by_t.get(prev_t), ac_l2_by_t.get(prev_t), ac_l3_by_t.get(prev_t),
        genset_running_sorted, ac_interval,
    )
    timeline.append((prev_t, prev_state))

    for t, v in ac_power_s[1:]:
        gap = t - prev_t
        if gap > gap_tolerance:
            fill_t = prev_t + ac_interval
            while fill_t < t - ac_interval // 2:
                timeline.append((fill_t, "OUTAGE"))
                fill_t += ac_interval
        state = _classify_ac_slot(
            t,
            ac_freq_by_t.get(t),
            ac_l1_by_t.get(t), ac_l2_by_t.get(t), ac_l3_by_t.get(t),
            genset_running_sorted, ac_interval,
        )
        timeline.append((t, state))
        prev_t = t

    timeline.sort(key=lambda x: x[0])

    # ── 5. Group into outage events ──────────────────────────────────────────
    events = _build_events(timeline, ac_interval)
    log.info("%s: %d outage event(s)", label, len(events))
    if not events:
        return

    # ── 6. Per-event analysis ─────────────────────────────────────────────────
    MIN_OUTAGE_MS = 5 * 60 * 1000   # 5 min — filter AC data-gap false outages

    for ev in events:
        ev_start_ms = ev["start_ms"]
        ev_end_ms   = ev["end_ms"]
        gen_slots   = ev["gen_slots"]

        # Skip suspiciously short events (AC data gaps, not real outages)
        if ev_end_ms - ev_start_ms < MIN_OUTAGE_MS:
            log.debug("%s: outage at %s too short (%.1f min) — skipped",
                      label, _fmt_bdt(ev_start_ms),
                      (ev_end_ms - ev_start_ms) / 60_000)
            continue

        # Battery data window: extend slightly beyond event boundaries
        buf    = 2 * batt_interval
        b_pow  = [(t, v) for t, v in batt_power_s if ev_start_ms - buf <= t <= ev_end_ms + buf]
        b_curr = [(t, v) for t, v in batt_curr_s  if ev_start_ms - buf <= t <= ev_end_ms + buf]
        b_volt = [(t, v) for t, v in batt_volt_s  if ev_start_ms - buf <= t <= ev_end_ms + buf]

        # Confirm battery was discharging to validate this is a real outage
        if not _battery_discharging(b_pow, b_curr):
            log.debug("%s: outage at %s not confirmed by battery data — skipped",
                      label, _fmt_bdt(ev_start_ms))
            continue

        # Discharge statistics for the outage window (battery group = total)
        discharged_kwh, avg_discharge_kw = _discharge_stats(
            batt_power_s, ev_start_ms, ev_end_ms, batt_interval
        )

        # Per-tenant DC load breakdown.
        #
        # Intelligent Rect system (branch_power_by_tenant is non-empty):
        #   Each tenant column is populated from the corresponding Branch Group NE
        #   which records that operator's actual DC load on the shared bus.
        #
        # Single-tenant / dedicated rectifier (branch_power_by_tenant is empty):
        #   There are no Branch Group NEs — the entire battery output belongs to
        #   one operator.  Fill that operator's column with the total battery
        #   discharge power; leave all other tenant columns empty.
        tenant_load_kw: dict[str, Optional[float]] = {k: None for k in TENANT_COLS}

        if branch_power_by_tenant:
            # Intelligent Rect — average Branch Group power per tenant during outage
            for tkey in TENANT_COLS:
                series = branch_power_by_tenant.get(tkey, [])
                vals = [v for t, v in series
                        if ev_start_ms <= t <= ev_end_ms and v is not None and v >= 0]
                tenant_load_kw[tkey] = round(sum(vals) / len(vals), 2) if vals else None
        else:
            # Single-tenant — map total discharge to the correct operator column.
            # Use tenant_code → canonical key map first (e.g. "GP01" → "GP"),
            # fall back to tenant_name if it directly matches a TENANT_COLS key,
            # and finally try extracting from the code itself.
            canonical_key = (
                _TENANT_KEY_MAP.get(tenant_code)
                or _TENANT_KEY_MAP.get(tenant_code.upper())
                or (tenant_name if tenant_name in TENANT_COLS else None)
            )
            if canonical_key:
                tenant_load_kw[canonical_key] = avg_discharge_kw

        # Minimum voltage during outage window
        min_volt: Optional[float] = None
        volt_in_window = [v for t, v in b_volt
                          if ev_start_ms <= t <= ev_end_ms and v is not None]
        if volt_in_window:
            min_volt = min(volt_in_window)

        # Detect LLVD
        llvd_time_ms, llvd_method = _detect_llvd(b_pow, b_curr, b_volt, ev_start_ms)

        # Backup end time and result classification
        backup_end_ms: Optional[int] = None
        genset_detected = len(gen_slots) > 0
        gen_before_llvd = False

        if llvd_time_ms:
            backup_end_ms = llvd_time_ms
            gen_before_llvd = any(
                llvd_time_ms - 2 * ac_interval <= t <= llvd_time_ms
                for t, _ in gen_slots
            )
            result_label = "✅ LLVD (Gen Before LLVD)" if gen_before_llvd else "✅ LLVD Recorded"
        elif genset_detected:
            first_gen_t   = min(t for t, _ in gen_slots)
            backup_end_ms = first_gen_t
            llvd_method   = "Gen arrival (prevented LLVD)"
            gen_before_llvd = True
            result_label  = "✅ Gen Before LLVD"
        else:
            backup_end_ms = ev_end_ms
            result_label  = "⚪ AC Restored (No LLVD)"

        # Actual backup time
        actual_bt_min: Optional[float] = None
        if backup_end_ms and backup_end_ms > ev_start_ms:
            actual_bt_min = (backup_end_ms - ev_start_ms) / 60_000

        # Battery voltage at backup end
        batt_v_at_end: Optional[float] = None
        if backup_end_ms and b_volt:
            closest = min(b_volt, key=lambda x: abs(x[0] - backup_end_ms))
            batt_v_at_end = closest[1]

        # Initial SoC at outage start.
        # Use the last BMS SoC reading at or before ev_start_ms.
        # If none exists (outage started before first SoC reading), fall back
        # to the first reading within one interval after outage start.
        # Either way this reflects the battery's charge state when the outage began.
        initial_soc_pct: Optional[float] = None
        if batt_soc_s:
            before = [(t, v) for t, v in batt_soc_s if t <= ev_start_ms]
            after  = [(t, v) for t, v in batt_soc_s
                      if ev_start_ms < t <= ev_start_ms + batt_interval]
            if before:
                initial_soc_pct = before[-1][1]   # most recent reading before outage
            elif after:
                initial_soc_pct = after[0][1]     # first reading just after start (fallback)

        ev_duration_min = (ev_end_ms - ev_start_ms) / 60_000

        results.append({
            "Site":                              site_name,
            "Tenant Code":                       tenant_code,
            "Tenant Name":                       tenant_name,
            "Outage Start (BDT)":                _fmt_bdt(ev_start_ms),
            "Outage End (BDT)":                  _fmt_bdt(ev_end_ms),
            "Outage Duration (min)":             _r2(ev_duration_min),
            "Generator Detected":                "Yes" if genset_detected else "No",
            "Gen Before LLVD":                   "Yes" if gen_before_llvd else "No",
            "LLVD Triggered":                    "Yes" if llvd_time_ms else "No",
            "LLVD / Backup End (BDT)":           _fmt_bdt(backup_end_ms) if backup_end_ms else "",
            "Actual Backup Time (min)":          _r2(actual_bt_min),
            "Discharged Energy (kWh)":           _r2(discharged_kwh),
            "Avg Discharge Power - Total (kW)":  _r2(avg_discharge_kw),
            "Avg Discharge Power - Robi (kW)":   _r2(tenant_load_kw.get("Robi")),
            "Avg Discharge Power - GP (kW)":     _r2(tenant_load_kw.get("GP")),
            "Avg Discharge Power - BL (kW)":     _r2(tenant_load_kw.get("BL")),
            "Avg Discharge Power - TBL (kW)":    _r2(tenant_load_kw.get("TBL")),
            "Avg Discharge Power - Non-MNO (kW)":_r2(tenant_load_kw.get("Non-MNO")),
            "Avg Discharge Power - Others (kW)": _r2(tenant_load_kw.get("Others")),
            "Min Batt Voltage (V)":              _r2(min_volt),
            "Batt Voltage at LLVD (V)":          _r2(batt_v_at_end),
            "Initial SoC (%)":                   _r2(initial_soc_pct),
            "LLVD Detection Method":             llvd_method,
            "Result":                            result_label,
        })


# ══════════════════════════════════════════════════════════════════════════════
# Chunked API fetch
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_chunked(client, dns: list, signal_ids: list,
                   start_ms: int, end_ms: int) -> list:
    """
    Fetch statistic data in ≤23 h chunks (API max is 24 h).
    Handles multiple DN batches if len(dns) > MAX_DNS_PER_CALL.
    Returns a flat list of raw API records.
    """
    all_raw: list = []
    dn_batches = [dns[i:i + MAX_DNS_PER_CALL] for i in range(0, len(dns), MAX_DNS_PER_CALL)]

    for batch in dn_batches:
        t = start_ms
        while t < end_ms:
            chunk_end = min(t + API_WINDOW_MS, end_ms)
            try:
                chunk = client.get_statistic(batch, signal_ids, t, chunk_end)
                all_raw.extend(chunk)
                log.debug("_fetch_chunked: %d records [%s … %s]",
                          len(chunk), _fmt_bdt(t), _fmt_bdt(chunk_end))
            except Exception as exc:
                log.warning("_fetch_chunked: error fetching %s..%s — %s",
                             _fmt_bdt(t), _fmt_bdt(chunk_end), exc)
            t = chunk_end

    return all_raw


# ══════════════════════════════════════════════════════════════════════════════
# Series extraction and interval detection
# ══════════════════════════════════════════════════════════════════════════════

def _extract_series(raw: list, signal_id: int) -> list[tuple[int, float]]:
    """
    Extract sorted (time_ms, value) pairs for a given signal ID.
    When multiple DNs report the same signal at the same time (multi-battery),
    the values are averaged.
    """
    by_time: dict[int, list[float]] = {}
    for item in raw:
        try:
            sid = int(item.get("signalId", 0))
        except (TypeError, ValueError):
            continue
        if sid != signal_id:
            continue

        t = (item.get("statisticTime") or item.get("collectTime") or
             item.get("signalResultTime") or item.get("endTime"))
        v = (item.get("statisticValue") or item.get("signalValue") or
             item.get("value"))
        try:
            t = int(t)
            v = float(v)
        except (TypeError, ValueError):
            continue

        by_time.setdefault(t, []).append(v)

    return sorted(
        ((t, sum(vs) / len(vs)) for t, vs in by_time.items()),
        key=lambda x: x[0],
    )


def _detect_interval(series: list) -> Optional[int]:
    """
    Return the mode gap (ms) between consecutive data points.
    Ignores gaps larger than 2 h (outage gaps) to get the normal collection interval.
    """
    if len(series) < 3:
        return None
    gaps = [
        series[i + 1][0] - series[i][0]
        for i in range(len(series) - 1)
    ]
    normal = [g for g in gaps if 0 < g < 2 * 3600 * 1000]
    if not normal:
        return None
    try:
        return _stat_mode(normal)
    except Exception:
        return sorted(normal)[len(normal) // 2]


# ══════════════════════════════════════════════════════════════════════════════
# Timeline building and event grouping
# ══════════════════════════════════════════════════════════════════════════════

def _classify_ac_slot(t: int,
                       freq: Optional[float],
                       volt_l1: Optional[float],
                       volt_l2: Optional[float],
                       volt_l3: Optional[float],
                       genset_running_sorted: list,
                       interval_ms: int) -> str:
    """
    Classify an AC slot as GRID or GENERATOR using three evidence sources
    in priority order:

      1. Genset running-state statistic (#30012 = 2 → Running) — most authoritative
         when the site has a monitored Genset NE.  A binary search on the sorted
         running-timestamp list checks for a match within ±interval_ms.

      2. AC frequency — generators in Bangladesh typically run above 51 Hz.
         Grid frequency is tightly regulated at 50 Hz (±1 Hz → 49–51 Hz).
         Any reading outside [49, 51] Hz → GENERATOR.

      3. Phase voltage asymmetry — grid supplies all three phases (L1/L2/L3).
         A single-phase genset will have only one phase ≥ PHASE_VOLTAGE_MIN (100 V).
         If exactly 1 of 3 known phase voltages is live → GENERATOR.
         If 2 or 3 phases are live → GRID.

    Defaults to GRID when no source can make a determination, to avoid
    false-positive generator classification.
    """
    # Priority 1: genset running-state statistic (historical, per-interval)
    if genset_running_sorted:
        idx = bisect.bisect_left(genset_running_sorted, t)
        for i in (idx - 1, idx):
            if 0 <= i < len(genset_running_sorted):
                if abs(genset_running_sorted[i] - t) <= interval_ms:
                    return "GENERATOR"

    # Priority 2: AC frequency
    if freq is not None:
        if GRID_FREQ_LOW <= freq <= GRID_FREQ_HIGH:
            return "GRID"
        return "GENERATOR"

    # Priority 3: phase voltage asymmetry
    phases = [volt_l1, volt_l2, volt_l3]
    known  = [v for v in phases if v is not None]
    if len(known) == 3:
        live = sum(1 for v in known if v >= PHASE_VOLTAGE_MIN)
        if live == 1:
            return "GENERATOR"   # single-phase supply — generator
        if live >= 2:
            return "GRID"        # multi-phase supply — grid

    # Cannot determine
    return "GRID"


def _build_events(timeline: list, interval_ms: int) -> list[dict]:
    """
    Scan timeline and group outage slots into discrete events.

    Rules:
      • An event STARTS at the first OUTAGE slot after a GRID slot.
      • GENERATOR slots within an event extend it (generator is on, battery still off-grid).
      • An event ENDS at the first GRID slot after one or more OUTAGE slots.
      • The event's end_ms is the last non-GRID slot's time + interval_ms.

    Returns a list of dicts:
        {start_ms, end_ms, gen_slots: [(t, state), …]}
    """
    events: list[dict] = []
    in_event = False
    ev_start_ms: int = 0
    last_non_grid_ms: int = 0
    gen_slots: list[tuple[int, str]] = []

    for t, state in timeline:
        if not in_event:
            if state == "OUTAGE":
                in_event       = True
                ev_start_ms    = t
                last_non_grid_ms = t
                gen_slots      = []
        else:
            if state == "GRID":
                # End of event — grid has returned
                events.append({
                    "start_ms":  ev_start_ms,
                    "end_ms":    last_non_grid_ms + interval_ms,
                    "gen_slots": list(gen_slots),
                })
                in_event = False
            elif state == "GENERATOR":
                gen_slots.append((t, state))
                last_non_grid_ms = t
            elif state == "OUTAGE":
                last_non_grid_ms = t

    # Handle event that extends to the end of the analysis window
    if in_event:
        events.append({
            "start_ms":  ev_start_ms,
            "end_ms":    last_non_grid_ms + interval_ms,
            "gen_slots": list(gen_slots),
        })

    return events


# ══════════════════════════════════════════════════════════════════════════════
# LLVD detection
# ══════════════════════════════════════════════════════════════════════════════

def _battery_discharging(batt_power: list, batt_curr: list) -> bool:
    """Return True if battery was clearly discharging during the window."""
    for _, v in batt_power:
        if v is not None and abs(v) >= BATT_DISCHARGING_POWER:
            return True
    for _, v in batt_curr:
        if v is not None and abs(v) >= BATT_DISCHARGING_CURR:
            return True
    return False


def _detect_llvd(batt_power: list, batt_curr: list, batt_volt: list,
                 outage_start_ms: int) -> tuple[Optional[int], str]:
    """
    Detect LLVD trigger from battery data.

    Primary  : battery charge/discharge power (abs) drops from >1 kW to ≤1 kW.
    Fallback : battery current drops >70 % from preceding slot, AND the
               resulting discharge power is ≤1 kW, AND voltage ≤ LLVD_VOLT_MAX
               (48.5 V — approaching the 48–47.5 V LLVD threshold).
               All three conditions must hold to avoid false positives from
               normal load changes (e.g. 49.6 V × 27 A = 1.34 kW is still
               above threshold and must NOT be tagged as LLVD).

    Returns (llvd_time_ms, method_label).  (None, "N/A") if not detected.
    """
    # Pre-index voltage by timestamp for cross-lookup in both detection paths
    volt_by_t = {t: v for t, v in batt_volt if v is not None}

    # ── Primary: power drop + voltage confirmation ───────────────────────────
    # LLVD is voltage-driven: discharge power falling to ≤1 kW is a consequence
    # of LLVD disconnecting loads, not the cause.  Voltage MUST also be approaching
    # the 47.5–48 V threshold (≤ LLVD_VOLT_MAX = 48.5 V).
    # If voltage data is absent for that slot we cannot confirm → skip.
    prev_p: Optional[float] = None
    for t, v in batt_power:
        if v is None:
            continue
        curr_p = abs(v)
        if t < outage_start_ms:
            prev_p = curr_p
            continue
        if prev_p is not None and prev_p > LLVD_POWER_KW and curr_p <= LLVD_POWER_KW:
            volt_now = volt_by_t.get(t)
            if volt_now is None or volt_now > LLVD_VOLT_MAX:
                # Voltage too high or unavailable — not LLVD, keep scanning
                prev_p = curr_p
                continue
            return (t, f"Batt power {prev_p:.1f}→{curr_p:.1f} kW, {volt_now:.1f} V")
        prev_p = curr_p

    # ── Fallback: current drop with power + voltage confirmation ─────────────
    # Pre-index power by timestamp (volt_by_t already built above)
    power_by_t = {t: abs(v) for t, v in batt_power if v is not None}

    prev_c: Optional[float] = None
    for t, v in batt_curr:
        if v is None:
            continue
        curr_c = abs(v)
        if t < outage_start_ms:
            prev_c = curr_c
            continue
        if prev_c is not None and prev_c > 0:
            drop = (prev_c - curr_c) / prev_c
            if drop >= LLVD_CURRENT_DROP_PCT:
                # Condition 1: discharge power at this slot must be ≤ 1 kW
                power_now = power_by_t.get(t)
                if power_now is None:
                    # Estimate from current × voltage (V·A → kW)
                    volt_now = volt_by_t.get(t)
                    if volt_now and curr_c:
                        power_now = (volt_now * curr_c) / 1000.0

                if power_now is None or power_now > LLVD_POWER_KW:
                    prev_c = curr_c
                    continue   # power still above 1 kW — not LLVD

                # Condition 2: voltage must be approaching LLVD threshold
                volt_now = volt_by_t.get(t)
                if volt_now is not None and volt_now > LLVD_VOLT_MAX:
                    prev_c = curr_c
                    continue   # voltage still too high — not LLVD

                return (t,
                        f"Batt current {prev_c:.0f}→{curr_c:.0f} A "
                        f"({drop*100:.0f}% drop, "
                        f"{power_now:.2f} kW"
                        + (f", {volt_now:.1f} V" if volt_now else "")
                        + ")")
        prev_c = curr_c

    return None, "N/A"


# ══════════════════════════════════════════════════════════════════════════════
# Output helpers
# ══════════════════════════════════════════════════════════════════════════════

def _tenant_key_from_ne_name(ne_name: str) -> Optional[str]:
    """
    Try to extract the canonical TENANT_COLS key from a Battery Group NE name.

    Pattern seen in the field:
        CMBRM1_Huawei01_Int_GP01_Battery Group   → "GP"
        SITE_Vdr_Int_ROBI01_Battery Group         → "Robi"

    Returns None if the name doesn't match or the code isn't in the map.
    """
    m = re.search(r'_Int_([^_]+)_Battery', ne_name, re.IGNORECASE)
    if not m:
        return None
    code = m.group(1).strip()
    return _TENANT_KEY_MAP.get(code) or _TENANT_KEY_MAP.get(code.upper())


def _branch_tenant_key(ne_name: str) -> str:
    """
    Derive the canonical TENANT_COLS key from a Branch Group NE name.

    Name patterns observed in the field:
      Branch Group[1]-ROBI01          → "Robi"
      Branch Group[2]-GP01            → "GP"
      SITE_..._Branch Group[3]-BL01   → "BL"
      Branch Group[3]-Cooling_Others  → "Others"
      Branch Group[4]-RMS             → "Others"
      Branch Group[3]                 → "Others"  (no tenant suffix)
    """
    m = re.search(r'\]\-(.+)$', ne_name)
    if not m:
        return "Others"
    label = m.group(1).strip()
    # Auxiliary / non-MNO infrastructure → Others
    if label in _BRANCH_AUX or "Cooling" in label:
        return "Others"
    # Canonical lookup (try exact, then upper-case)
    key = _TENANT_KEY_MAP.get(label) or _TENANT_KEY_MAP.get(label.upper())
    return key if key else "Others"


def _extract_branch_by_tenant(
    raw: list, branch_by_tenant: dict[str, list[str]]
) -> dict[str, list[tuple[int, float]]]:
    """
    Extract per-tenant Branch Group power time-series from raw statistic records.

    Unlike _extract_series (which averages across DNs), here we SUM the Branch Group
    power readings within the same tenant group — each Branch Group NE is a separate
    physical branch and their loads add up.

    Returns {tenant_key: sorted [(time_ms, total_power_kW), ...]}
    """
    # Build DN → tenant reverse map
    dn_to_tenant: dict[str, str] = {
        dn: tenant
        for tenant, dns in branch_by_tenant.items()
        for dn in dns
    }

    # Accumulate: tenant → time_ms → [values from different DNs]
    by_tenant_time: dict[str, dict[int, list[float]]] = {
        k: {} for k in branch_by_tenant
    }

    for item in raw:
        try:
            sid = int(item.get("signalId", 0))
        except (TypeError, ValueError):
            continue
        if sid != SIG_BRANCH_POWER:
            continue

        dn = item.get("dn") or item.get("neDn") or item.get("DN", "")
        tenant = dn_to_tenant.get(dn)
        if not tenant:
            continue

        t = (item.get("statisticTime") or item.get("collectTime") or
             item.get("signalResultTime") or item.get("endTime"))
        v = (item.get("statisticValue") or item.get("signalValue") or item.get("value"))
        try:
            t = int(t)
            v = float(v)
        except (TypeError, ValueError):
            continue

        by_tenant_time[tenant].setdefault(t, []).append(v)

    return {
        tenant: sorted(
            ((t, sum(vs)) for t, vs in time_map.items()),
            key=lambda x: x[0],
        )
        for tenant, time_map in by_tenant_time.items()
    }


def _result_columns() -> list[str]:
    return [
        "Site", "Tenant Code", "Tenant Name",
        "Outage Start (BDT)", "Outage End (BDT)", "Outage Duration (min)",
        "Generator Detected", "Gen Before LLVD",
        "LLVD Triggered", "LLVD / Backup End (BDT)", "Actual Backup Time (min)",
        "Discharged Energy (kWh)",
        "Avg Discharge Power - Total (kW)",
        "Avg Discharge Power - Robi (kW)",
        "Avg Discharge Power - GP (kW)",
        "Avg Discharge Power - BL (kW)",
        "Avg Discharge Power - TBL (kW)",
        "Avg Discharge Power - Non-MNO (kW)",
        "Avg Discharge Power - Others (kW)",
        "Min Batt Voltage (V)", "Batt Voltage at LLVD (V)", "Initial SoC (%)",
        "LLVD Detection Method", "Result",
    ]


def _discharge_stats(batt_power: list, start_ms: int, end_ms: int,
                     interval_ms: int) -> tuple[Optional[float], Optional[float]]:
    """
    Calculate total discharged energy (kWh) and average discharge power (kW)
    for battery readings within [start_ms, end_ms].

    Battery discharge power is negative in NetEco — abs() is applied.
    Only slots at or above BATT_DISCHARGING_POWER are counted.
    Energy = Σ(power_kW × interval_hours) per slot.
    """
    interval_h = interval_ms / 3_600_000
    vals = [
        abs(v) for t, v in batt_power
        if start_ms <= t <= end_ms
        and v is not None
        and abs(v) >= BATT_DISCHARGING_POWER
    ]
    if not vals:
        return None, None
    total_kwh = sum(vals) * interval_h
    avg_kw    = sum(vals) / len(vals)
    return round(total_kwh, 3), round(avg_kw, 2)


def _fmt_bdt(ms) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=_BDT).strftime("%Y-%m-%d %H:%M BDT")
    except Exception:
        return str(ms)


def _r2(val) -> str | float:
    """Round to 2 d.p. or return empty string for None."""
    if val is None:
        return ""
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return ""
