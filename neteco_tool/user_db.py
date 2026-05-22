"""
User management — authentication, per-user profile, site selections, run history.
All data stored in instance/users.db (separate from ne_cache.db).

Tables
------
  users                — accounts + hashed passwords
  user_site_selections — last site DNs used per tool per user
  user_run_history     — log of extract / report / analysis runs
  user_lbb_history     — log of LBB validation runs
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

from werkzeug.security import generate_password_hash as _gen_hash, check_password_hash

# Python 3.8 on macOS ships LibreSSL which lacks scrypt support.
# Force pbkdf2:sha256 so the app works on both macOS (dev) and EC2/Ubuntu (prod).
def generate_password_hash(password: str) -> str:
    return _gen_hash(password, method="pbkdf2:sha256")

_BDT = timezone(timedelta(hours=6))   # Bangladesh Standard Time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "users.db")

MAX_RUN_HISTORY = 50   # kept per user
MAX_LBB_HISTORY = 30   # kept per user


# ─────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ─────────────────────────────────────────────
# Schema init
# ─────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                last_login    TEXT
            );

            CREATE TABLE IF NOT EXISTS user_site_selections (
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tool       TEXT NOT NULL,
                site_dns   TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, tool)
            );

            CREATE TABLE IF NOT EXISTS user_run_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                run_type    TEXT NOT NULL,
                description TEXT,
                row_count   INTEGER DEFAULT 0,
                export_path TEXT DEFAULT '',
                run_at_ms   INTEGER NOT NULL,
                run_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_run_hist_user
                ON user_run_history(user_id, run_at_ms DESC);

            CREATE TABLE IF NOT EXISTS user_lbb_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode        TEXT NOT NULL,
                total       INTEGER DEFAULT 0,
                pass_count  INTEGER DEFAULT 0,
                fail_count  INTEGER DEFAULT 0,
                inc_count   INTEGER DEFAULT 0,
                run_at_ms   INTEGER NOT NULL,
                run_at      TEXT NOT NULL,
                export_path TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_lbb_hist_user
                ON user_lbb_history(user_id, run_at_ms DESC);
        """)
    _ensure_default_admin()


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _now_bdt() -> str:
    return datetime.now(_BDT).strftime("%Y-%m-%d %H:%M BDT")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_default_admin():
    """Create default admin/admin123 if no users exist yet."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            ("admin", generate_password_hash("admin123"), 1, _now_bdt()),
        )


# ─────────────────────────────────────────────
# User CRUD
# ─────────────────────────────────────────────

def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, is_admin, created_at, last_login FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    """Returns all fields including password_hash — only use internally."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, is_admin, created_at, last_login "
            "FROM users WHERE username=?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at, last_login FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, is_admin: bool = False) -> tuple[bool, str]:
    """Create a new user. Returns (success, message)."""
    username = username.strip()
    if not username:
        return False, "Username cannot be empty"
    if len(username) > 50:
        return False, "Username too long (max 50 chars)"
    if len(password) < 6:
        return False, "Password must be at least 6 characters"
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
                (username, generate_password_hash(password), 1 if is_admin else 0, _now_bdt()),
            )
        return True, f"User '{username}' created successfully"
    except sqlite3.IntegrityError:
        return False, f"Username '{username}' already exists"


def delete_user(user_id: int) -> tuple[bool, str]:
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return True, f"User '{row['username']}' deleted"


def change_password(user_id: int, new_password: str) -> tuple[bool, str]:
    if len(new_password) < 6:
        return False, "Password must be at least 6 characters"
    with get_conn() as conn:
        n = conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), user_id),
        ).rowcount
    return (True, "Password updated") if n else (False, "User not found")


def verify_login(username: str, password: str) -> dict | None:
    """
    Verify credentials. Returns a safe user dict (no password_hash) on success, else None.
    Also updates last_login timestamp.
    """
    row = get_user_by_username(username)
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (_now_bdt(), row["id"]))
    safe = {k: v for k, v in row.items() if k != "password_hash"}
    return safe


# ─────────────────────────────────────────────
# Per-user site selections
# ─────────────────────────────────────────────

def save_site_selection(user_id: int, tool: str, site_dns: list):
    """Upsert the last-used site list for a given tool."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_site_selections (user_id, tool, site_dns, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id, tool) DO UPDATE
               SET site_dns=excluded.site_dns, updated_at=excluded.updated_at""",
            (user_id, tool, json.dumps(site_dns), _now_bdt()),
        )


def get_site_selection(user_id: int, tool: str) -> list:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT site_dns FROM user_site_selections WHERE user_id=? AND tool=?",
            (user_id, tool),
        ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["site_dns"])
    except Exception:
        return []


def get_all_site_selections(user_id: int) -> dict:
    """Return {tool: [dns]} for all tools this user has selections for."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT tool, site_dns FROM user_site_selections WHERE user_id=?",
            (user_id,),
        ).fetchall()
    result = {}
    for r in rows:
        try:
            result[r["tool"]] = json.loads(r["site_dns"])
        except Exception:
            result[r["tool"]] = []
    return result


# ─────────────────────────────────────────────
# Per-user run history (extract / report / analysis)
# ─────────────────────────────────────────────

def add_run_history(user_id: int, run_type: str, description: str,
                    row_count: int = 0, export_path: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_run_history
               (user_id, run_type, description, row_count, export_path, run_at_ms, run_at)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, run_type, description, row_count, export_path or "",
             _now_ms(), _now_bdt()),
        )
        # Prune oldest beyond limit
        conn.execute(
            """DELETE FROM user_run_history WHERE user_id=? AND id NOT IN (
               SELECT id FROM user_run_history WHERE user_id=?
               ORDER BY run_at_ms DESC LIMIT ?)""",
            (user_id, user_id, MAX_RUN_HISTORY),
        )


def get_run_history(user_id: int, limit: int = 25) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, run_type, description, row_count, export_path, run_at
               FROM user_run_history WHERE user_id=?
               ORDER BY run_at_ms DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Per-user LBB validation history
# ─────────────────────────────────────────────

def add_lbb_run(user_id: int, mode: str, total: int,
                pass_count: int, fail_count: int, inc_count: int,
                export_path: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_lbb_history
               (user_id, mode, total, pass_count, fail_count, inc_count,
                run_at_ms, run_at, export_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, mode, total, pass_count, fail_count, inc_count,
             _now_ms(), _now_bdt(), export_path or ""),
        )
        conn.execute(
            """DELETE FROM user_lbb_history WHERE user_id=? AND id NOT IN (
               SELECT id FROM user_lbb_history WHERE user_id=?
               ORDER BY run_at_ms DESC LIMIT ?)""",
            (user_id, user_id, MAX_LBB_HISTORY),
        )


def get_lbb_history(user_id: int, limit: int = 10) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, mode, total, pass_count, fail_count, inc_count, run_at, export_path
               FROM user_lbb_history WHERE user_id=?
               ORDER BY run_at_ms DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_lbb_run(user_id: int) -> dict:
    """Return the most recent LBB run summary for this user, or {}."""
    rows = get_lbb_history(user_id, limit=1)
    return rows[0] if rows else {}
