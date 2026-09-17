"""
Database layer for the Hangar app.
Everything is stored in a single local SQLite file (data/hangar.db) so
aircraft and their STC/337 records persist between sessions.
"""
import sqlite3
import os
from datetime import datetime
import paths

DB_PATH = os.path.join(paths.data_dir(), "hangar.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode lets a background processing thread write while the main
    # request thread reads job progress at the same time, without one
    # blocking the other the way SQLite's default journal mode can.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS aircraft (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tail_number TEXT NOT NULL,
        make_model TEXT,
        serial_number TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stc_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
        stc_number TEXT NOT NULL,
        holder TEXT,
        description TEXT,
        source_file TEXT,
        source_pages TEXT,
        confidence TEXT,
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS form337_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
        reg_mark TEXT,
        work_date TEXT,
        summary TEXT,
        stc_refs TEXT,
        source_file TEXT,
        source_pages TEXT,
        confidence TEXT,
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS review_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
        source_file TEXT,
        page INTEGER,
        reason TEXT,
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS registration_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
        reg_mark TEXT NOT NULL,
        date_str TEXT,
        date_parsed TEXT,
        confidence TEXT,
        source_file TEXT,
        source_page INTEGER,
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS processing_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        current_page INTEGER DEFAULT 0,
        total_pages INTEGER DEFAULT 0,
        added_stc INTEGER DEFAULT 0,
        added_337 INTEGER DEFAULT 0,
        error_message TEXT,
        started_at TEXT,
        finished_at TEXT
    );
    """)
    conn.commit()
    conn.close()


# ---------- Aircraft ----------

def add_aircraft(tail_number, make_model="", serial_number=""):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO aircraft (tail_number, make_model, serial_number, created_at) VALUES (?, ?, ?, ?)",
        (tail_number, make_model, serial_number, datetime.now().isoformat()),
    )
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def list_aircraft():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM aircraft ORDER BY tail_number").fetchall()
    conn.close()
    return rows


def get_aircraft(aircraft_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM aircraft WHERE id = ?", (aircraft_id,)).fetchone()
    conn.close()
    return row


def delete_aircraft(aircraft_id):
    conn = get_conn()
    conn.execute("DELETE FROM aircraft WHERE id = ?", (aircraft_id,))
    conn.commit()
    conn.close()


# ---------- STC records ----------

def add_stc_record(aircraft_id, stc_number, holder, description, source_file, source_pages, confidence="ok"):
    conn = get_conn()
    conn.execute(
        """INSERT INTO stc_records
           (aircraft_id, stc_number, holder, description, source_file, source_pages, confidence, added_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (aircraft_id, stc_number, holder, description, source_file, source_pages, confidence, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_stc_records(aircraft_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM stc_records WHERE aircraft_id = ? ORDER BY stc_number", (aircraft_id,)
    ).fetchall()
    conn.close()
    return rows


def stc_exists(aircraft_id, stc_number):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM stc_records WHERE aircraft_id = ? AND stc_number = ?", (aircraft_id, stc_number)
    ).fetchone()
    conn.close()
    return row is not None


# ---------- 337 records ----------

def add_337_record(aircraft_id, reg_mark, work_date, summary, stc_refs, source_file, source_pages, confidence="ok"):
    conn = get_conn()
    conn.execute(
        """INSERT INTO form337_records
           (aircraft_id, reg_mark, work_date, summary, stc_refs, source_file, source_pages, confidence, added_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (aircraft_id, reg_mark, work_date, summary, stc_refs, source_file, source_pages, confidence, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_337_records(aircraft_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM form337_records WHERE aircraft_id = ? ORDER BY work_date", (aircraft_id,)
    ).fetchall()
    conn.close()
    return rows


# ---------- Review flags ----------

def add_review_flag(aircraft_id, source_file, page, reason):
    conn = get_conn()
    conn.execute(
        "INSERT INTO review_flags (aircraft_id, source_file, page, reason, added_at) VALUES (?, ?, ?, ?, ?)",
        (aircraft_id, source_file, page, reason, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_review_flags(aircraft_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM review_flags WHERE aircraft_id = ? ORDER BY source_file, page", (aircraft_id,)
    ).fetchall()
    conn.close()
    return rows


# ---------- Registration history ----------

def add_registration_event(aircraft_id, reg_mark, date_str, date_parsed_iso, confidence, source_file, source_page):
    conn = get_conn()
    conn.execute(
        """INSERT INTO registration_events
           (aircraft_id, reg_mark, date_str, date_parsed, confidence, source_file, source_page, added_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (aircraft_id, reg_mark, date_str, date_parsed_iso, confidence, source_file, source_page,
         datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_registration_events(aircraft_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM registration_events WHERE aircraft_id = ? ORDER BY date_parsed IS NULL, date_parsed DESC",
        (aircraft_id,),
    ).fetchall()
    conn.close()
    return rows


def update_tail_number(aircraft_id, tail_number):
    conn = get_conn()
    conn.execute("UPDATE aircraft SET tail_number = ? WHERE id = ?", (tail_number, aircraft_id))
    conn.commit()
    conn.close()


def refresh_tail_from_registration(aircraft_id):
    """
    Set the aircraft's displayed tail number to whichever registration mark
    has the most recent *parseable* date on file. High-confidence events
    (explicit re-registration letters) win over medium-confidence ones
    (airworthiness certificate reads) on the same date. Events with a date
    that couldn't be parsed are kept in history but never used to decide
    the current tail number, since we can't rank them.

    Returns the new tail number, or None if there's no dated evidence yet
    (in which case the manually-entered/original tail number is left alone).
    """
    events = get_registration_events(aircraft_id)
    dated = [e for e in events if e['date_parsed']]
    if not dated:
        return None
    conf_rank = {'high': 2, 'medium': 1, 'low': 0}
    best = max(dated, key=lambda e: (e['date_parsed'], conf_rank.get(e['confidence'], 0)))
    update_tail_number(aircraft_id, best['reg_mark'])
    return best['reg_mark']


# ---------- Background processing jobs ----------
# These let file processing run in a background thread, decoupled from any
# single web request - so a long OCR job isn't at the mercy of a hosting
# platform's request/connection time limit. Any page load can check status
# by reading this table, regardless of which browser session started the job
# or whether that session is even still connected.

def create_job(aircraft_id, filename):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO processing_jobs (aircraft_id, filename, status, started_at) VALUES (?, ?, 'processing', ?)",
        (aircraft_id, filename, datetime.now().isoformat()),
    )
    conn.commit()
    jid = cur.lastrowid
    conn.close()
    return jid


def update_job_progress(job_id, current_page, total_pages):
    conn = get_conn()
    conn.execute(
        "UPDATE processing_jobs SET current_page = ?, total_pages = ? WHERE id = ?",
        (current_page, total_pages, job_id),
    )
    conn.commit()
    conn.close()


def finish_job(job_id, added_stc=0, added_337=0, error_message=None):
    conn = get_conn()
    status = 'error' if error_message else 'done'
    conn.execute(
        """UPDATE processing_jobs
           SET status = ?, added_stc = ?, added_337 = ?, error_message = ?, finished_at = ?
           WHERE id = ?""",
        (status, added_stc, added_337, error_message, datetime.now().isoformat(), job_id),
    )
    conn.commit()
    conn.close()


def get_jobs(aircraft_id, limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM processing_jobs WHERE aircraft_id = ? ORDER BY id DESC LIMIT ?",
        (aircraft_id, limit),
    ).fetchall()
    conn.close()
    return rows


def get_active_jobs(aircraft_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM processing_jobs WHERE aircraft_id = ? AND status = 'processing' ORDER BY id",
        (aircraft_id,),
    ).fetchall()
    conn.close()
    return rows
