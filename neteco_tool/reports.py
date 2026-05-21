"""
Predefined report definitions and execution logic.
Each report bundles a set of MO types + signal IDs + signal type into a named query.
"""
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# Predefined report catalogue
# ─────────────────────────────────────────────

REPORTS = {
    "battery_live": {
        "id": "battery_live",
        "name": "Battery Status (Live)",
        "icon": "battery-charging",
        "description": "Real-time battery state, voltage, current, SOC, temperature and SOH for all battery groups and strings.",
        "signal_type": "sampling",
        "mo_signals": {
            "Battery Group":  {"type_id": 32772, "signal_ids": [10001, 10002, 10003, 10004, 10005, 2101493]},
            "Battery String": {"type_id": 32773, "signal_ids": [10002, 10003, 10004, 10005, 10006, 10007]},
        },
    },
    "rectifier_live": {
        "id": "rectifier_live",
        "name": "Rectifier Status (Live)",
        "icon": "zap",
        "description": "Real-time rectifier running state, DC output voltage/current, AC input voltage, and load usage.",
        "signal_type": "sampling",
        "mo_signals": {
            "Rectifier Group": {"type_id": 32771, "signal_ids": [10001, 10002, 10010]},
            "Rectifier":       {"type_id": 32770, "signal_ids": [10002, 10003, 10004, 10005]},
        },
    },
    "dc_power_live": {
        "id": "dc_power_live",
        "name": "DC Power Bus (Live)",
        "icon": "activity",
        "description": "DC bus voltage, total load current, power supply type and load power.",
        "signal_type": "sampling",
        "mo_signals": {
            "Digital Power":        {"type_id": 33330, "signal_ids": [2101505, 10017, 10013]},
            "DC Output Distribution": {"type_id": 33318, "signal_ids": [10001, 10002, 10003]},
        },
    },
    "mains_live": {
        "id": "mains_live",
        "name": "AC / Mains Status (Live)",
        "icon": "power",
        "description": "Mains state, AC voltage, current, active power, frequency across all phases.",
        "signal_type": "sampling",
        "mo_signals": {
            "Mains":                {"type_id": 36892, "signal_ids": [10001, 10002, 10003, 10004, 10005, 10006, 10010, 10011]},
            "AC Input Distribution": {"type_id": 33329, "signal_ids": [10001, 10002, 10003, 10004, 10005, 10006, 10007, 10008, 10014, 10015]},
        },
    },
    "site_config": {
        "id": "site_config",
        "name": "Site Configuration",
        "icon": "settings",
        "description": "Site settings: designed backup time, LLVD threshold, site type, maintenance mode.",
        "signal_type": "config",
        "mo_signals": {
            "Site":                  {"type_id": 30001, "signal_ids": [20001, 20003, 20008, 20009, 20010, 20011, 20012, 20013, 20015, 20016]},
            "DC Output Distribution": {"type_id": 33318, "signal_ids": [20013, 20014, 20015]},
        },
    },
    "active_alarms": {
        "id": "active_alarms",
        "name": "Active Alarms",
        "icon": "alert-triangle",
        "description": "All currently active (uncleared) alarms for selected sites.",
        "signal_type": "alarm_current",
        "mo_signals": {},
    },
    "historical_alarms": {
        "id": "historical_alarms",
        "name": "Historical Alarms",
        "icon": "clock",
        "description": "Cleared/historical alarms within a specified date range.",
        "signal_type": "alarm_history",
        "mo_signals": {},
        "needs_date_range": True,
    },
    "inventory": {
        "id": "inventory",
        "name": "Hardware Inventory",
        "icon": "package",
        "description": "Device inventory data: equipment model, serial number, manufacture date.",
        "signal_type": "inventory",
        "mo_signals": {},
    },
}


# ─────────────────────────────────────────────
# Signal name lookup (common signal IDs)
# ─────────────────────────────────────────────
SIGNAL_NAMES = {
    # Battery Group
    10001: "Battery State",
    10002: "Voltage (V) / Current (A)",  # differs by MO; we'll use per-MO name
    10003: "Current (A) / DC Output Voltage (V)",
    10004: "Remaining Capacity (%)",
    10005: "Temperature (°C)",
    2101493: "Battery SOH",
    # Battery String
    10006: "Rated Capacity (Ah)",
    10007: "Remaining Capacity (Ah)",
    # Rectifier Group
    10001: "Running State / Battery State",
    10010: "Load Usage Rate (%)",
    # Rectifier
    # 10002 = Running State, 10003 = DC Output Voltage, 10004 = DC Output Current, 10005 = AC Input Voltage
    # Digital Power / DC Output Distribution
    2101505: "DC Output Voltage (V)",
    10017: "Total DC Load Current (A)",
    10013: "Current Power Supply Type",
    # Mains
    10006: "AC Current (A)",
    10010: "Active Power (kW)",
    10011: "AC Frequency (Hz)",
    # Site config
    20001: "Site Type",
    20003: "Site ID",
    20008: "Site Level",
    20009: "Battery Designed Backup Time (h)",
    20010: "Load Work Mode",
    20011: "Description",
    20012: "Backup Time Warning Threshold (h)",
    20013: "LLVD Voltage (V) / Site Category",
    20014: "LLVD2 Voltage (V)",
    20015: "LLVD3 Voltage (V) / Acceptance State",
    20016: "Maintenance Mode",
}


