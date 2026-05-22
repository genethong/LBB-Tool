"""
NetEco Tool — Flask Web Application
Phase 1: Data Extractor + Predefined Reports
Phase 2: Battery Backup Analysis
"""
from __future__ import annotations
import io
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
from flask import (
    Flask, jsonify, render_template, request,
    send_file, session, Response, stream_with_context, redirect, url_for, g
)

import threading
import ne_tree
import reports as rpt
import analysis as ana
import lbb_validation as lbb_val
import user_db as udb
from neteco_client import NetEcoClient, NetEcoAPIError

# ── NE tree refresh progress state ────────────
_ne_refresh_state: dict = {"stage": "idle", "message": "", "extra": {}}
_ne_refresh_lock  = threading.Lock()

# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_DIR = os.path.join(_BASE_DIR, "instance")
EXPORTS_DIR   = os.path.join(_BASE_DIR, "exports")
os.makedirs(_INSTANCE_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────
LOG_FILE = os.path.join(_INSTANCE_DIR, "neteco_tool.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),          # also prints to Terminal
    ]
)
log = logging.getLogger("neteco_tool")
log.info("=== NetEco Tool starting ===")

app = Flask(__name__, instance_path=_INSTANCE_DIR)

# ── Stable SECRET_KEY — persisted to instance/secret_key.txt ──────
# Generated once on first run, then reused across restarts so that
# existing user sessions survive a gunicorn restart.
_sk_file = os.path.join(_INSTANCE_DIR, "secret_key.txt")
if os.path.exists(_sk_file):
    with open(_sk_file) as _f:
        _SECRET = _f.read().strip()
else:
    import secrets as _sec
    _SECRET = _sec.token_hex(32)
    with open(_sk_file, "w") as _f:
        _f.write(_SECRET)
app.secret_key = _SECRET

# ── Auto-load signal dictionary on startup ────
_SIGNAL_DICT_CSV = os.path.join(_BASE_DIR, "data", "signal_dict.csv")

def _ensure_signal_dict():
    if not os.path.isfile(_SIGNAL_DICT_CSV):
        log.warning("signal_dict.csv not found at %s — signal browser will be unavailable", _SIGNAL_DICT_CSV)
        return
    if ne_tree.signal_dict_has_data():
        stats = ne_tree.signal_dict_stats()
        log.info("Signal dictionary already loaded: %s", stats)
        return
    log.info("Loading signal dictionary from %s…", _SIGNAL_DICT_CSV)
    try:
        count = ne_tree.load_signal_dict_from_csv(_SIGNAL_DICT_CSV)
        log.info("Signal dictionary loaded: %d signals (%s)", count, ne_tree.signal_dict_stats())
    except Exception as exc:
        log.error("Failed to load signal dictionary: %s", exc)

_ensure_signal_dict()

CONFIG_FILE = os.path.join(_INSTANCE_DIR, "config.json")
ne_tree.init_db()
log.info("NE cache database initialised")
udb.init_db()
log.info("User database initialised — default admin: admin / admin123 (change on first login)")

# ─────────────────────────────────────────────
# Global error handler — logs every exception
# ─────────────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    log.error("Unhandled exception:\n%s", traceback.format_exc())
    return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


# ─────────────────────────────────────────────
# Request logging
# ─────────────────────────────────────────────
@app.before_request
def log_request():
    log.info("→ %s %s", request.method, request.path)


@app.after_request
def log_response(response):
    log.info("← %s %s %s", request.method, request.path, response.status_code)
    return response


# ─────────────────────────────────────────────
# Auth — context processor + guard
# ─────────────────────────────────────────────

@app.context_processor
def _inject_current_user():
    """Make current_user available in every template automatically."""
    uid = session.get("user_id")
    if uid:
        return {"current_user": udb.get_user_by_id(uid)}
    return {"current_user": None}


@app.before_request
def _require_auth():
    """
    Enforce login on every route except:
      - the login page itself
      - static assets
    API routes return JSON 401; page routes redirect to /login.
    """
    open_endpoints = {"login_page", "static"}
    if request.endpoint in open_endpoints:
        return  # publicly accessible

    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required", "redirect": "/login"}), 401
        return redirect(url_for("login_page", next=request.path))


# ─────────────────────────────────────────────
# Jinja filters
# ─────────────────────────────────────────────
@app.template_filter("format_number")
def format_number(val):
    try:
        return f"{int(val):,}"
    except (TypeError, ValueError):
        return val


# ─────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────
def load_config() -> dict:
    # Environment variables take priority (Render / cloud deployment)
    if os.environ.get("NETECO_URL"):
        return {
            "neteco_url": os.environ["NETECO_URL"],
            "username":   os.environ.get("NETECO_USERNAME", ""),
            "password":   os.environ.get("NETECO_PASSWORD", ""),
        }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_client() -> NetEcoClient | None:
    cfg = load_config()
    if not cfg.get("neteco_url"):
        return None
    client = NetEcoClient(cfg["neteco_url"], cfg["username"], cfg["password"])
    if "openid" in session:
        client.openid = session["openid"]
        client._openid_time = session.get("openid_time", 0)
    return client


def refresh_session(client: NetEcoClient):
    session["openid"] = client.openid
    session["openid_time"] = client._openid_time or time.time()


def fresh_login_client() -> tuple:
    """
    Always do a fresh login before a user-triggered data extraction.
    Returns (client, error_response) — if error_response is not None, return it immediately.
    This guarantees a valid token for every Extract / Report / Analysis call.
    """
    client = get_client()
    if not client:
        return None, jsonify({"error": "NetEco not configured — go to Setup first"})
    ok, msg = client.login()
    if not ok:
        log.error("fresh_login_client failed: %s", msg)
        return None, jsonify({"error": f"Login failed: {msg}"})
    refresh_session(client)
    log.info("fresh_login_client: new token obtained")
    return client, None


# ─────────────────────────────────────────────
# Log viewer page
# ─────────────────────────────────────────────
@app.route("/logs")
def view_logs():
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        # Last 200 lines, newest first
        content = "".join(reversed(lines[-200:]))
    except FileNotFoundError:
        content = "(No log file yet)"
    return f"""<!DOCTYPE html><html><head><title>NetEco Logs</title>
    <style>body{{background:#0f172a;color:#94a3b8;font-family:monospace;padding:20px;}}
    pre{{white-space:pre-wrap;word-break:break-all;font-size:12px;}}
    a{{color:#3b82f6;}}</style></head><body>
    <a href="/">← Dashboard</a>&nbsp;&nbsp;
    <a href="/logs" onclick="location.reload();return false;">🔄 Refresh</a>
    <h3 style="color:#e2e8f0;">NetEco Tool — Log (last 200 lines, newest first)</h3>
    <pre>{content}</pre></body></html>"""


# ─────────────────────────────────────────────
# Login / logout
# ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("user_id"):
        return redirect("/")
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = udb.verify_login(username, password)
        if user:
            session["user_id"]   = user["id"]
            session["username"]  = user["username"]
            session["is_admin"]  = bool(user["is_admin"])
            log.info("LOGIN: user '%s' authenticated", username)
            return redirect(request.args.get("next") or "/")
        error = "Invalid username or password"
        log.warning("LOGIN: failed attempt for username '%s'", username)
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    username = session.get("username", "?")
    session.clear()
    log.info("LOGOUT: user '%s' logged out", username)
    return redirect("/login")


# ─────────────────────────────────────────────
# Admin — user management
# ─────────────────────────────────────────────

@app.route("/admin/users")
def admin_users_page():
    if not session.get("is_admin"):
        return "Forbidden — admin access required", 403
    users = udb.list_users()
    return render_template("admin_users.html", users=users)


