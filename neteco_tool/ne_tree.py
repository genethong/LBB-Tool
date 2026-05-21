"""
NE Tree management — builds and caches the NE hierarchy from CM-MO CSV or API.
Stored in SQLite for fast, low-memory access.
"""
import csv
import json
import os
import sqlite3
import re
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "ne_cache.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # allow concurrent reads during NE-tree writes
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ne_nodes (
                dn          TEXT PRIMARY KEY,
                name        TEXT,
                parent_dn   TEXT,
                type_name   TEXT,
                type_id     INTEGER,
                type_ver_id INTEGER,
                status      TEXT,
                create_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_parent ON ne_nodes(parent_dn);
            CREATE INDEX IF NOT EXISTS idx_type   ON ne_nodes(type_name);

            CREATE TABLE IF NOT EXISTS signal_dict (
                mo_type_id  INTEGER,
                ver_id      INTEGER,
                signal_attr TEXT,
                signal_id   INTEGER,
                signal_name TEXT,
                unit        TEXT,
                PRIMARY KEY (mo_type_id, ver_id, signal_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sig_lookup
                ON signal_dict(mo_type_id, ver_id, signal_attr);
        """)


def load_from_csv(csv_path: str) -> int:
    """Import CM-MO CSV into SQLite. Returns row count."""
    init_db()
    count = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM ne_nodes")
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            for row in reader:
                batch.append((
                    row.get("dn", "").strip(),
                    row.get("name", "").strip(),
                    row.get("parentDn", "").strip(),
                    row.get("typeName", "").strip(),
                    _safe_int(row.get("typeId")),
                    _safe_int(row.get("typeVerId")),
                    row.get("status", "").strip(),
                    row.get("creatTime", "").strip(),
                ))
                count += 1
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO ne_nodes VALUES (?,?,?,?,?,?,?,?)", batch
                    )
                    batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO ne_nodes VALUES (?,?,?,?,?,?,?,?)", batch
                )
    return count


def load_from_api(client) -> int:
    """Pull MO tree from NetEco API and store in SQLite."""
    init_db()
    mos = client.get_mos()
    count = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM ne_nodes")
        batch = []
        for mo in mos:
            batch.append((
                mo.get("dn", ""),
                mo.get("name", ""),
                mo.get("parentDn", ""),
                mo.get("typeName", ""),
                _safe_int(mo.get("typeId")),
                _safe_int(mo.get("typeVerId")),
                mo.get("status", ""),
                mo.get("createTime", ""),
            ))
            count += 1
            if len(batch) >= 2000:
                conn.executemany(
                    "INSERT OR REPLACE INTO ne_nodes VALUES (?,?,?,?,?,?,?,?)", batch
                )
                batch.clear()
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO ne_nodes VALUES (?,?,?,?,?,?,?,?)", batch
            )
    return count


def has_data() -> bool:
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM ne_nodes").fetchone()
            return row["c"] > 0
    except Exception:
        return False


def get_regions() -> list:
    """Return all Subnet nodes sorted by name."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dn, name, parent_dn FROM ne_nodes WHERE type_name='Subnet' ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_sites_in_region(region_dn: str) -> list:
    """Return all Site nodes that are children of the given subnet (direct or nested)."""
    # First collect all subnet DNs under region
    all_subnet_dns = _get_all_subnet_dns(region_dn)
    all_subnet_dns.append(region_dn)

    placeholders = ",".join("?" * len(all_subnet_dns))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT dn, name, parent_dn, status FROM ne_nodes
                WHERE type_name='Site' AND parent_dn IN ({placeholders})
                ORDER BY name""",
            all_subnet_dns,
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_sites(status_filter: str = None) -> list:
    with get_conn() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT dn, name, parent_dn, status FROM ne_nodes WHERE type_name='Site' AND status=? ORDER BY name",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT dn, name, parent_dn, status FROM ne_nodes WHERE type_name='Site' ORDER BY name"
            ).fetchall()
    return [dict(r) for r in rows]


def search_sites(query: str, limit: int = 100) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dn, name, parent_dn, status FROM ne_nodes WHERE type_name='Site' AND name LIKE ? ORDER BY name LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_children(parent_dn: str, type_names: list = None) -> list:
    with get_conn() as conn:
        if type_names:
            ph = ",".join("?" * len(type_names))
            rows = conn.execute(
                f"SELECT dn, name, parent_dn, type_name, type_id, type_ver_id, status FROM ne_nodes "
                f"WHERE parent_dn=? AND type_name IN ({ph}) ORDER BY name",
                [parent_dn] + type_names,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT dn, name, parent_dn, type_name, type_id, type_ver_id, status FROM ne_nodes "
                "WHERE parent_dn=? ORDER BY name",
                (parent_dn,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_ne_by_dn(dn: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ne_nodes WHERE dn=?", (dn,)).fetchone()
    return dict(row) if row else None


def get_site_ne_tree(site_dn: str) -> dict:
    """
    Return the full device tree under a site as a nested dict.
    Used to find all DNs of a given type (e.g. Battery Group) under a site.
    """
    node = get_ne_by_dn(site_dn)
    if not node:
        return {}
    node["children"] = _load_children_recursive(site_dn, depth=0, max_depth=5)
    return node


def get_dns_by_type_under_sites(site_dns: list, type_names: list) -> dict:
    """
    For a list of site DNs, find all NE DNs of the requested types.
    Returns: {type_name: [dn, ...]}
    """
    # We need to search recursively. Use a CTE or iterative approach.
    result = {t: [] for t in type_names}
    if not site_dns:
        return result

    with get_conn() as conn:
        ph_types = ",".join("?" * len(type_names))
        # Get all NEs of the target types whose DN tree leads back to one of the sites.
        # Approach: get ALL nodes matching the type names, then filter by those
        # whose ancestor chain includes one of the site DNs.
        rows = conn.execute(
            f"SELECT dn, name, parent_dn, type_name, status FROM ne_nodes "
            f"WHERE type_name IN ({ph_types})",
            type_names,
        ).fetchall()

    # Build a parent lookup
    with get_conn() as conn:
        all_nodes = conn.execute(
            "SELECT dn, parent_dn FROM ne_nodes"
        ).fetchall()
    parent_map = {r["dn"]: r["parent_dn"] for r in all_nodes}

    site_set = set(site_dns)
    for row in rows:
        dn = row["dn"]
        # Walk up the tree to see if we reach a site_dn
        current = row["parent_dn"]
        found = False
        steps = 0
        while current and steps < 10:
            if current in site_set:
                found = True
                break
            current = parent_map.get(current, "")
            steps += 1
        if found:
            result[row["type_name"]].append(dict(row))

    return result


def get_tenants_in_site(site_dn: str) -> list:
    """
    Return the list of tenants (controllers) under a site.
    Tenant code is the operator suffix in controller name, e.g. ROBI01, GP01, BL01.
    """
    controllers = get_children(site_dn, ["Controller"])
    tenants = []
    for c in controllers:
        name = c["name"]
        # Pattern: SITECODE_VendorN_Int_TENANTCODE
        parts = name.split("_")
        if len(parts) >= 4:
            tenant = parts[-1]  # e.g. ROBI01, GP01
        else:
            tenant = name
        tenants.append({"dn": c["dn"], "name": c["name"], "tenant": tenant, "status": c["status"]})
    return tenants


def get_ne_context_bulk(ne_dns: list) -> dict:
    """
    For a list of NE DNs, walk the parent chain to find:
      - site_name : name of the Site ancestor
      - tenant    : operator/tenant code extracted from the intermediate node
                    (e.g. Digital Power, Controller) or from the site name itself.

    Naming convention observed in the field:
      Intermediate node: <PREFIX>_<Vendor>_Int_<TenantCode>_<NE Type>
      e.g. BSGND02_Huawei01_Int_ROBI01_Digital Power   → prefix = BSGND02
           BAR_R0370_Huawei01_Int_BL01_Digital Power    → prefix = BAR_R0370
           CGHLS07_Huawei01_Int_CGHLS07_Digital Power   → prefix = CGHLS07

    Fallback: if no _Int_ pattern found, use the last _-segment of the site name
    (e.g. e.coBD000165CG_CGHLS07 → CGHLS07).

    Returns: {ne_dn: {"site_name": str, "tenant": str}}
    """
    if not ne_dns:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dn, name, parent_dn, type_name FROM ne_nodes"
        ).fetchall()
    node_map = {r["dn"]: dict(r) for r in rows}

    # Auxiliary branch labels — not a tenant, these are infrastructure loads
    _AUXILIARY = frozenset({"RMS", "Cooling_Others", "Cooling", "Others"})

    # Operator code → human-readable name
    _OPERATOR = {
        "ROBI01": "Robi", "ROBI": "Robi",
        "GP01":   "GP",   "GP":   "GP",
        "BL01":   "BL",   "BL":   "BL",
        "TBL01":  "TBL",  "TBL":  "TBL",
        "NON-MNO": "Non-MNO", "NON_MNO": "Non-MNO",
    }

    def _operator_name_from_ne(ne_name: str) -> str:
        """Extract human-readable operator name directly from the NE node name.

        For iCBD / Branch Group NEs the operator code is the suffix after ]-:
          Load-iCBD[2]-ROBI01   → 'Robi'
          Branch Group[1]-GP01  → 'GP'
          Load-iCBD[51]-RMS     → ''  (auxiliary, not a tenant)

        For standard NEs the operator code is between _Int_ and the next _:
          BSGND02_Huawei01_Int_ROBI01_Digital Power → 'Robi'
          BAR_R0370_Huawei01_Int_BL01_Battery Group → 'BL'

        For Intelligent Rect shared NEs (no per-tenant suffix) returns ''.
        """
        def _lookup(code: str) -> str:
            """Case-insensitive lookup in _OPERATOR map."""
            return _OPERATOR.get(code) or _OPERATOR.get(code.upper()) or ""

        # iCBD / Branch Group suffix pattern: ...[N]-TENANTCODE
        sfx = re.search(r'\]\-(.+)$', ne_name)
        if sfx:
            label = sfx.group(1).strip()
            return _lookup(label)

        # Standard _Int_TENANTCODE_ pattern
        m_int = re.search(r'_Int_([^_]+)_', ne_name, re.IGNORECASE)
        if m_int:
            code = m_int.group(1)
            # Skip 'Intelligent' — that's the multi-tenant controller marker
            if code.lower() != "intelligent":
                return _lookup(code)
        return ""

    def _site_prefix(raw: str) -> str:
        """Strip trailing vendor token from a prefix string.
        'BAR_R0370_Huawei01' → 'BAR_R0370'
        Vendor tokens end with digits but are not purely numeric (e.g. Huawei01, ZTE01).
        """
        tokens = raw.split("_")
        if tokens and tokens[-1] and tokens[-1][-1].isdigit() and not tokens[-1].isdigit():
            tokens = tokens[:-1]
        return "_".join(tokens) if tokens else ""

    def _extract_tenant(name: str) -> str:
        """Derive the tenant identifier from an intermediate NE node name.

        Three distinct cases in the field:

        1. Standard single-tenant
           SITECODE_Vendor_Int_TENANTCODE_NE Type
           e.g. BSGND02_Huawei01_Int_ROBI01_Digital Power  → BSGND02
                BAR_R0370_Huawei01_Int_BL01_Digital Power   → BAR_R0370

        2. Intelligent Rect shared NE (no per-tenant branch suffix)
           SITECODE_Vendor_[Int_]Intelligent_Rect_NE Type
           e.g. BRPTG03_Huawei01_Int_Intelligent_Rect_Digital Power → BRPTG03

        3. Intelligent Rect per-tenant / auxiliary branch
           SITECODE_Vendor_[Int_]Intelligent_Rect_...[N]-LABEL
           e.g. BRPTG03_…_Branch Group[1]-ROBI01       → ROBI01
                BRPTG03_…_Load-iCBD[5]-GP01             → GP01
                BRPTG03_…_Load-iCBD[51]-RMS             → Auxiliary
                BRPTG03_…_Load-iCBD[53]-Cooling_Others  → Auxiliary
        """
        # ── Cases 2 & 3 : Intelligent Rect ─────────────────────────────
        m = re.search(r'_(?:Int_)?Intelligent_Rect_', name, re.IGNORECASE)
        if m:
            after = name[m.end():]          # everything after the controller token
            # Branch NEs carry a tenant/aux label after the port index: ]-LABEL
            sfx = re.search(r'\]\-(.+)$', after)
            if sfx:
                label = sfx.group(1).strip()
                if label in _AUXILIARY:
                    return "Auxiliary"
                if label:
                    return label            # e.g. ROBI01, GP01, BL01, Non-MNO, ROBI
            # Shared NE (Digital Power, Battery Group, etc.) → site prefix
            before = name[:m.start()]       # e.g. "BRPTG03_Huawei01"
            return _site_prefix(before)

        # ── Case 1 : Standard _Int_ single-tenant ──────────────────────
        parts_int = name.split("_Int_")
        if len(parts_int) >= 2:
            return _site_prefix(parts_int[0])

        return ""

    result = {}
    for ne_dn in ne_dns:
        site_name         = ""
        tenant            = ""
        last_intermediate = ""   # name of last node seen before Site
        dn = ne_dn
        for _ in range(10):
            node = node_map.get(dn)
            if not node:
                break
            if node["type_name"] in ("Site", "Subnet", "Root"):
                if node["type_name"] == "Site":
                    site_name = node["name"]
                break
            last_intermediate = node["name"]
            dn = node.get("parent_dn") or ""

        # Tenant code: extracted from the intermediate node (Digital Power etc.)
        if last_intermediate:
            tenant = _extract_tenant(last_intermediate)

        # Fallback: use last _-segment of the site name
        if not tenant and site_name:
            parts = site_name.split("_")
            if len(parts) >= 2:
                tenant = parts[-1]

        # Tenant name: extracted directly from the NE's own name
        ne_node = node_map.get(ne_dn)
        ne_own_name = ne_node["name"] if ne_node else ""
        tenant_name = _operator_name_from_ne(ne_own_name)

        result[ne_dn] = {
            "site_name":   site_name,
            "tenant":      tenant,
            "tenant_name": tenant_name,
        }
    return result


def get_region_tree() -> list:
    """
    Return a hierarchical list of regions with their child regions and site counts.
    For the UI tree selector.
    """
    regions = get_regions()
    region_map = {r["dn"]: r for r in regions}

    # Find top-level regions (parent is not a subnet in our map OR parent is country)
    tree = []
    for r in regions:
        parent = r["parent_dn"]
        # Top level = parent not in region_map (it's the country subnet or root)
        if parent not in region_map:
            r["children"] = _get_child_regions(r["dn"], region_map)
            r["site_count"] = _count_sites_under(r["dn"])
            tree.append(r)

    return tree


def find_site_by_code(site_code: str) -> dict:
    """
    Find a site by exact name, then by substring match.
    Returns {dn, name, status} or {} if not found.
    """
    code = site_code.strip()
    if not code:
        return {}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT dn, name, status FROM ne_nodes WHERE type_name='Site' AND name=? LIMIT 1",
            (code,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT dn, name, status FROM ne_nodes "
                "WHERE type_name='Site' AND name LIKE ? ORDER BY name LIMIT 1",
                (f"%{code}%",),
            ).fetchone()
    return dict(row) if row else {}


def get_controller_dn_for_tenant(site_dn: str, tenant_code: str) -> str:
    """
    Return the DN of the controller whose name ends with tenant_code.
    Falls back to site_dn if no match (so extraction still works at site level).
    The tenant_code is the last '_'-separated segment of the controller name,
    e.g. 'e.co12345_HUAWEI2_Int_KCSDR04' → tenant 'KCSDR04'.
    """
    if not tenant_code:
        return site_dn
    controllers = get_children(site_dn, ["Controller"])
    for c in controllers:
        parts = c["name"].split("_")
        ctn = parts[-1] if len(parts) >= 2 else c["name"]
        if tenant_code.upper() in ctn.upper():
            return c["dn"]
    return site_dn   # fallback — no matching tenant found


def get_ne_tree_for_type(site_dns: list, mo_type: str) -> list:
    """
    Return NEs of `mo_type` grouped under their owning site,
    with the full parent chain shown as a label.

    Result:
      [
        {
          "site_dn": "NE=xxx",
          "site_name": "SITE_A",
          "nes": [
            {"dn": "NE=xxx,...", "name": "BattGrp_001", "status": "CONNECTED",
             "parent_chain": "Controller_ROBI → DigitalPower_1"}
          ]
        }, ...
      ]
    """
    ne_map = get_dns_by_type_under_sites(site_dns, [mo_type])
    nes = ne_map.get(mo_type, [])
    if not nes:
        return []

    # Build full parent lookup once
    with get_conn() as conn:
        all_nodes = conn.execute(
            "SELECT dn, name, parent_dn, type_name FROM ne_nodes"
        ).fetchall()
    node_map = {r["dn"]: dict(r) for r in all_nodes}
    site_set = set(site_dns)

    site_results = {}   # site_dn -> {site_dn, site_name, nes:[]}

    for ne in nes:
        # Walk up the parent chain until we hit one of the selected site DNs
        chain_names = []
        current_dn = ne["parent_dn"]
        site_dn = None
        site_name = ""
        depth = 0
        while current_dn and depth < 12:
            node = node_map.get(current_dn)
            if not node:
                break
            if current_dn in site_set:
                site_dn = current_dn
                site_name = node["name"]
                break
            chain_names.insert(0, node["name"])
            current_dn = node.get("parent_dn", "")
            depth += 1

        if not site_dn:
            continue

        if site_dn not in site_results:
            site_results[site_dn] = {
                "site_dn": site_dn,
                "site_name": site_name,
                "nes": [],
            }

        site_results[site_dn]["nes"].append({
            "dn":           ne["dn"],
            "name":         ne["name"],
            "status":       ne.get("status", ""),
            "parent_chain": " → ".join(chain_names),
        })

    result = list(site_results.values())
    result.sort(key=lambda s: s["site_name"])
    for s in result:
        s["nes"].sort(key=lambda n: n["name"])
    return result


def get_all_type_names() -> list:
    """Return all distinct type_name values and their counts — used to verify NE names."""
    if not has_data():
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT type_name, COUNT(*) as cnt FROM ne_nodes GROUP BY type_name ORDER BY cnt DESC"
        ).fetchall()
    return [{"type_name": r["type_name"], "count": r["cnt"]} for r in rows]


def get_stats() -> dict:
    if not has_data():
        return {}
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM ne_nodes").fetchone()["c"]
        sites = conn.execute("SELECT COUNT(*) as c FROM ne_nodes WHERE type_name='Site'").fetchone()["c"]
        connected = conn.execute(
            "SELECT COUNT(*) as c FROM ne_nodes WHERE type_name='Site' AND status='CONNECTED'"
        ).fetchone()["c"]
        batt = conn.execute(
            "SELECT COUNT(*) as c FROM ne_nodes WHERE type_name='Battery Group'"
        ).fetchone()["c"]
        rect = conn.execute(
            "SELECT COUNT(*) as c FROM ne_nodes WHERE type_name='Rectifier Group'"
        ).fetchone()["c"]
    return {
        "total_nes": total,
        "total_sites": sites,
        "connected_sites": connected,
        "battery_groups": batt,
        "rectifier_groups": rect,
    }


def get_freshness() -> dict:
    """
    Return NE data freshness using the DB file mtime as a proxy.
    Returns {"loaded_at": "YYYY-MM-DD HH:MM", "count": <site_count>} or
    {"loaded_at": None, "count": 0} when no data is loaded.
    """
    if not has_data():
        return {"loaded_at": None, "count": 0}
    try:
        mtime = os.path.getmtime(DB_PATH)
        loaded_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        loaded_at = "Unknown"
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM ne_nodes WHERE type_name='Site'"
        ).fetchone()["c"]
    return {"loaded_at": loaded_at, "count": count}


def get_site_hardware(site_dn: str) -> dict:
    """
    Return hardware topology for a single site entirely from the local NE DB.
    No API calls — instant.

    Returns a dict with:
      site_dn, site_name, site_status,
      battery_groups: [{dn, name, status, string_count}],
      battery_group_count, battery_string_count,
      rectifier_groups: [{dn, name, status, module_count}],
      rectifier_group_count, rectifier_module_count,
      vision_unit_count,
      controllers: [{dn, name, status, model, tenant_code}],
    """
    site_node = get_ne_by_dn(site_dn)
    if not site_node:
        return {"error": "Site not found", "site_dn": site_dn}

    mo_types = [
        "Battery Group", "Rectifier Group",
        "Vision Lithium Battery", "Controller", "Site Unit",
    ]
    ne_map = get_dns_by_type_under_sites([site_dn], mo_types)

    # ── Build a node_map to walk parent chains ────────────────────
    with get_conn() as _conn:
        _rows = _conn.execute(
            "SELECT dn, name, parent_dn, type_name FROM ne_nodes"
        ).fetchall()
    _node_map = {r["dn"]: dict(r) for r in _rows}

    def _controller_dn(start_dn: str) -> str:
        """Walk up from start_dn and return the first Controller ancestor DN."""
        dn = start_dn
        for _ in range(10):
            node = _node_map.get(dn)
            if not node:
                return ""
            parent_dn = node.get("parent_dn", "")
            if not parent_dn:
                return ""
            parent = _node_map.get(parent_dn)
            if not parent:
                return ""
            if parent.get("type_name") == "Controller":
                return parent_dn
            dn = parent_dn
        return ""

    # ── Battery groups (+ string count per group) ─────────────────
    battery_groups = []
    total_strings = 0
    for bg in sorted(ne_map.get("Battery Group", []), key=lambda x: x["name"]):
        strings = get_children(bg["dn"], ["Battery String"])
        battery_groups.append({
            "dn":           bg["dn"],
            "name":         bg["name"],
            "status":       bg.get("status", ""),
            "string_count": len(strings),
        })
        total_strings += len(strings)

    # ── Name-prefix helpers for ETP48 core model matching ────────────
    # NE hierarchy is flat under Site: Site Units are siblings of Rectifier Groups,
    # not nested children of Controllers. Match by shared name prefix:
    #   "BAR_R0370_Huawei01_Int_BL01_Site Unit"      → prefix "BAR_R0370_Huawei01_Int_BL01"
    #   "BAR_R0370_Huawei01_Int_BL01_Rectifier Group" → prefix "BAR_R0370_Huawei01_Int_BL01"
    # Generic "Site Unit" (SCC800 aggregate) has no "_Site Unit" suffix → ignored.
    def _su_prefix(name: str) -> str:
        suffix = "_Site Unit"
        return name[:-len(suffix)] if name.endswith(suffix) else ""

    def _rg_prefix(name: str) -> str:
        suffix = "_Rectifier Group"
        return name[:-len(suffix)] if name.endswith(suffix) else ""

    prefix_to_su_dn: dict = {}
    unprefixed_su_dns: list = []   # generic "Site Unit" names (no underscore prefix)
    for su_ne in ne_map.get("Site Unit", []):
        px = _su_prefix(su_ne["name"])
        if px:
            prefix_to_su_dn[px] = su_ne["dn"]
        else:
            unprefixed_su_dns.append(su_ne["dn"])

    # Fallback for single-su-dn case: if only one prefixed site unit exists
    # and a rectifier group has no prefix, it's unambiguously that system's unit.
    _single_prefixed_su = list(prefix_to_su_dn.values())[0] if len(prefix_to_su_dn) == 1 else ""

    def _resolve_site_unit_dn(rg_name: str) -> str:
        px = _rg_prefix(rg_name)
        if px:
            return prefix_to_su_dn.get(px, "")
        # Generic rectifier group name (no underscore prefix) → fallback:
        # 1. Only one unprefixed site unit → assign it
        if len(unprefixed_su_dns) == 1:
            return unprefixed_su_dns[0]
        # 2. Only one prefixed site unit in the whole site → assign it
        if _single_prefixed_su:
            return _single_prefixed_su
        return ""

    # ── Rectifier groups (+ module count + module DNs + site_unit_dn) ──
    rectifier_groups = []
    total_modules = 0
    all_rectifier_dns: list = []   # individual Rectifier module DNs across all groups
    for rg in sorted(ne_map.get("Rectifier Group", []), key=lambda x: x["name"]):
        modules = get_children(rg["dn"], ["Rectifier"])
        module_dns = [m["dn"] for m in modules]
        all_rectifier_dns.extend(module_dns)
        rectifier_groups.append({
            "dn":           rg["dn"],
            "name":         rg["name"],
            "status":       rg.get("status", ""),
            "module_count": len(modules),
            "module_dns":   module_dns,
            "site_unit_dn": _resolve_site_unit_dn(rg["name"]),
        })
        total_modules += len(modules)

    # ── Controllers (tenant + model from name pattern) ────────────
    _KNOWN_MODELS = [
        "SCC800", "SCC500", "SCC300", "SCC200",
        "ECC800", "ECC500", "ECC300",
        "SMU01A", "SMU11A",
    ]
    controllers = []
    for c in sorted(ne_map.get("Controller", []), key=lambda x: x["name"]):
        name = c["name"]
        # Tenant code: last '_'-delimited segment (e.g. ROBI01, GP01, BL01)
        parts = name.split("_")
        tenant_code = parts[-1] if len(parts) >= 2 else ""
        # Controller model: look for known model strings anywhere in the name
        model = ""
        name_up = name.upper()
        for m in _KNOWN_MODELS:
            if m in name_up:
                model = m
                break
        controllers.append({
            "dn":          c["dn"],
            "name":        name,
            "status":      c.get("status", ""),
            "model":       model,
            "tenant_code": tenant_code,
        })

    # ── Vision Lithium Battery NEs ────────────────────────────────
    vision_nes = [
        {"dn": ne["dn"], "name": ne["name"], "status": ne.get("status", "")}
        for ne in ne_map.get("Vision Lithium Battery", [])
    ]

    # ── Site Unit NEs (hold ETP48xxx core model via signal 2101653) ──
    site_units = [
        {
            "dn":   ne["dn"],
            "name": ne["name"],
        }
        for ne in ne_map.get("Site Unit", [])
    ]
    site_unit_dns = [su["dn"] for su in site_units]   # flat list for API call

    return {
        "site_dn":               site_dn,
        "site_name":             site_node["name"],
        "site_status":           site_node["status"],
        "battery_groups":        battery_groups,
        "battery_group_count":   len(battery_groups),
        "battery_string_count":  total_strings,
        "rectifier_groups":      rectifier_groups,
        "rectifier_group_count": len(rectifier_groups),
        "rectifier_module_count":total_modules,
        "rectifier_dns":         all_rectifier_dns,   # individual Rectifier NEs → signal 10014 (R48xxx)
        "site_units":            site_units,          # [{dn, name}] → signal 2101653 (ETP48xxx)
        "site_unit_dns":         site_unit_dns,       # flat list kept for backward compat
        "vision_units":          vision_nes,
        "vision_unit_count":     len(vision_nes),
        "controllers":           controllers,
    }


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────
def _get_all_subnet_dns(parent_dn: str) -> list:
    result = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dn FROM ne_nodes WHERE parent_dn=? AND type_name='Subnet'",
            (parent_dn,),
        ).fetchall()
    for r in rows:
        result.append(r["dn"])
        result.extend(_get_all_subnet_dns(r["dn"]))
    return result


def _get_child_regions(parent_dn: str, region_map: dict) -> list:
    children = [r for r in region_map.values() if r["parent_dn"] == parent_dn]
    for c in children:
        c["children"] = _get_child_regions(c["dn"], region_map)
        c["site_count"] = _count_sites_under(c["dn"])
    return children


def _count_sites_under(region_dn: str) -> int:
    all_dns = _get_all_subnet_dns(region_dn) + [region_dn]
    ph = ",".join("?" * len(all_dns))
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) as c FROM ne_nodes WHERE type_name='Site' AND parent_dn IN ({ph})",
            all_dns,
        ).fetchone()
    return row["c"] if row else 0


def _load_children_recursive(parent_dn: str, depth: int, max_depth: int) -> list:
    if depth >= max_depth:
        return []
    children = get_children(parent_dn)
    for c in children:
        c["children"] = _load_children_recursive(c["dn"], depth + 1, max_depth)
    return children


def _safe_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────
# Signal Dictionary  (loaded from "MO Types and Signal..." CSV)
# ─────────────────────────────────────────────────────────────────

# Map lowercase signal_type keys (used by the rest of the app) to
# the exact Signal Attribute values stored in the CSV / SQLite.
_ATTR_MAP = {
    "sampling":  "Sampling",
    "config":    "Config",
    "statistic": "Statistic",
}


def load_signal_dict_from_csv(csv_path: str) -> int:
    """
    Parse the 'MO Types and Signal Information…' CSV (File A) and load every
    WebService-supported row into the signal_dict table.

    Join key used downstream:
        signal_dict.mo_type_id  ← A col E  (Managed Object Type ID)
        signal_dict.ver_id      ← A col C  (Root Device Type Version ID)
        → matches ne_nodes.type_id / ne_nodes.type_ver_id  (from CM-MO CSV / API)

    Returns the number of rows inserted.
    """
    init_db()
    count = 0
    with get_conn() as conn:
        conn.execute("DELETE FROM signal_dict")
        batch = []
        with open(csv_path, encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                mo_type_id = _safe_int(row.get("Managed Object Type ID", "").strip())
                ver_id     = _safe_int(row.get("Root Device Type Version ID", "").strip())
                signal_id  = _safe_int(row.get("Signal ID", "").strip())
                if mo_type_id is None or ver_id is None or signal_id is None:
                    continue
                signal_attr = row.get("Signal Attribute", "").strip()
                if signal_attr not in ("Sampling", "Config", "Statistic"):
                    continue
                batch.append((
                    mo_type_id,
                    ver_id,
                    signal_attr,
                    signal_id,
                    row.get("Signal Name", "").strip(),
                    row.get("Unit", "").strip(),
                ))
                count += 1
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO signal_dict VALUES (?,?,?,?,?,?)", batch
                    )
                    batch.clear()
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO signal_dict VALUES (?,?,?,?,?,?)", batch
            )
    return count


def signal_dict_has_data() -> bool:
    """Return True if the signal dictionary has been loaded."""
    try:
        init_db()
        with get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM signal_dict").fetchone()
            return row["c"] > 0
    except Exception:
        return False


def signal_dict_stats() -> dict:
    """Return row counts per signal attribute for the status panel."""
    try:
        init_db()
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT signal_attr, COUNT(*) AS c FROM signal_dict GROUP BY signal_attr"
            ).fetchall()
        return {r["signal_attr"]: r["c"] for r in rows}
    except Exception:
        return {}


def get_signal_label(type_id: int, type_ver_id: int, signal_id: int) -> dict:
    """
    Return {'name': ..., 'unit': ...} for a single signal from the dictionary.
    Tries exact (type_id, type_ver_id) first, then falls back to any ver_id
    for that type_id.  Returns empty strings if not found.
    """
    try:
        init_db()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT signal_name, unit FROM signal_dict "
                "WHERE mo_type_id=? AND ver_id=? AND signal_id=? LIMIT 1",
                (type_id, type_ver_id, signal_id),
            ).fetchone()
            if row:
                return {"name": row["signal_name"], "unit": row["unit"]}
            # Fallback: any ver_id for this type_id
            row = conn.execute(
                "SELECT signal_name, unit FROM signal_dict "
                "WHERE mo_type_id=? AND signal_id=? LIMIT 1",
                (type_id, signal_id),
            ).fetchone()
            return {"name": row["signal_name"], "unit": row["unit"]} if row else {"name": "", "unit": ""}
    except Exception:
        return {"name": "", "unit": ""}


def get_signal_labels_bulk(type_id: int, type_ver_id: int, signal_ids: list) -> dict:
    """
    Return {signal_id: {'name': ..., 'unit': ...}} for a batch of signal IDs.
    More efficient than calling get_signal_label() in a loop.
    """
    if not signal_ids:
        return {}
    try:
        init_db()
        result = {}
        with get_conn() as conn:
            # Exact ver_id match
            ph = ",".join("?" * len(signal_ids))
            rows = conn.execute(
                f"SELECT signal_id, signal_name, unit FROM signal_dict "
                f"WHERE mo_type_id=? AND ver_id=? AND signal_id IN ({ph})",
                [type_id, type_ver_id] + list(signal_ids),
            ).fetchall()
            for r in rows:
                result[r["signal_id"]] = {"name": r["signal_name"], "unit": r["unit"]}

            # Fill in any missing via fallback (any ver_id)
            missing = [sid for sid in signal_ids if sid not in result]
            if missing:
                ph2 = ",".join("?" * len(missing))
                rows2 = conn.execute(
                    f"SELECT signal_id, signal_name, unit FROM signal_dict "
                    f"WHERE mo_type_id=? AND signal_id IN ({ph2}) "
                    f"GROUP BY signal_id",
                    [type_id] + missing,
                ).fetchall()
                for r in rows2:
                    if r["signal_id"] not in result:
                        result[r["signal_id"]] = {"name": r["signal_name"], "unit": r["unit"]}
        return result
    except Exception:
        return {}


def get_signals_for_ne(ne_dn: str, signal_type: str) -> list:
    """
    Return the signal list for a given NE DN and signal type
    ('sampling' | 'config' | 'statistic').

    Lookup chain:
      1. Get type_id + type_ver_id from ne_nodes for ne_dn.
      2. Query signal_dict with exact (type_id, type_ver_id, signal_attr).
      3. If no match, fall back to the nearest ver_id available for that type_id
         (prefer the highest ver_id ≤ actual; if none, the lowest available).

    Returns list of {id, name, unit} dicts, sorted by signal name.
    """
    init_db()
    signal_attr = _ATTR_MAP.get(signal_type.lower())
    if not signal_attr:
        return []

    with get_conn() as conn:
        ne_row = conn.execute(
            "SELECT type_id, type_ver_id FROM ne_nodes WHERE dn = ?", (ne_dn,)
        ).fetchone()

    if not ne_row or ne_row["type_id"] is None:
        return []

    type_id     = ne_row["type_id"]
    type_ver_id = ne_row["type_ver_id"] or 0

    def _query(vid):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT signal_id, signal_name, unit FROM signal_dict "
                "WHERE mo_type_id = ? AND ver_id = ? AND signal_attr = ? "
                "ORDER BY signal_name",
                (type_id, vid, signal_attr),
            ).fetchall()
        return [{"id": r["signal_id"], "name": r["signal_name"], "unit": r["unit"]} for r in rows]

    # 1 — exact match
    signals = _query(type_ver_id)
    if signals:
        return signals

    # 2 — nearest available ver_id for this type_id
    with get_conn() as conn:
        ver_rows = conn.execute(
            "SELECT DISTINCT ver_id FROM signal_dict WHERE mo_type_id = ? ORDER BY ver_id",
            (type_id,),
        ).fetchall()
    available = [r["ver_id"] for r in ver_rows]
    if not available:
        return []

    # Prefer highest ver_id that is ≤ actual; fall back to lowest if none qualify
    candidates = [v for v in available if v <= type_ver_id]
    best_vid   = max(candidates) if candidates else min(available)
    return _query(best_vid)
