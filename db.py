"""SQLite storage for OAuth state and per-workspace Campfire tokens.

The database lives at DB_PATH (on Render, a path on the persistent disk such as
/var/data/app.sqlite3). Nothing secret is ever written to the repository.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_state (
    state        TEXT PRIMARY KEY,
    customer_id  TEXT,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connection (
    customer_id    TEXT PRIMARY KEY,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT,
    expires_at     REAL NOT NULL,
    scope          TEXT,
    connected_at   REAL NOT NULL,
    updated_at     REAL NOT NULL
);
"""

STATE_TTL_SECONDS = 600
DEFAULT_CUSTOMER = "default"


def db_path() -> str:
    return os.environ.get("DB_PATH") or "app.sqlite3"


@contextmanager
def connect():
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- OAuth state (single use, short lived) ---------------------------------

def save_state(state: str, customer_id: str | None) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (now - STATE_TTL_SECONDS,))
        conn.execute(
            "INSERT INTO oauth_state (state, customer_id, created_at) VALUES (?, ?, ?)",
            (state, customer_id, now),
        )


def pop_state(state: str) -> sqlite3.Row | None:
    """Return and delete a state row if it exists and has not expired."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM oauth_state WHERE state = ?", (state,)).fetchone()
        conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
        conn.execute("COMMIT")
    if row is None or row["created_at"] < time.time() - STATE_TTL_SECONDS:
        return None
    return row


# --- Connections ------------------------------------------------------------

def save_connection(customer_id: str, token: dict) -> None:
    now = time.time()
    expires_in = float(token.get("expires_in") or 3600)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO connection
                (customer_id, access_token, refresh_token, expires_at, scope, connected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = COALESCE(excluded.refresh_token, connection.refresh_token),
                expires_at = excluded.expires_at,
                scope = COALESCE(excluded.scope, connection.scope),
                updated_at = excluded.updated_at
            """,
            (
                customer_id,
                token["access_token"],
                token.get("refresh_token"),
                now + expires_in,
                token.get("scope"),
                now,
                now,
            ),
        )


def get_connection(customer_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM connection WHERE customer_id = ?", (customer_id,)
        ).fetchone()


def claim_default_connection(customer_id: str) -> sqlite3.Row | None:
    """Bind a connection made without a known workspace to the first verified viewer.

    If /connect was opened outside Campfire (no cf_ctx) and the token response did
    not name the workspace, the token is stored under "default". The first page view
    with a verified cf_ctx claims it, so a second workspace can never read it.
    """
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM connection WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "UPDATE connection SET customer_id = ? WHERE customer_id = ?",
                (customer_id, DEFAULT_CUSTOMER),
            )
        conn.execute("COMMIT")
    return get_connection(customer_id)


def delete_connection(customer_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM connection WHERE customer_id = ?", (customer_id,))