@app.route("/api/admin/users", methods=["GET", "POST"])
def api_admin_users():
    if not session.get("is_admin"):
        return jsonify({"error": "Forbidden"}), 403
    if request.method == "GET":
        return jsonify(udb.list_users())
    data = request.json or {}
    ok, msg = udb.create_user(
        data.get("username", ""),
        data.get("password", ""),
        is_admin=bool(data.get("is_admin", False)),
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
def api_admin_delete_user(uid):
    if not session.get("is_admin"):
        return jsonify({"error": "Forbidden"}), 403
    if uid == session.get("user_id"):
        return jsonify({"ok": False, "message": "You cannot delete your own account"})
    ok, msg = udb.delete_user(uid)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/admin/users/<int:uid>/reset-password", methods=["POST"])
def api_admin_reset_password(uid):
    if not session.get("is_admin"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    ok, msg = udb.change_password(uid, data.get("password", ""))
    return jsonify({"ok": ok, "message": msg})


# ─────────────────────────────────────────────
# User profile, password, history
# ─────────────────────────────────────────────

@app.route("/profile")
def profile_page():
    uid = session["user_id"]
    run_hist = udb.get_run_history(uid, limit=25)
    lbb_hist = udb.get_lbb_history(uid, limit=15)
    site_sels = udb.get_all_site_selections(uid)
    return render_template("profile.html",
                           run_history=run_hist,
                           lbb_history=lbb_hist,
                           site_selections=site_sels)


@app.route("/api/user/change-password", methods=["POST"])
def api_change_password():
    data = request.json or {}
    current_pwd = data.get("current_password", "")
    new_pwd     = data.get("new_password", "")
    # Verify current password before allowing change
    user = udb.get_user_by_username(session["username"])
    from werkzeug.security import check_password_hash as _chk
    if not user or not _chk(user["password_hash"], current_pwd):
        return jsonify({"ok": False, "message": "Current password is incorrect"})
    ok, msg = udb.change_password(session["user_id"], new_pwd)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/user/history")
def api_user_history():
    uid = session["user_id"]
    return jsonify({
        "run_history": udb.get_run_history(uid, limit=25),
        "lbb_history": udb.get_lbb_history(uid, limit=15),
    })


@app.route("/api/user/site-selection")
def api_get_site_selection():
    tool = request.args.get("tool", "").strip()
    if not tool:
        return jsonify([])
    return jsonify(udb.get_site_selection(session["user_id"], tool))


# ─────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────
@app.route("/")
def index():
    cfg = load_config()
    stats = ne_tree.get_stats() if ne_tree.has_data() else {}
    return render_template("index.html", configured=bool(cfg.get("neteco_url")),
                           has_data=ne_tree.has_data(), stats=stats)


@app.route("/setup")
def setup():
    cfg = load_config()
    return render_template("setup.html", config=cfg)


@app.route("/extractor")
def extractor():
    if not ne_tree.has_data():
        return render_template("no_data.html")
    regions = ne_tree.get_region_tree()
    return render_template("extractor.html", regions=regions)


@app.route("/reports")
def reports_page():
    if not ne_tree.has_data():
        return render_template("no_data.html")
    regions = ne_tree.get_region_tree()
    return render_template("reports.html", regions=regions, reports=rpt.REPORTS)


@app.route("/analysis")
def analysis_page():
    if not ne_tree.has_data():
        return render_template("no_data.html")
    regions = ne_tree.get_region_tree()
    return render_template("analysis.html", regions=regions)


@app.route("/lbb-validation")
def lbb_validation_page():
    if not ne_tree.has_data():
        return render_template("no_data.html")
    return render_template("lbb_validation.html")


# ─────────────────────────────────────────────
# API — Setup & Connection
# ─────────────────────────────────────────────
@app.route("/api/public-ip")
def api_public_ip():
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=6)
        return jsonify(r.json())
    except Exception:
        return jsonify({"ip": "Unable to detect"})


@app.route("/api/save-config", methods=["POST"])
def api_save_config():
    data = request.json
    save_config({
        "neteco_url": data.get("neteco_url", "").rstrip("/"),
        "username": data.get("username", ""),
        "password": data.get("password", ""),
    })
    return jsonify({"ok": True})


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    data = request.json
    client = NetEcoClient(
        data.get("neteco_url", "").rstrip("/"),
        data.get("username", ""),
        data.get("password", ""),
    )
    ok, msg = client.login()
    if ok:
        refresh_session(client)
        client.logout()
    return jsonify({"success": ok, "message": msg})


@app.route("/api/login", methods=["POST"])
def api_login():
    client = get_client()
    if not client:
        return jsonify({"success": False, "message": "Not configured"})
    ok, msg = client.login()
    if ok:
        refresh_session(client)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/connection-status")
def api_connection_status():
    cfg = load_config()
    if not cfg.get("neteco_url"):
        return jsonify({"configured": False, "connected": False})
    # ── IMPORTANT: Do NOT re-login if we already have a fresh token. ──
    # Each login() call creates a NEW openid and invalidates the old one,
    # breaking any in-flight API calls that still hold the previous token.
    if "openid" in session and session.get("openid_time", 0):
        age = time.time() - session["openid_time"]
        if age < 1500:   # token is < 25 min old — still valid
            return jsonify({"configured": True, "connected": True, "message": "Connected"})
    # No valid cached token — do a fresh login
    client = get_client()
    try:
        ok, msg = client.login()
        if ok:
            refresh_session(client)
        return jsonify({"configured": True, "connected": ok, "message": msg})
    except Exception as e:
        return jsonify({"configured": True, "connected": False, "message": str(e)})


# ─────────────────────────────────────────────
# API — NE Tree
# ─────────────────────────────────────────────
@app.route("/api/regions")
def api_regions():
    return jsonify(ne_tree.get_region_tree())


@app.route("/api/sites")
def api_sites():
    region_dn = request.args.get("region_dn")
    tenant = request.args.get("tenant", "").upper()
    status = request.args.get("status")  # CONNECTED | DISCONNECTED | None

    if region_dn:
        sites = ne_tree.get_sites_in_region(region_dn)
    else:
        sites = ne_tree.get_all_sites(status_filter=status)

    # Filter by tenant code if provided
    if tenant:
        sites = [s for s in sites if _tenant_matches(s["name"], tenant)]

    return jsonify(sites)


@app.route("/api/search-sites")
def api_search_sites():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    results = ne_tree.search_sites(q, limit=50)
    return jsonify(results)


@app.route("/api/refresh-ne-tree", methods=["POST"])
def api_refresh_ne_tree():
    """Refresh NE tree from NetEco API (blocking, kept for backward compat)."""
    client = get_client()
    if not client:
        return jsonify({"success": False, "message": "Not configured"})
    try:
        count = ne_tree.load_from_api(client)
        refresh_session(client)
        return jsonify({"success": True, "message": f"Loaded {count:,} NE nodes from NetEco"})
    except NetEcoAPIError as e:
        return jsonify({"success": False, "message": str(e)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/refresh-ne-tree/stream")
def api_refresh_ne_tree_stream():
    """SSE stream: runs NE tree refresh in background thread, streams live progress."""
    client = get_client()
    if not client:
        def _err():
            yield f"data: {json.dumps({'stage':'error','message':'Not configured','extra':{}})}\n\n"
        return Response(stream_with_context(_err()), content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _run():
        global _ne_refresh_state
        def _cb(stage, message, extra):
            with _ne_refresh_lock:
                _ne_refresh_state = {"stage": stage, "message": message, "extra": extra}
        try:
            ne_tree.load_from_api(client, progress_cb=_cb)
            refresh_session(client)
        except Exception as exc:
            with _ne_refresh_lock:
                _ne_refresh_state = {"stage": "error", "message": str(exc), "extra": {}}

    @stream_with_context
    def _generate():
        global _ne_refresh_state
        # Reset state and kick off background thread
        with _ne_refresh_lock:
            _ne_refresh_state = {"stage": "starting", "message": "Starting…", "extra": {}}
        t = threading.Thread(target=_run, daemon=True)
        t.start()

        last_sent = None
        while True:
            with _ne_refresh_lock:
                state = dict(_ne_refresh_state)
            # Only send when state changes (avoid flooding)
            if state != last_sent:
                yield f"data: {json.dumps(state)}\n\n"
                last_sent = dict(state)
            if state["stage"] in ("done", "error"):
                break
            time.sleep(0.4)
        # Final flush in case thread finished between checks
        with _ne_refresh_lock:
            state = dict(_ne_refresh_state)
        if state != last_sent:
            yield f"data: {json.dumps(state)}\n\n"
        yield "data: {\"stage\":\"end\"}\n\n"

    return Response(_generate(), content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/upload-cm-mo", methods=["POST"])
def api_upload_cm_mo():
    """Accept a CM-MO CSV upload and import it.
    Uses a UUID-suffixed temp file so concurrent uploads don't overwrite each other.
    """
    import uuid as _uuid
    if "file" not in request.files:
        return jsonify({"success": False, "message": "No file provided"})
    f = request.files["file"]
    if not f.filename.endswith(".csv"):
        return jsonify({"success": False, "message": "Please upload a .csv file"})
    # Unique temp path per request — safe for concurrent uploads
    tmp_path = os.path.join(_INSTANCE_DIR, f"cm_mo_upload_{_uuid.uuid4().hex}.csv")
    try:
        f.save(tmp_path)
        count = ne_tree.load_from_csv(tmp_path)
        return jsonify({"success": True, "message": f"Imported {count:,} NE nodes from CSV"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)  # always clean up the temp file


# ─────────────────────────────────────────────
# API — Custom Data Extract
# ─────────────────────────────────────────────
@app.route("/api/signals/available", methods=["POST"])
def api_signals_available():
    """
    Given a list of site DNs and MO type names, return available NE counts.
    Used to populate the signal picker UI.
    """
    data = request.json
    site_dns = data.get("site_dns", [])
    mo_types = data.get("mo_types", [
        "Battery Group", "Battery String", "Rectifier Group", "Rectifier",
        "Digital Power", "DC Output Distribution", "Mains", "AC Input Distribution",
        "Site", "Genset",
    ])
    if not site_dns:
        return jsonify({})
    result = ne_tree.get_dns_by_type_under_sites(site_dns, mo_types)
    return jsonify({k: len(v) for k, v in result.items()})


@app.route("/api/ne-types")
def api_ne_types():
    """Return all NE type_names actually stored in the DB with counts.
    Lets users verify the exact name used (e.g. 'Battery Group' vs 'BatteryGroup').
    """
    return jsonify(ne_tree.get_all_type_names())


@app.route("/api/ne-hierarchy", methods=["POST"])
def api_ne_hierarchy():
    """
    Return NEs of the selected type grouped under their site, with parent chain.
    Reads local DB only — no API call needed. Fast.
    """
    data     = request.json
    site_dns = data.get("site_dns", [])
    mo_type  = data.get("mo_type", "")
    if not site_dns or not mo_type:
        return jsonify({"error": "site_dns and mo_type required"})
    tree = ne_tree.get_ne_tree_for_type(site_dns, mo_type)
    return jsonify(tree)


@app.route("/api/debug/system-type")
def api_debug_system_type():
    """
    Debug: query signal 2101653 (System Type / core model) against EVERY NE under a
    site and return which DNs respond with a non-empty value.
    Usage: GET /api/debug/system-type?site_dn=<dn>
    """
    site_dn = request.args.get("site_dn", "").strip()
    if not site_dn:
        return jsonify({"error": "site_dn query param required"})

    client, err = fresh_login_client()
    if err:
        return err

    try:
        # Get every NE that lives under this site from the local cache
        import sqlite3 as _sq
        with ne_tree.get_conn() as conn:
            rows = conn.execute(
                "SELECT dn, name, type_name FROM ne_nodes WHERE parent_dn = ?",
                (site_dn,)
            ).fetchall()

        all_dns   = [r["dn"]       for r in rows]
        dn_meta   = {r["dn"]: {"name": r["name"], "type_name": r["type_name"]} for r in rows}

        hits = []
        if all_dns:
            raw = client.get_sampling(all_dns, [2101653])
            for item in raw:
                val = (item.get("signalValue") or item.get("value") or "").strip()
                if val:
                    dn = item.get("dn", "")
                    meta = dn_meta.get(dn, {})
                    hits.append({
                        "dn":        dn,
                        "name":      meta.get("name", ""),
                        "type_name": meta.get("type_name", ""),
                        "value":     val,
                    })

        return jsonify({
            "site_dn":       site_dn,
            "nes_queried":   len(all_dns),
            "hits":          hits,
            "raw_response":  raw[:20] if all_dns else [],  # first 20 raw items for inspection
        })
    except Exception as e:
        log.exception("debug/system-type error")
        return jsonify({"error": str(e)})


@app.route("/api/signal-discover", methods=["POST"])
def api_signal_discover():
    """
    Return the signal list for a given NE DN and signal type, sourced from
    the local signal dictionary (File A loaded into SQLite).  No live API
    call is made — the dictionary must be loaded first via /api/signal-dict/load.

    Payload:
        ne_dn        — DN of the NE to look up
        signal_type  — 'sampling' | 'config' | 'statistic'

    Response:
        {signals: [{id, name, unit}], ne_name, source: 'dict'}
    """
    data      = request.json or {}
    ne_dn     = data.get("ne_dn")
    sig_type  = data.get("signal_type", "sampling").lower()

    if not ne_dn:
        return jsonify({"error": "ne_dn is required"})

    if not ne_tree.signal_dict_has_data():
        return jsonify({
            "error": "Signal dictionary not ready — please restart the app."
        })

    signals = ne_tree.get_signals_for_ne(ne_dn, sig_type)
    ne_node = ne_tree.get_ne_by_dn(ne_dn)
    ne_name = ne_node["name"] if ne_node else ne_dn

    log.info("signal-discover (dict): %d %s signals for NE %s", len(signals), sig_type, ne_name)
    return jsonify({"signals": signals, "ne_name": ne_name, "source": "dict"})


@app.route("/api/signal-dict/status")
def api_signal_dict_status():
    """Return current signal dictionary load status."""
    has = ne_tree.signal_dict_has_data()
    stats = ne_tree.signal_dict_stats() if has else {}
    total = sum(stats.values())
    return jsonify({"loaded": has, "total": total, "stats": stats})


@app.route("/api/download-export")
def api_download_export():
    """
    Serve a previously saved export file as a browser download.
    Query param: path (absolute server path inside EXPORTS_DIR).
    """
    path = request.args.get("path", "").strip()
    if not path:
        return "Missing path", 400
    # Security: must be inside EXPORTS_DIR
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(EXPORTS_DIR)):
        return "Forbidden", 403
    if not os.path.isfile(real):
        return "File not found", 404
    return send_file(real, as_attachment=True,
                     download_name=os.path.basename(real))


@app.route("/api/extract", methods=["POST"])
def api_extract():
    """
    Custom extraction: user specifies site DNs, MO type, signal IDs, signal type.
    """
    data = request.json
    site_dns   = data.get("site_dns", [])
    mo_type    = data.get("mo_type", "")
    signal_ids = data.get("signal_ids", [])
    sig_type   = data.get("signal_type", "sampling")  # sampling | config | statistic
    start_ms   = data.get("start_ms")
    end_ms     = data.get("end_ms")

    if not site_dns:
        return jsonify({"error": "No sites selected"})
    if not mo_type:
        return jsonify({"error": "No MO type selected"})

    client, err = fresh_login_client()
    if err:
        return err

    try:
        # Find NE DNs of the requested type under selected sites
        log.info("EXTRACT: site_dns=%s mo_type=%s sig_type=%s", site_dns, mo_type, sig_type)
        ne_map = ne_tree.get_dns_by_type_under_sites(site_dns, [mo_type])
        nes = ne_map.get(mo_type, [])
        log.info("EXTRACT: found %d NE(s) of type '%s' under %d site(s)", len(nes), mo_type, len(site_dns))
        if not nes:
            log.warning("EXTRACT: 0 NEs found — check that CM-MO data has '%s' entries linked to the selected sites", mo_type)
            return jsonify({"columns": [], "rows": [], "count": 0,
                            "message": f"No {mo_type} NEs found under selected sites. "
                                       f"This usually means the CM-MO data doesn't have {mo_type} "
                                       f"entries linked to those site DNs. Try refreshing NE tree from API."})

        dns = [ne["dn"] for ne in nes]
        dn_to_name = {ne["dn"]: ne["name"] for ne in nes}

        if sig_type == "sampling":
            raw = client.get_sampling(dns, signal_ids if signal_ids else None)
        elif sig_type == "config":
            raw = client.get_config_signals(dns, signal_ids if signal_ids else None)
        elif sig_type == "statistic":
            if not start_ms or not end_ms:
                return jsonify({"error": "Date range required for statistical data"})
            # signalIds is mandatory per API spec — resolve from dict when not specified
            stat_signal_ids = signal_ids or []
            if not stat_signal_ids and dns:
                dict_sigs = ne_tree.get_signals_for_ne(dns[0], "statistic")
                stat_signal_ids = [s["id"] for s in dict_sigs]
            if not stat_signal_ids:
                return jsonify({"error": "No statistical signals found for this MO type in the signal dictionary"})
            raw = client.get_statistic(dns, stat_signal_ids, int(start_ms), int(end_ms))
        else:
            return jsonify({"error": f"Unknown signal type: {sig_type}"})

        refresh_session(client)

        # Enrich signal names/units from local dictionary
        first_dn = dns[0] if dns else None
        first_ne_info = {}
        if first_dn:
            with ne_tree.get_conn() as _c:
                _r = _c.execute(
                    "SELECT type_id, type_ver_id FROM ne_nodes WHERE dn=?", (first_dn,)
                ).fetchone()
                if _r:
                    first_ne_info = {"type_id": _r["type_id"], "type_ver_id": _r["type_ver_id"] or 0}
        seen_sids  = set(int(i.get("signalId", 0)) for i in raw if str(i.get("signalId","")).strip().lstrip("-").isdigit())
        sig_labels = ne_tree.get_signal_labels_bulk(
            first_ne_info.get("type_id", 0),
            first_ne_info.get("type_ver_id", 0),
            list(seen_sids),
        ) if first_ne_info else {}

        rows = []
        for item in raw:
            dn    = item.get("dn", "")
            sid_i = int(item["signalId"]) if str(item.get("signalId","")).strip().lstrip("-").isdigit() else 0
            lbl   = sig_labels.get(sid_i, {})
            rows.append({
                "Timestamp":   _fmt_ts(item.get("signalResultTime") or item.get("collectTime") or
                                       item.get("statisticTime") or item.get("endTime")),
                "NE Name":     dn_to_name.get(dn, dn),
                "Signal Name": lbl.get("name") or item.get("signalName", ""),
                "Value":       (item.get("signalValue") or item.get("value") or
                                item.get("statisticValue") or ""),
                "Unit":        lbl.get("unit") or item.get("unit", ""),
            })

        cols = ["Timestamp", "NE Name", "Signal Name", "Value", "Unit"]
        if not rows:
            return jsonify({"columns": cols, "rows": [], "count": 0,
                            "message": "API returned no signal data for the selected NEs."})
        title = f"Extract_{mo_type.replace(' ','_')}_{sig_type}"
        saved_path = _save_excel(rows, cols, title)
        log.info("EXTRACT: saved %d rows → %s", len(rows), saved_path)
        # ── Save per-user history + last site selection ──
        try:
            udb.add_run_history(session["user_id"], "extract",
                                f"{mo_type} ({sig_type}) — {len(site_dns)} site(s)",
                                len(rows), saved_path)
            udb.save_site_selection(session["user_id"], "extractor", site_dns)
        except Exception as _he:
            log.warning("Could not save run history: %s", _he)
        return jsonify({"columns": cols, "rows": rows, "count": len(rows), "saved_path": saved_path})

    except NetEcoAPIError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/alarms", methods=["POST"])
def api_alarms():
    data = request.json
    site_dns = data.get("site_dns", [])
    mode     = data.get("mode", "current")  # current | history
    start_ms = data.get("start_ms")
    end_ms   = data.get("end_ms")

    client, err = fresh_login_client()
    if err:
        return err
    try:
        if mode == "current":
            raw = client.get_active_alarms(dns=site_dns if site_dns else None)
        else:
            if not start_ms or not end_ms:
                return jsonify({"error": "Date range required for historical alarms"})
            raw = client.get_historical_alarms(int(start_ms), int(end_ms),
                                               dns=site_dns if site_dns else None)
        refresh_session(client)
        result = rpt._format_alarm_results(raw, mode)
        return jsonify(result)
    except NetEcoAPIError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": str(e)})


# ─────────────────────────────────────────────
# API — Predefined Reports
# ─────────────────────────────────────────────
@app.route("/api/reports/run", methods=["POST"])
def api_run_report():
    data = request.json
    report_id = data.get("report_id")
    site_dns  = data.get("site_dns", [])
    start_ms  = data.get("start_ms")
    end_ms    = data.get("end_ms")

    if not site_dns:
        return jsonify({"error": "No sites selected"})

    client, err = fresh_login_client()
    if err:
        return err

    try:
        result = rpt.run_report(
            report_id, site_dns, client, ne_tree,
            start_ms=int(start_ms) if start_ms else None,
            end_ms=int(end_ms) if end_ms else None,
        )
        refresh_session(client)
        if result.get("rows"):
            saved_path = _save_excel(result["rows"], result.get("columns", []), f"Report_{report_id}")
            result["saved_path"] = saved_path
            log.info("REPORT: saved %d rows → %s", len(result["rows"]), saved_path)
            try:
                udb.add_run_history(session["user_id"], "report",
                                    f"{report_id} — {len(site_dns)} site(s)",
                                    len(result["rows"]), saved_path)
                udb.save_site_selection(session["user_id"], "reports", site_dns)
            except Exception as _he:
                log.warning("Could not save run history: %s", _he)
        return jsonify(result)
    except NetEcoAPIError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": str(e)})


# ─────────────────────────────────────────────
# API — Battery Backup Analysis
# ─────────────────────────────────────────────
@app.route("/api/analysis/battery-backup", methods=["POST"])
def api_battery_backup():
    data = request.json
    site_dns = data.get("site_dns", [])
    start_ms = data.get("start_ms")
    end_ms   = data.get("end_ms")

    if not site_dns:
        return jsonify({"error": "No sites selected"})
    if not start_ms or not end_ms:
        return jsonify({"error": "Date range required"})

    client, err = fresh_login_client()
    if err:
        return err

    try:
        result = ana.run_backup_analysis(
            site_dns, int(start_ms), int(end_ms), client, ne_tree
        )
        refresh_session(client)
        if result.get("rows"):
            saved_path = _save_excel(result["rows"], result.get("columns", []), "BatteryBackup_Analysis")
            result["saved_path"] = saved_path
            log.info("ANALYSIS: saved %d rows → %s", len(result["rows"]), saved_path)
            try:
                udb.add_run_history(session["user_id"], "analysis",
                                    f"Battery Backup — {len(site_dns)} site(s)",
                                    len(result["rows"]), saved_path)
                udb.save_site_selection(session["user_id"], "analysis", site_dns)
            except Exception as _he:
                log.warning("Could not save run history: %s", _he)
        return jsonify(result)
    except NetEcoAPIError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": str(e)})


# ─────────────────────────────────────────────
# API — LBB Claim Validation
# ─────────────────────────────────────────────

@app.route("/api/validation/lbb/parse", methods=["POST"])
def api_lbb_parse():
    """
    Quick parse of uploaded LBB file — returns row preview + warnings.
    No NetEco API calls made here; used to give user a row-count preview.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"})
    rows, warnings = lbb_val.parse_lbb_upload(request.files["file"])
    return jsonify({"rows": rows, "warnings": warnings, "count": len(rows)})


@app.route("/api/validation/lbb", methods=["POST"])
def api_lbb_validate():
    """
    LBB validation: parse upload → resolve site DNs → run incident check
    (Quick mode) or incident + history (Full mode) per site.
    Accepts multipart/form-data: file, mode (quick|full), hist_days (int, default 30).
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"})

    rows, warnings = lbb_val.parse_lbb_upload(request.files["file"])
    if not rows:
        msg = warnings[0] if warnings else "No valid rows found in the uploaded file."
        return jsonify({"error": msg})

    mode      = request.form.get("mode", "quick").strip().lower()
    if mode not in ("quick", "full"):
        mode = "quick"
    try:
        hist_days = int(request.form.get("hist_days", 30))
        hist_days = max(7, min(hist_days, 90))   # clamp 7–90
    except (ValueError, TypeError):
        hist_days = 30

    client, err = fresh_login_client()
    if err:
        return err

    try:
        result = lbb_val.run_lbb_validation(rows, client, ne_tree,
                                            mode=mode, hist_days=hist_days)
        refresh_session(client)
        result["parse_warnings"] = warnings
        log.info("LBB Validation (%s): %d site(s) processed", mode, result.get("count", 0))

        # ── Save per-user LBB run to history ──────────────────────
        try:
            results_list = result.get("results", [])
            pass_c = sum(1 for r in results_list
                         if str(r.get("verdict", "")).lower().startswith("pass"))
            fail_c = sum(1 for r in results_list
                         if str(r.get("verdict", "")).lower().startswith("fail"))
            inc_c  = len(results_list) - pass_c - fail_c
            udb.add_lbb_run(session["user_id"], mode,
                            len(results_list), pass_c, fail_c, inc_c)
        except Exception as _se:
            log.warning("Could not save LBB run to user history: %s", _se)

        return jsonify(result)
    except NetEcoAPIError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        log.exception("LBB Validation unhandled error")
        return jsonify({"error": str(e)})


@app.route("/api/validation/lbb/export", methods=["POST"])
def api_lbb_export():
    """
    Export LBB validation results to a multi-sheet Excel file.
    Accepts the full validation JSON from the frontend.
    """
    data = request.json
    if not data or not data.get("results"):
        return jsonify({"error": "No validation results to export"})

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    inc_rows, h30_rows, h90_rows = lbb_val.flatten_for_excel(data)

    buf = io.BytesIO()
    wb  = Workbook()

    hdr_fill = PatternFill("solid", fgColor="1E3A5F")
    hdr_font = Font(color="FFFFFF", bold=True)

    def _write_sheet(ws, rows):
        if not rows:
            ws.append(["No data"])
            return
        cols = list(rows[0].keys())
        ws.append(cols)
        for cell in ws[1]:
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center")
        for r in rows:
            ws.append([r.get(c, "") for c in cols])
        for col in ws.columns:
            ml = max((len(str(c.value or "")) for c in col), default=10) + 2
            ws.column_dimensions[col[0].column_letter].width = min(ml, 50)

    ws1 = wb.active
    ws1.title = "Incident Validation"
    _write_sheet(ws1, inc_rows)

    ws2 = wb.create_sheet("Historical 30d Top3")
    _write_sheet(ws2, h30_rows)

    ws3 = wb.create_sheet("Historical 90d Top3")
    _write_sheet(ws3, h90_rows)

    wb.save(buf)
    buf.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"LBB_Validation_{timestamp}.xlsx"
    save_path = os.path.join(EXPORTS_DIR, filename)
    with open(save_path, "wb") as f:
        f.write(buf.getvalue())
    log.info("LBB Export saved: %s", save_path)

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────
# API — Dashboard
# ─────────────────────────────────────────────

@app.route("/api/dashboard/status")
def api_dashboard_status():
    """
    Returns combined dashboard status in one call:
      freshness  — NE data load timestamp and site count
      lbb        — last LBB validation run for THIS user (from users.db)
    """
    freshness = ne_tree.get_freshness()
    lbb_summary = {}
    uid = session.get("user_id")
    if uid:
        last = udb.get_last_lbb_run(uid)
        if last:
            lbb_summary = {
                "run_at":     last.get("run_at", ""),
                "mode":       last.get("mode", ""),
                "total":      last.get("total", 0),
                "pass_count": last.get("pass_count", 0),
                "fail_count": last.get("fail_count", 0),
                "inc_count":  last.get("inc_count", 0),
            }
    return jsonify({"freshness": freshness, "lbb": lbb_summary})


@app.route("/api/site/hardware")
def api_site_hardware():
    """
    Return hardware topology for one site from the local NE DB (no API call).
    Query: site_dn=...
    """
    site_dn = request.args.get("site_dn", "").strip()
    if not site_dn:
        return jsonify({"error": "site_dn required"})
    if not ne_tree.has_data():
        return jsonify({"error": "NE data not loaded"})
    return jsonify(ne_tree.get_site_hardware(site_dn))


@app.route("/api/site/hardware/config", methods=["POST"])
def api_site_hardware_config():
    """
    Fetch config signals for Battery Groups and Rectifier Groups of a site.
    Payload:
      site_dn             — for logging
      battery_group_dns   — list of Battery Group DNs
      rectifier_group_dns — list of Rectifier Group DNs
      vision_dns          — list of Vision Lithium Battery DNs (optional)

    Returns:
      battery_config   — {dn: {20037: total_ah, 20035: string_ah}}
      rectifier_config — {dn: {20002: model_str, 20007: capacity_a}}
      vision_config    — {dn: {538976260: rated_ah}}
    """
    data = request.json or {}
    batt_dns        = data.get("battery_group_dns", [])
    rect_dns        = data.get("rectifier_group_dns", [])
    vis_dns         = data.get("vision_dns", [])
    rect_module_dns = data.get("rectifier_dns", [])    # individual Rectifier NEs → signal 10014 (R48xxx module)
    site_unit_dns   = data.get("site_unit_dns", [])    # Site Unit NEs → signal 2101653 (ETP48xxx core model)

    if not batt_dns and not rect_dns and not vis_dns and not rect_module_dns and not site_unit_dns:
        return jsonify({"error": "No DNs provided"})

    client, err = fresh_login_client()
    if err:
        return err

    def _parse_config(raw_items: list) -> dict:
        out = {}
        for item in raw_items:
            dn  = item.get("dn", "")
            sid = item.get("signalId")
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                continue
            val = item.get("signalValue") or item.get("value") or ""
            out.setdefault(dn, {})[sid] = val
        return out

    try:
        result: dict = {}

        if batt_dns:
            raw = client.get_config_signals(batt_dns, [20037, 20035])
            result["battery_config"] = _parse_config(raw)

            # Total battery capacity — sampling signal 10013 on Battery Group NEs (type 32772)
            raw_bg = client.get_sampling(batt_dns, [10013])
            result["battery_group_sampling"] = _parse_config(raw_bg)

        if rect_dns:
            raw = client.get_config_signals(rect_dns, [20002, 20007])
            result["rectifier_config"] = _parse_config(raw)

        if vis_dns:
            raw = client.get_config_signals(vis_dns, [538976260])
            result["vision_config"] = _parse_config(raw)

        # Rectifier module model — sampling signal 10014 ("Model") on individual Rectifier NEs
        # Returns R48xxxxx strings (individual module model)
        if rect_module_dns:
            raw_m = client.get_sampling(rect_module_dns, [10014])
            result["rectifier_module_sampling"] = _parse_config(raw_m)

        # Core model — sampling signal 2101653 ("System Type").
        # ETP48xxx systems: reported on Site Unit NEs.
        # TP48300B / other cabinet types: reported on the Rectifier Group NE directly.
        # Query both and merge so the JS lookup (by site_unit_dn OR rg.dn) always finds the value.
        su_samp: dict = {}
        if site_unit_dns:
            raw_su = client.get_sampling(site_unit_dns, [2101653])
            su_samp.update(_parse_config(raw_su))
        if rect_dns:
            raw_rg = client.get_sampling(rect_dns, [2101653])
            # merge — don't overwrite existing Site Unit entries
            for dn, sigs in _parse_config(raw_rg).items():
                if dn not in su_samp:
                    su_samp[dn] = sigs
        result["site_unit_sampling"] = su_samp

        refresh_session(client)
        return jsonify(result)

    except NetEcoAPIError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        log.exception("hardware/config error")
        return jsonify({"error": str(e)})


@app.route("/api/site/dc-load")
def api_site_dc_load():
    """
    SSE stream: fetch 30-day Branch Group DC load statistics for one site.
    Query: site_dn=..., days=30

    Event types emitted:
      {progress: 0–100, chunk: N, total: M}   — progress tick per 23h chunk
      {done: true, tenant_stats, total_avg_w, total_peak_w, readings, branch_groups}
      {error: "..."}
    """
    site_dn = request.args.get("site_dn", "").strip()
    try:
        days = max(1, min(int(request.args.get("days", 30)), 90))
    except (ValueError, TypeError):
        days = 30

    if not site_dn:
        return jsonify({"error": "site_dn required"}), 400

    # Login before entering the generator (needs request context)
    client, err = fresh_login_client()
    if err:
        return err

    def _generate():
        try:
            import datetime as _dt

            # ── Step 1: Select NEs based on rectifier type ────────────
            #
            # A site can have multiple independent rectifier systems of different types:
            #   - Intelligent (SCC800): Load-iCBD NEs, per-tenant, signal 30003
            #   - Non-intelligent (DCOD): DC Output Distribution NEs, signal 30003
            #
            # Both types use signal 30003 → combine into one query so mixed sites
            # (e.g. one intelligent + one non-intelligent system) show all tenants.
            #
            # Fallback only if neither iCBD nor DCOD found → Branch Group, signal 30002.

            all_types = ["DC Output Distribution", "Load-iCBD", "LOAD-iCBD", "Branch Group"]
            ne_map_dc = ne_tree.get_dns_by_type_under_sites([site_dn], all_types)

            dcod_nes = ne_map_dc.get("DC Output Distribution", [])
            icbd_nes = ne_map_dc.get("Load-iCBD", []) + ne_map_dc.get("LOAD-iCBD", [])
            bg_nes   = ne_map_dc.get("Branch Group", [])

            combined_nes = icbd_nes + dcod_nes   # both use signal 30003

            if combined_nes:
                load_nes  = combined_nes
                signal_id = 30003
                parts = []
                if icbd_nes: parts.append(f"iCBD×{len(icbd_nes)}")
                if dcod_nes: parts.append(f"DCOD×{len(dcod_nes)}")
                source_desc = " + ".join(parts)
                if   icbd_nes and dcod_nes: site_type = "mixed"
                elif icbd_nes:              site_type = "intelligent"
                else:                       site_type = "standard"
            elif bg_nes:
                load_nes    = bg_nes
                signal_id   = 30002
                source_desc = f"Branch Group fallback ({len(bg_nes)} NEs)"
                site_type   = "branch"
            else:
                yield f"data: {json.dumps({'error': 'No load NEs found. Checked: Load-iCBD, DC Output Distribution, Branch Group. Ensure NE tree is loaded.'})}\n\n"
                return

            load_dns = [ne["dn"] for ne in load_nes]
            ctx = ne_tree.get_ne_context_bulk(load_dns)
            log.info("dc-load: site=%s  type=%s  source=%s  nes=%d",
                     site_dn, site_type, source_desc, len(load_dns))

            # ── Step 2: Fetch 30-day statistic in 23h chunks ──────────
            # Same chunking pattern as analysis.py battery fetch.
            # Signal 30003 on load NE types is a statistic signal.
            now_ms   = int(time.time() * 1000)
            start_ms = now_ms - days * 24 * 3600 * 1000
            CHUNK_MS = 23 * 3600 * 1000   # stay under 24h API limit

            chunks: list = []
            t = start_ms
            while t < now_ms:
                chunks.append((t, min(t + CHUNK_MS, now_ms)))
                t += CHUNK_MS

            total_chunks = len(chunks)
            chunk_done   = 0
            all_readings: list = []   # [(ts_ms, dn, value_kw)]

            for cs, ce in chunks:
                try:
                    raw = client.get_statistic(load_dns, [signal_id], cs, ce)
                    for item in raw:
                        ts_raw = (item.get("statisticTime") or
                                  item.get("collectTime") or
                                  item.get("endTime"))
                        dn  = item.get("dn", "")
                        val = (item.get("statisticValue") if item.get("statisticValue") is not None
                               else item.get("signalValue") if item.get("signalValue") is not None
                               else item.get("value"))
                        if dn and val is not None:
                            try:
                                all_readings.append((int(ts_raw or 0), dn, float(val)))
                            except (TypeError, ValueError):
                                pass
                except Exception as _ce:
                    log.warning("dc-load chunk error: %s", _ce)

                chunk_done += 1
                pct = int(chunk_done / total_chunks * 100)
                yield f"data: {json.dumps({'progress': pct, 'chunk': chunk_done, 'total': total_chunks})}\n\n"

            log.info("dc-load: total raw readings=%d across %d NEs", len(all_readings), len(load_dns))

            # ── Step 3: Compute avg + peak per tenant ────────────────
            # Key rule: each physical NE is an independent load — its average must
            # be computed from its own readings only, then summed across NEs in the
            # same tenant/system bucket.  Pooling all readings from multiple NEs
            # into one list and averaging them dilutes minority NEs (e.g. a cooling
            # NE drawing 0.21 kW gets hidden when mixed with 8 idle NEs).
            #
            # Both tenant_stats AND system_totals therefore use the same pattern:
            #   tenant/system total = Σ (per-NE average)   not  mean(all readings)
            #
            # Tenant normalisation:
            # get_ne_context_bulk sets "tenant_name" to the canonical MNO label
            # ("Robi", "GP", "BL", "TBL") via _operator_name_from_ne(), or ""
            # when the NE belongs to an auxiliary/non-MNO branch (cooling, RMS…).
            # Anything without a recognised MNO name → "Others".
            tenant_ne_vals: dict = {}   # (tname, dn) → [val_kw, ...]  — one entry per NE per tenant
            tenant_system:  dict = {}   # tenant_name → system prefix (e.g. "CMSDR34", "CTG_R2106")
            ne_readings_count: dict = {}  # dn → reading count (for debug)
            # Per-system totals: Σ per-NE averages across ALL NEs (tenants + Others/auxiliary)
            system_ne_vals: dict = {}   # (system_prefix, dn) → [val_kw, ...]

            for _ts, dn, val_kw in all_readings:
                ne_ctx  = ctx.get(dn, {})
                tname   = (ne_ctx.get("tenant_name") or "").strip()
                system  = (ne_ctx.get("tenant") or "").strip()   # system/site prefix
                if not tname:
                    tname = "Others"
                # Track per-(tenant, NE) so multi-NE tenants add up correctly
                tenant_ne_vals.setdefault((tname, dn), []).append(val_kw)
                ne_readings_count[dn] = ne_readings_count.get(dn, 0) + 1
                # Map named tenant → system prefix
                if system and tname != "Others":
                    tenant_system[tname] = system
                # Track per-(system, NE) for system totals — all NEs including Others
                if system:
                    system_ne_vals.setdefault((system, dn), []).append(val_kw)

            # Aggregate tenant stats: sum of per-NE averages per tenant bucket
            # This ensures "Others" (cooling, RMS…) is the true sum, not a diluted mean
            tenant_stats: dict = {}
            for (tname, dn), vals in tenant_ne_vals.items():
                if vals:
                    ne_avg  = sum(vals) / len(vals)
                    ne_peak = max(vals)
                    if tname not in tenant_stats:
                        tenant_stats[tname] = {"avg_kw": 0.0, "peak_kw": 0.0, "count": 0}
                    tenant_stats[tname]["avg_kw"]  += ne_avg
                    tenant_stats[tname]["peak_kw"] += ne_peak   # conservative sum of peaks
                    tenant_stats[tname]["count"]   += len(vals)
            for tname in tenant_stats:
                tenant_stats[tname]["avg_kw"]  = round(tenant_stats[tname]["avg_kw"],  2)
                tenant_stats[tname]["peak_kw"] = round(tenant_stats[tname]["peak_kw"], 2)

            # Aggregate system totals: same Σ per-NE pattern, includes ALL NEs
            system_totals: dict = {}
            for (system, dn), vals in system_ne_vals.items():
                if vals:
                    ne_avg  = sum(vals) / len(vals)
                    ne_peak = max(vals)
                    if system not in system_totals:
                        system_totals[system] = {"avg_kw": 0.0, "peak_kw": 0.0}
                    system_totals[system]["avg_kw"]  += ne_avg
                    system_totals[system]["peak_kw"] += ne_peak
            for sys in system_totals:
                system_totals[sys]["avg_kw"]  = round(system_totals[sys]["avg_kw"],  2)
                system_totals[sys]["peak_kw"] = round(system_totals[sys]["peak_kw"], 2)

            # Per-system tenant breakdown: system → {tenant → {avg_kw, peak_kw}}
            # Correctly separates Others-in-CMSDR34 from Others-in-CTG_R2106
            per_system: dict = {}
            for (tname, dn), vals in tenant_ne_vals.items():
                ne_ctx = ctx.get(dn, {})
                system = (ne_ctx.get("tenant") or "").strip()
                if not system or not vals:
                    continue
                ne_avg  = sum(vals) / len(vals)
                ne_peak = max(vals)
                per_system.setdefault(system, {})
                per_system[system].setdefault(tname, {"avg_kw": 0.0, "peak_kw": 0.0, "count": 0})
                per_system[system][tname]["avg_kw"]  += ne_avg
                per_system[system][tname]["peak_kw"] += ne_peak
                per_system[system][tname]["count"]   += len(vals)
            for sys in per_system:
                for t in per_system[sys]:
                    per_system[sys][t]["avg_kw"]  = round(per_system[sys][t]["avg_kw"],  2)
                    per_system[sys][t]["peak_kw"] = round(per_system[sys][t]["peak_kw"], 2)

            # Site totals: sum across all tenant buckets (incl. Others) — now matches system_totals
            total_avg_kw  = round(sum(s["avg_kw"]  for s in tenant_stats.values()), 2) if tenant_stats else 0
            total_peak_kw = round(sum(s["peak_kw"] for s in tenant_stats.values()), 2) if tenant_stats else 0

            query_range = {
                "start_utc": datetime.fromtimestamp(start_ms / 1000, tz=_BDT).strftime("%Y-%m-%d %H:%M BDT"),
                "end_utc":   datetime.fromtimestamp(now_ms   / 1000, tz=_BDT).strftime("%Y-%m-%d %H:%M BDT"),
                "signal":    signal_id,
            }

            # Debug list: each NE found + how many readings it returned
            ne_debug = [
                {
                    "dn":       ne["dn"],
                    "name":     ne.get("name", ""),
                    "type":     ne.get("type_name", ""),
                    "readings": ne_readings_count.get(ne["dn"], 0),
                    "tenant":   (ctx.get(ne["dn"]) or {}).get("tenant_name", ""),
                    "system":   (ctx.get(ne["dn"]) or {}).get("tenant", ""),
                }
                for ne in load_nes
            ]

            yield f"data: {json.dumps({'done': True, 'tenant_stats': tenant_stats, 'tenant_system': tenant_system, 'system_totals': system_totals, 'per_system': per_system, 'total_avg_kw': total_avg_kw, 'total_peak_kw': total_peak_kw, 'readings': len(all_readings), 'load_nes': len(load_dns), 'source': source_desc, 'site_type': site_type, 'days': days, 'query_range': query_range, 'ne_debug': ne_debug})}\n\n"

        except Exception as _e:
            log.exception("dc-load SSE unhandled error")
            yield f"data: {json.dumps({'error': str(_e)})}\n\n"

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/validation/lbb-template")
def api_lbb_template():
    """Serve the blank LBB Validation Excel template."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "LBB Validation"

    headers = [
        "EASI Site ID",
        "Customer Site Reference",
        "MNO Name for Fault/Service Degradation Claim",
        "Connected Tenant with the Rectifier",
        "Mains Fail Time",
        "Rectifier Design Load (kW)",
        "Considered BB Modality (hrs)",
    ]
    examples = [
        ["e.coBD000090CM", "CMCDG27", "Robi Axiata Limited", "Robi",
         "2025-09-09 16:25", 3.59, 6],
        ["e.coBD000257DH", "DHDKK05", "Robi Axiata Limited", "Robi+BL",
         "2026-04-02 10:30", 6.00, 6],
    ]

    thin  = Side(style="thin", color="CCCCCC")
    bdr   = Border(left=thin, right=thin, top=thin, bottom=thin)
    hfill = PatternFill("solid", fgColor="1E3A5F")
    hfont = Font(color="FFFFFF", bold=True, size=11)
    note_fill = PatternFill("solid", fgColor="FFF9C4")

    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.font  = hfont
        cell.fill  = hfill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = bdr

    for row in examples:
        ws.append(row)
        for ci in range(1, len(headers) + 1):
            ws.cell(ws.max_row, ci).fill   = note_fill
            ws.cell(ws.max_row, ci).border = bdr

    col_widths = [22, 26, 44, 32, 20, 26, 28]
    for i, w in enumerate(col_widths, 1):
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 36

    # Instructions sheet
    wi = wb.create_sheet("Instructions")
    wi.column_dimensions["A"].width = 32
    wi.column_dimensions["B"].width = 60
    notes = [
        ("Column", "Description / Notes"),
        ("EASI Site ID",
         "Full site identifier from NetEco (e.g. e.coBD000090CM). "
         "Matched against the local NE tree — must be loaded first via Setup."),
        ("Customer Site Reference",
         "Short site code (e.g. CMCDG27). For reference only — not used for lookup."),
        ("MNO Name for Fault Claim",
         "The operator claiming the battery failure "
         "(e.g. Robi Axiata Limited, Grameenphone, Banglalink, Teletalk). "
         "Determines which tenant system to analyse."),
        ("Connected Tenant with the Rectifier",
         "All tenants sharing the rectifier/battery bank "
         "(e.g. Robi, Robi+BL). For reference only."),
        ("Mains Fail Time",
         "Date and time of the reported outage event. "
         "Format: YYYY-MM-DD HH:MM (BDT). "
         "Rows WITHOUT this value are skipped — those are capacity upgrades, not battery faults."),
        ("Rectifier Design Load (kW)",
         "Total design load of the rectifier system. "
         "Used to flag if avg discharge power exceeded design capacity."),
        ("Considered BB Modality (hrs)",
         "Contractual battery backup SLA in hours (e.g. 6). "
         "Actual backup time is compared against this value."),
    ]
    hf2 = Font(bold=True)
    for ri, (col, desc) in enumerate(notes, 1):
        c1 = wi.cell(ri, 1, col)
        c2 = wi.cell(ri, 2, desc)
        if ri == 1:
            c1.font = hf2; c2.font = hf2
        c2.alignment = Alignment(wrap_text=True)
        wi.row_dimensions[ri].height = 42 if ri > 1 else 18

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="LBB_Validation_Template.xlsx",
    )


# ─────────────────────────────────────────────
# Bulk Extractor
# ─────────────────────────────────────────────
@app.route("/bulk")
def bulk_extract_page():
    return render_template("bulk_extract.html")


@app.route("/api/template/bulk-extract")
def api_bulk_template():
    """Serve the pre-filled Excel template for bulk extraction."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    # ── Sites sheet ──
    ws = wb.active
    ws.title = "Sites"
    headers = ["Site_ID", "Tenant_Code", "Date"]
    examples = [
        ["e.co12345", "KCSDR04", "2026-05-01"],
        ["e.co67890", "DKBDR01", "2026-05-01"],
    ]
    hdr_fill = PatternFill("solid", fgColor="1E3A5F")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.font = hdr_font; cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border = border
    for row in examples:
        ws.append(row)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 14

    # ── Instructions sheet ──
    wi = wb.create_sheet("Instructions")
    wi.column_dimensions["A"].width = 22
    wi.column_dimensions["B"].width = 55
    notes = [
        ("Column",        "Description"),
        ("Site_ID",       "Site name or code as stored in NetEco (e.g. e.co12345). "
                          "A partial match is used, so 'e.co12345' will match "
                          "'e.co12345_SomeSuffix'."),
        ("Tenant_Code",   "Operator/tenant suffix in the controller name, e.g. KCSDR04, "
                          "DKBDR01, GP01, ROBI01. Used to filter NEs to one tenant's "
                          "equipment at a shared site. Leave blank to get all tenants."),
        ("Date",          "The day for which data is needed (YYYY-MM-DD). "
                          "Statistical and historical alarm data uses midnight–23:59 on this date. "
                          "Sampling/Config use current values (date is for reference only)."),
    ]
    for r, (col, desc) in enumerate(notes, 1):
        c1 = wi.cell(r, 1, col); c2 = wi.cell(r, 2, desc)
        if r == 1:
            c1.font = Font(bold=True); c2.font = Font(bold=True)
        c2.alignment = Alignment(wrap_text=True)
    wi.row_dimensions[1].height = 18
    for i in range(2, len(notes) + 1):
        wi.row_dimensions[i].height = 42

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return send_file(buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="BulkExtract_Template.xlsx")


@app.route("/api/bulk/parse", methods=["POST"])
def api_bulk_parse():
    """
    Parse an uploaded Excel with Site_ID / Tenant_Code / Date columns.
    Resolves each site against the local NE tree DB.
    Returns a preview list for the UI.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"})
    f = request.files["file"]
    try:
        df = pd.read_excel(f)
    except Exception as e:
        return jsonify({"error": f"Cannot read Excel: {e}"})

    # Normalise column names — case-insensitive, strip spaces/underscores
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    col_map = {}
    for expected in ["site_id", "tenant_code", "date"]:
        for c in df.columns:
            if expected.replace("_", "") in c.replace("_", ""):
                col_map[expected] = c; break

    if "site_id" not in col_map:
        return jsonify({"error": "Excel must have a 'Site_ID' column"})

    rows = []
    for _, row in df.iterrows():
        site_code  = str(row.get(col_map["site_id"], "")).strip()
        tenant     = str(row.get(col_map.get("tenant_code", ""), "")).strip() if "tenant_code" in col_map else ""
        date_raw   = row.get(col_map.get("date", ""), "") if "date" in col_map else ""
        date_str   = str(date_raw)[:10] if date_raw and str(date_raw) != "nan" else ""
        tenant     = "" if tenant == "nan" else tenant

        if not site_code or site_code == "nan":
            continue

        site = ne_tree.find_site_by_code(site_code)
        resolved = bool(site)
        rows.append({
            "site_code":  site_code,
            "tenant":     tenant,
            "date":       date_str,
            "site_dn":    site.get("dn", ""),
            "site_name":  site.get("name", ""),
            "site_status": site.get("status", ""),
            "resolved":   resolved,
            "message":    "" if resolved else f"'{site_code}' not found in local DB",
        })

    total     = len(rows)
    resolved  = sum(1 for r in rows if r["resolved"])
    log.info("bulk/parse: %d rows, %d resolved", total, resolved)
    return jsonify({"rows": rows, "total": total, "resolved": resolved})


@app.route("/api/bulk/mo-available", methods=["POST"])
def api_bulk_mo_available():
    """
    Given a list of site DNs, return which MO types have NEs in the local DB.
    Returns {mo_type: count} for non-zero types — used to populate the signal tree.
    """
    data     = request.json
    site_dns = data.get("site_dns", [])
    if not site_dns:
        return jsonify({})
    mo_types = [
        "Battery Group", "Battery String",
        "Rectifier Group", "Rectifier",
        "Digital Power", "DC Output Distribution",
        "AC Input Distribution", "Mains",
        "Controller", "Site", "Genset",
        "Load-iCBD", "LOAD-iCBD", "General-iCBD",
    ]
    result = ne_tree.get_dns_by_type_under_sites(site_dns, mo_types)
    return jsonify({k: len(v) for k, v in result.items() if v})


@app.route("/api/extract/multi", methods=["POST"])
def api_extract_multi():
    """
    Multi-job extraction for the single Data Extractor page.
    Accepts a list of site DNs + a jobs list — each job specifies MO type,
    signal type, optional signal IDs, and optional date range.
    Alarm jobs have no mo_type.

    Payload:
      site_dns  — list of site DNs (from site selector)
      jobs      — [{mo_type?, signal_type, signal_ids:[], start_ms?, end_ms?}]

    Returns:
      {count, sheets, saved_path, summary:[{label,count,message?}],
       preview:{columns,rows}}   ← first 200 rows for live table display
    """
    data     = request.json
    site_dns = data.get("site_dns", [])
    jobs     = data.get("jobs", [])

    if not site_dns:
        return jsonify({"error": "No sites selected"})
    if not jobs:
        return jsonify({"error": "No signal selections"})

    client, err = fresh_login_client()
    if err:
        return err

    from openpyxl.styles import Font, PatternFill, Alignment

    sheets:  dict = {}    # sheet_key → [row_dicts]
    preview: list = []    # first 200 combined rows
    summary: list = []

    for job in jobs:
        mo_type    = job.get("mo_type")        # None for alarm jobs
        sig_type   = job["signal_type"]
        signal_ids = [int(s) for s in job.get("signal_ids", []) if str(s).strip()]
        start_ms   = job.get("start_ms")
        end_ms     = job.get("end_ms")
        is_alarm   = sig_type.startswith("alarm")
        sheet_key  = f"{'Alarms' if is_alarm else mo_type[:18]}_{sig_type}"[:31]
        label      = f"{'Alarms' if is_alarm else mo_type} / {sig_type}"

        try:
            # ── Alarm jobs ──────────────────────────────────
            if is_alarm:
                if sig_type == "alarm_current":
                    raw = client.get_active_alarms(dns=site_dns)
                else:
                    if not start_ms or not end_ms:
                        summary.append({"label": label, "count": 0,
                                        "message": "Skipped — no date range"})
                        continue
                    raw = client.get_historical_alarms(
                        int(start_ms), int(end_ms), dns=site_dns)

                if sheet_key not in sheets:
                    sheets[sheet_key] = []
                for item in raw:
                    row = {
                        "Alarm Type":   "Active" if sig_type == "alarm_current" else "Historical",
                        "DN":           item.get("dn", ""),
                        "Alarm Name":   item.get("alarmName", ""),
                        "Severity":     item.get("severityStr", item.get("severity", "")),
                        "Alarm Time":   _fmt_ts(item.get("alarmTime") or item.get("occurTime")),
                        "Clear Time":   _fmt_ts(item.get("clearTime", "")),
                        "Description":  item.get("alarmDesc", ""),
                    }
                    sheets[sheet_key].append(row)
                    if len(preview) < 200:
                        preview.append(row)
                cnt = len(sheets[sheet_key])
                summary.append({"label": label, "count": cnt})
                log.info("extract/multi: %s → %d rows", label, cnt)
                continue

            # ── MO-based signal jobs ─────────────────────────
            ne_map = ne_tree.get_dns_by_type_under_sites(site_dns, [mo_type])
            nes    = ne_map.get(mo_type, [])
            if not nes:
                summary.append({"label": label, "count": 0,
                                 "message": f"No '{mo_type}' NEs under selected sites"})
                log.warning("extract/multi: no NEs for %s", label)
                continue

            dns        = [ne["dn"] for ne in nes]
            dn_to_name = {ne["dn"]: ne["name"] for ne in nes}
            dn_context = ne_tree.get_ne_context_bulk(dns)  # → {dn: {site_name, tenant}}

            # Build a type_id / type_ver_id lookup for signal name enrichment
            ne_type_info = {}   # dn → {type_id, type_ver_id}
            with ne_tree.get_conn() as _c:
                for ne in nes:
                    r = _c.execute(
                        "SELECT type_id, type_ver_id FROM ne_nodes WHERE dn=?", (ne["dn"],)
                    ).fetchone()
                    if r:
                        ne_type_info[ne["dn"]] = {
                            "type_id":     r["type_id"],
                            "type_ver_id": r["type_ver_id"] or 0,
                        }

            if sig_type == "sampling":
                raw = client.get_sampling(dns, signal_ids or None)
            elif sig_type == "config":
                raw = client.get_config_signals(dns, signal_ids or None)
            elif sig_type == "statistic":
                if not start_ms or not end_ms:
                    summary.append({"label": label, "count": 0,
                                     "message": "Skipped — no date range"})
                    continue
                # signalIds is mandatory per API spec — resolve from dict when not specified
                stat_signal_ids = signal_ids or []
                if not stat_signal_ids and dns:
                    dict_sigs = ne_tree.get_signals_for_ne(dns[0], "statistic")
                    stat_signal_ids = [s["id"] for s in dict_sigs]
                if not stat_signal_ids:
                    summary.append({"label": label, "count": 0,
                                    "message": "No statistical signals in dictionary for this MO type"})
                    continue
                raw = client.get_statistic(dns, stat_signal_ids, int(start_ms), int(end_ms))
            else:
                continue

            # Pre-fetch signal labels from dictionary for efficient enrichment
            # Use the first NE's type info as representative (same MO type = same signals)
            first_ne_dn = dns[0] if dns else None
            first_type  = ne_type_info.get(first_ne_dn, {})
            seen_sids   = set(int(item.get("signalId", 0)) for item in raw if item.get("signalId"))
            sig_labels  = ne_tree.get_signal_labels_bulk(
                first_type.get("type_id", 0),
                first_type.get("type_ver_id", 0),
                list(seen_sids),
            ) if first_type else {}

            if sheet_key not in sheets:
                sheets[sheet_key] = []
            for item in raw:
                dn     = item.get("dn", "")
                sid    = item.get("signalId", "")
                sid_i  = int(sid) if str(sid).strip().lstrip("-").isdigit() else 0
                label_info = sig_labels.get(sid_i, {})

                # Signal name: prefer dict (has proper names), fall back to API value
                sig_name = label_info.get("name") or item.get("signalName", "")
                # Unit: prefer dict, fall back to API
                unit     = label_info.get("unit") or item.get("unit", "")
                # Value field differs by signal type
                value    = (item.get("signalValue") or item.get("value") or
                            item.get("statisticValue") or "")
                # Timestamp field also differs
                ts       = _fmt_ts(
                    item.get("signalResultTime") or item.get("collectTime") or
                    item.get("statisticTime")    or item.get("endTime")
                )
                ctx = dn_context.get(dn, {})
                row = {
                    "Timestamp":   ts,
                    "Site Code":   ctx.get("site_name", ""),
                    "Tenant":      ctx.get("tenant", ""),
                    "Tenant Name": ctx.get("tenant_name", ""),
                    "NE Name":     dn_to_name.get(dn, dn),
                    "Signal Name": sig_name,
                    "Value":       value,
                    "Unit":        unit,
                }
                sheets[sheet_key].append(row)
                if len(preview) < 500:
                    preview.append(row)
            cnt = len(sheets[sheet_key])
            summary.append({"label": label, "count": cnt})
            log.info("extract/multi: %s → %d rows", label, cnt)

        except NetEcoAPIError as e:
            summary.append({"label": label, "count": 0, "message": str(e)})
            log.error("extract/multi API error [%s]: %s", label, e)
        except Exception as e:
            summary.append({"label": label, "count": 0, "message": str(e)})
            log.error("extract/multi error [%s]: %s", label, e)

    refresh_session(client)

    total = sum(len(v) for v in sheets.values())
    if not total:
        return jsonify({"error": "No data returned for any selection",
                        "count": 0, "summary": summary})

    # ── Multi-sheet Excel ───────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(EXPORTS_DIR, f"Extract_{timestamp}.xlsx")
    hdr_fill  = PatternFill("solid", fgColor="1E3A5F")
    hdr_font  = Font(color="FFFFFF", bold=True)

    with pd.ExcelWriter(save_path, engine="openpyxl") as writer:
        for sname, rows in sheets.items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            pd.DataFrame(rows, columns=cols).to_excel(writer, index=False, sheet_name=sname)
            ws = writer.sheets[sname]
            for cell in ws[1]:
                cell.font = hdr_font; cell.fill = hdr_fill
                cell.alignment = Alignment(horizontal="center")
            for col in ws.columns:
                ml = max((len(str(c.value or "")) for c in col), default=10) + 2
                ws.column_dimensions[col[0].column_letter].width = min(ml, 55)

    log.info("extract/multi: %d rows, %d sheets → %s", total, len(sheets), save_path)
    preview_cols = list(preview[0].keys()) if preview else []
    return jsonify({
        "count":      total,
        "sheets":     len(sheets),
        "saved_path": save_path,
        "summary":    summary,
        "preview":    {"columns": preview_cols, "rows": preview},
    })


@app.route("/api/bulk/extract", methods=["POST"])
def api_bulk_extract():
    """
    Bulk extraction.

    Payload:
      rows      — list from /api/bulk/parse (resolved sites with date)
      jobs      — list of {mo_type, signal_type, signal_ids:[]}
                  signal_ids=[] means extract ALL signals for that MO+type combo.

    Output: saves one Excel with one sheet per MO+signal_type job.
    Returns {count, saved_path}.
    """
    data  = request.json
    rows  = data.get("rows", [])
    jobs  = data.get("jobs", [])

    resolved_rows = [r for r in rows if r.get("resolved")]
    if not resolved_rows:
        return jsonify({"error": "No resolved sites to extract"})
    if not jobs:
        return jsonify({"error": "No signal selections made"})

    client, err = fresh_login_client()
    if err:
        return err

    # ── Pre-compute date-range per row ──
    def _day_range(date_str):
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start = int(d.timestamp() * 1000)
            end   = int((d.replace(hour=23, minute=59, second=59)).timestamp() * 1000)
            return start, end
        except Exception:
            return None, None

    # ── Results dict: sheet_key → [row_dicts] ──
    sheets: dict = {}
    total_rows = 0

    for job in jobs:
        mo_type    = job["mo_type"]
        sig_type   = job["signal_type"]
        signal_ids = [int(s) for s in job.get("signal_ids", []) if str(s).strip()]
        sheet_key  = f"{mo_type[:18]}_{sig_type}"[:31]   # Excel sheet name ≤31 chars

        for site_row in resolved_rows:
            site_dn   = site_row["site_dn"]
            tenant    = site_row.get("tenant", "")
            site_code = site_row["site_code"]
            date_str  = site_row.get("date", "")
            start_ms, end_ms = _day_range(date_str)

            # Resolve which DNs to query (tenant-filtered if specified)
            lookup_dn = ne_tree.get_controller_dn_for_tenant(site_dn, tenant)
            ne_map    = ne_tree.get_dns_by_type_under_sites([lookup_dn], [mo_type])
            nes       = ne_map.get(mo_type, [])
            if not nes:
                log.warning("bulk: no %s NEs at %s / tenant=%s", mo_type, site_code, tenant)
                continue

            dns        = [ne["dn"] for ne in nes]
            dn_to_name = {ne["dn"]: ne["name"] for ne in nes}

            try:
                if sig_type == "sampling":
                    raw = client.get_sampling(dns, signal_ids or None)
                elif sig_type == "config":
                    raw = client.get_config_signals(dns, signal_ids or None)
                elif sig_type == "statistic":
                    if not start_ms:
                        log.warning("bulk: statistic skipped for %s — no date", site_code)
                        continue
                    # signalIds mandatory — resolve from dict if not specified
                    stat_sids = signal_ids or []
                    if not stat_sids and dns:
                        dict_sigs = ne_tree.get_signals_for_ne(dns[0], "statistic")
                        stat_sids = [s["id"] for s in dict_sigs]
                    if not stat_sids:
                        log.warning("bulk: statistic skipped for %s — no signals in dict", site_code)
                        continue
                    raw = client.get_statistic(dns, stat_sids, start_ms, end_ms)
                elif sig_type == "alarm_current":
                    raw = client.get_active_alarms(dns=dns)
                elif sig_type == "alarm_history":
                    if not start_ms:
                        continue
                    raw = client.get_historical_alarms(start_ms, end_ms, dns=dns)
                else:
                    continue
            except NetEcoAPIError as e:
                log.error("bulk: API error %s/%s/%s: %s", site_code, mo_type, sig_type, e)
                continue
            except Exception as e:
                log.error("bulk: unexpected error %s/%s/%s: %s", site_code, mo_type, sig_type, e)
                continue

            if sheet_key not in sheets:
                sheets[sheet_key] = []

            # Enrich context (tenant, tenant name) and signal labels from local dict
            dn_context = ne_tree.get_ne_context_bulk(dns)
            sig_labels: dict = {}
            if dns and not sig_type.startswith("alarm"):
                _first_dn = dns[0]
                with ne_tree.get_conn() as _c:
                    _nr = _c.execute(
                        "SELECT type_id, type_ver_id FROM ne_nodes WHERE dn=?",
                        (_first_dn,)
                    ).fetchone()
                    if _nr:
                        _seen_sids = set(
                            int(item.get("signalId", 0))
                            for item in raw
                            if str(item.get("signalId", "")).strip().lstrip("-").isdigit()
                        )
                        sig_labels = ne_tree.get_signal_labels_bulk(
                            _nr["type_id"], _nr["type_ver_id"] or 0, list(_seen_sids)
                        )

            # Format rows — column order matches Data Extractor
            is_alarm = sig_type.startswith("alarm")
            for item in raw:
                dn  = item.get("dn", "")
                ctx = dn_context.get(dn, {})
                if is_alarm:
                    r = {
                        "Timestamp":    _fmt_ts(item.get("alarmTime") or item.get("occurTime")),
                        "Site Code":    ctx.get("site_name", "") or site_code,
                        "Tenant":       ctx.get("tenant", "") or tenant,
                        "Tenant Name":  ctx.get("tenant_name", ""),
                        "NE Name":      dn_to_name.get(dn, dn),
                        "Signal Name":  item.get("alarmName", ""),
                        "Value":        item.get("severityStr", item.get("severity", "")),
                        "Unit":         _fmt_ts(item.get("clearTime")) or "Active",
                    }
                else:
                    _sid   = item.get("signalId", "")
                    _sid_i = int(_sid) if str(_sid).strip().lstrip("-").isdigit() else 0
                    _lbl   = sig_labels.get(_sid_i, {})
                    r = {
                        "Timestamp":   _fmt_ts(
                            item.get("statisticTime")    or item.get("signalResultTime") or
                            item.get("collectTime")      or item.get("endTime")
                        ),
                        "Site Code":   ctx.get("site_name", "") or site_code,
                        "Tenant":      ctx.get("tenant", "") or tenant,
                        "Tenant Name": ctx.get("tenant_name", ""),
                        "NE Name":     dn_to_name.get(dn, dn),
                        "Signal Name": _lbl.get("name") or item.get("signalName", ""),
                        "Value":       (item.get("statisticValue") or item.get("signalValue") or
                                        item.get("value") or ""),
                        "Unit":        _lbl.get("unit") or item.get("unit", ""),
                    }
                sheets[sheet_key].append(r)
                total_rows += 1

    refresh_session(client)

    if not total_rows:
        return jsonify({"error": "No data returned for any site/signal combination", "count": 0})

    # ── Write multi-sheet Excel ──
    from openpyxl.styles import Font, PatternFill, Alignment
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename   = f"BulkExtract_{timestamp}.xlsx"
    save_path  = os.path.join(EXPORTS_DIR, filename)
    hdr_fill   = PatternFill("solid", fgColor="1E3A5F")
    hdr_font   = Font(color="FFFFFF", bold=True)

    with pd.ExcelWriter(save_path, engine="openpyxl") as writer:
        for sheet_name, sheet_rows in sheets.items():
            if not sheet_rows:
                continue
            cols = list(sheet_rows[0].keys())
            df   = pd.DataFrame(sheet_rows, columns=cols)
            df.to_excel(writer, index=False, sheet_name=sheet_name)
            ws = writer.sheets[sheet_name]
            for cell in ws[1]:
                cell.font = hdr_font; cell.fill = hdr_fill
                cell.alignment = Alignment(horizontal="center")
            for col in ws.columns:
                ml = max((len(str(c.value or "")) for c in col), default=10) + 2
                ws.column_dimensions[col[0].column_letter].width = min(ml, 55)

    from urllib.parse import quote as _url_quote
    log.info("bulk/extract: %d rows across %d sheets → %s", total_rows, len(sheets), save_path)
    return jsonify({
        "count":        total_rows,
        "sheets":       len(sheets),
        "saved_path":   save_path,
        "download_url": f"/api/download-export?path={_url_quote(save_path, safe='')}",
        "filename":     filename,
    })


# ─────────────────────────────────────────────
# API — Excel Export
# ─────────────────────────────────────────────
@app.route("/api/export-excel", methods=["POST"])
def api_export_excel():
    data = request.json
    columns = data.get("columns", [])
    rows    = data.get("rows", [])
    title   = data.get("title", "NetEco_Export")

    if not rows:
        return jsonify({"error": "No data to export"})

    df = pd.DataFrame(rows, columns=columns)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Data")
        ws = writer.sheets["Data"]
        # Auto-fit column widths
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col) + 2
            ws.column_dimensions[col[0].column_letter].width = min(max_len, 50)
    buf.seek(0)

    filename = f"{title}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _save_excel(rows: list, columns: list, title: str) -> str:
    """
    Save rows to an Excel file in EXPORTS_DIR.
    Returns the absolute file path, or "" if there is nothing to save.
    """
    if not rows:
        log.info("_save_excel: no rows — skipping file creation")
        return ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = "".join(c if c.isalnum() or c in "_-" else "_" for c in title)
    filename = f"{safe_title}_{timestamp}.xlsx"
    path = os.path.join(EXPORTS_DIR, filename)
    try:
        df = pd.DataFrame(rows, columns=columns if columns else None)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Data")
            ws = writer.sheets["Data"]
            for col in ws.columns:
                max_len = max((len(str(cell.value or "")) for cell in col), default=10) + 2
                ws.column_dimensions[col[0].column_letter].width = min(max_len, 60)
        return path
    except Exception as e:
        log.error("_save_excel failed: %s", e)
        return ""


def _tenant_matches(site_name: str, tenant: str) -> bool:
    """Check if site name contains the tenant code after '_'."""
    parts = site_name.split("_")
    if len(parts) >= 2:
        return tenant.upper() in parts[-1].upper()
    return False


_BDT = timezone(timedelta(hours=6))   # Bangladesh Standard Time = UTC+6

def _fmt_ts(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=_BDT).strftime("%Y-%m-%d %H:%M:%S BDT")
    except Exception:
        return str(ms)


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    print(f"\n{'='*50}")
    print(f"  NetEco Tool starting on http://0.0.0.0:{port}")
    print(f"  Open: http://localhost:{port}")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
