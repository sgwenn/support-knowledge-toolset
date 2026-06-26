import os
import sqlite3
import time

_DB_PATH = os.environ.get("DB_PATH", "coverage.db")
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init() -> None:
    conn = _get_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gap_records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL UNIQUE,
            subject     TEXT,
            component   TEXT NOT NULL,
            score       REAL,
            surfaced_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS coverage_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            component       TEXT NOT NULL,
            confluence_url  TEXT,
            title           TEXT,
            published_at    REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            service       TEXT PRIMARY KEY,
            access_token  TEXT NOT NULL,
            refresh_token TEXT,
            expires_at    INTEGER,
            cloud_id      TEXT,
            updated_at    INTEGER
        );
    """)
    conn.commit()
    if os.path.exists(_DB_PATH):
        os.chmod(_DB_PATH, 0o600)


def get_atlassian_tokens() -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at, cloud_id FROM oauth_tokens WHERE service = 'atlassian'"
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def save_atlassian_tokens(
    access_token: str,
    refresh_token: str | None,
    expires_at: int | None,
    cloud_id: str | None,
) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO oauth_tokens (service, access_token, refresh_token, expires_at, cloud_id, updated_at) "
        "VALUES ('atlassian', ?, ?, ?, ?, ?)",
        (access_token, refresh_token, expires_at, cloud_id, int(time.time())),
    )
    conn.commit()


def add_gap_records(tickets: list[dict]) -> None:
    if not tickets:
        return
    conn = _get_conn()
    now = time.time()
    conn.executemany(
        "INSERT OR IGNORE INTO gap_records (ticket_id, subject, component, score, surfaced_at) "
        "VALUES (:ticket_id, :subject, :component, :score, :surfaced_at)",
        [
            {
                "ticket_id": t["ticket_id"],
                "subject": t.get("subject"),
                "component": t["component"],
                "score": t.get("score", 0),
                "surfaced_at": now,
            }
            for t in tickets
        ],
    )
    conn.commit()


def add_coverage_record(component: str, confluence_url: str, title: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO coverage_records (component, confluence_url, title, published_at) "
        "VALUES (?, ?, ?, ?)",
        (component, confluence_url, title, time.time()),
    )
    conn.commit()


def get_coverage() -> dict:
    conn = _get_conn()

    component_rows = conn.execute("""
        SELECT
            g.component,
            g.gaps,
            COALESCE(c.addressed, 0) AS addressed
        FROM (
            SELECT component, COUNT(*) AS gaps
            FROM gap_records
            GROUP BY component
        ) g
        LEFT JOIN (
            SELECT component, COUNT(*) AS addressed
            FROM coverage_records
            GROUP BY component
        ) c ON c.component = g.component
        ORDER BY g.gaps DESC
    """).fetchall()

    timeline_rows = conn.execute("""
        SELECT
            strftime('%Y-W%W', datetime(published_at, 'unixepoch')) AS week,
            COUNT(*) AS count
        FROM coverage_records
        GROUP BY week
        ORDER BY week ASC
    """).fetchall()

    return {
        "components": [
            {
                "component": row["component"],
                "gaps": row["gaps"],
                "addressed": row["addressed"],
            }
            for row in component_rows
        ],
        "weekly_timeline": [
            {"week": row["week"], "count": row["count"]}
            for row in timeline_rows
        ],
    }