# ─────────────────────────────────────────────
# Report execution
# ─────────────────────────────────────────────
def run_report(report_id: str, site_dns: list, client,
               ne_tree_mod, start_ms: int = None, end_ms: int = None) -> dict:
    """
    Execute a predefined report for the given site DNs.
    Returns {"columns": [...], "rows": [...], "site_names": {...}}
    """
    report = REPORTS.get(report_id)
    if not report:
        return {"error": f"Unknown report: {report_id}"}

    sig_type = report["signal_type"]

    # ── Alarm reports ──────────────────────────────
    if sig_type == "alarm_current":
        raw = client.get_active_alarms(dns=site_dns)
        return _format_alarm_results(raw, "active")

    if sig_type == "alarm_history":
        if not start_ms or not end_ms:
            return {"error": "Date range required for historical alarms"}
        raw = client.get_historical_alarms(start_ms, end_ms, dns=site_dns)
        return _format_alarm_results(raw, "history")

    if sig_type == "inventory":
        raw = client.get_inventory()
        return _format_inventory_results(raw)

    # ── Signal reports (sampling / config / statistic) ─
    mo_signals = report["mo_signals"]
    mo_type_names = list(mo_signals.keys())

    # Find all relevant NE DNs under the selected sites
    ne_by_type = ne_tree_mod.get_dns_by_type_under_sites(site_dns, mo_type_names)

    all_rows = []
    for mo_type_name, spec in mo_signals.items():
        nes = ne_by_type.get(mo_type_name, [])
        if not nes:
            continue
        dns = [ne["dn"] for ne in nes]
        dn_to_name = {ne["dn"]: ne["name"] for ne in nes}
        dn_to_status = {ne["dn"]: ne["status"] for ne in nes}

        if sig_type == "sampling":
            raw = client.get_sampling(dns, spec["signal_ids"])
        elif sig_type == "config":
            raw = client.get_config_signals(dns, spec["signal_ids"])
        elif sig_type == "statistic":
            if not start_ms or not end_ms:
                continue
            raw = client.get_statistic(dns, spec["signal_ids"], start_ms, end_ms)
        else:
            continue

        for item in raw:
            dn = item.get("dn", "")
            all_rows.append({
                "MO Type": mo_type_name,
                "NE Name": dn_to_name.get(dn, dn),
                "DN": dn,
                "Status": dn_to_status.get(dn, ""),
                "Signal ID": item.get("signalId", ""),
                "Signal Name": item.get("signalName", ""),
                "Value": item.get("signalValue", ""),
                "Unit": item.get("unit", ""),
                "Timestamp": _fmt_ts(item.get("signalResultTime") or item.get("collectTime")),
            })

    columns = ["MO Type", "NE Name", "DN", "Status", "Signal ID", "Signal Name", "Value", "Unit", "Timestamp"]
    return {"columns": columns, "rows": all_rows, "count": len(all_rows)}


def _format_alarm_results(raw: list, mode: str) -> dict:
    rows = []
    for a in raw:
        rows.append({
            "Alarm Name": a.get("alarmName", ""),
            "Alarm ID": a.get("alarmId", ""),
            "Severity": _severity_label(a.get("severity")),
            "DN": a.get("dn", ""),
            "Location": a.get("locationInfo", ""),
            "Occur Time": _fmt_ts(a.get("occurTime")),
            "Clear Time": _fmt_ts(a.get("clearTime")) if mode == "history" else "Active",
            "Ack Time": _fmt_ts(a.get("ackTime")),
            "Additional Text": a.get("additionalText", ""),
        })
    cols = ["Alarm Name", "Alarm ID", "Severity", "DN", "Location",
            "Occur Time", "Clear Time", "Ack Time", "Additional Text"]
    return {"columns": cols, "rows": rows, "count": len(rows)}


def _format_inventory_results(raw: list) -> dict:
    rows = []
    for item in raw:
        rows.append({
            "NE Name": item.get("neName", ""),
            "DN": item.get("neDn", ""),
            "Equipment Name": item.get("equipmentName", ""),
            "Model": item.get("model", ""),
            "Serial Number": item.get("sn", ""),
            "Manufacturer": item.get("manufacturer", ""),
            "Manufacture Date": item.get("manufactureDate", ""),
            "Software Version": item.get("softwareVersion", ""),
        })
    cols = ["NE Name", "DN", "Equipment Name", "Model", "Serial Number",
            "Manufacturer", "Manufacture Date", "Software Version"]
    return {"columns": cols, "rows": rows, "count": len(rows)}


def _fmt_ts(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def _severity_label(code):
    mapping = {1: "Critical", 2: "Major", 3: "Minor", 4: "Warning", 5: "Indeterminate"}
    return mapping.get(code, str(code) if code else "")
