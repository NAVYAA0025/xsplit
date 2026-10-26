import re
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS currency(code TEXT PRIMARY KEY, name TEXT, symbol TEXT);
CREATE TABLE IF NOT EXISTS user(
    user_id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, phone TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS friend(
    user_id INTEGER, friend_id INTEGER, PRIMARY KEY(user_id, friend_id));
CREATE TABLE IF NOT EXISTS grp(
    group_id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL,
    base_currency TEXT NOT NULL REFERENCES currency(code), status TEXT DEFAULT 'Active',
    created_by INTEGER REFERENCES user(user_id), created_date TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS group_member(
    member_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES user(user_id), guest_name TEXT, guest_contact TEXT, guest_token TEXT UNIQUE,
    role TEXT DEFAULT 'Member', join_date TEXT DEFAULT CURRENT_TIMESTAMP, is_guest INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1, invite_status TEXT, invited_by INTEGER REFERENCES user(user_id));
CREATE TABLE IF NOT EXISTS expense(
    expense_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    paid_by INTEGER NOT NULL REFERENCES group_member(member_id), description TEXT, category TEXT DEFAULT 'Other',
    amount REAL NOT NULL, currency TEXT NOT NULL REFERENCES currency(code), expense_date TEXT,
    exchange_rate_used REAL NOT NULL, amount_base REAL NOT NULL, rate_source TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS expense_split(
    split_id INTEGER PRIMARY KEY AUTOINCREMENT, expense_id INTEGER NOT NULL REFERENCES expense(expense_id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES group_member(member_id), share_amount_base REAL NOT NULL,
    share_percentage REAL, share_amount_original REAL NOT NULL, split_type TEXT, payment_status TEXT DEFAULT 'Pending');
CREATE TABLE IF NOT EXISTS expense_item(
    item_id INTEGER PRIMARY KEY AUTOINCREMENT, expense_id INTEGER NOT NULL REFERENCES expense(expense_id) ON DELETE CASCADE,
    name TEXT NOT NULL, price REAL NOT NULL, members TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipt(
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, expense_id INTEGER REFERENCES expense(expense_id) ON DELETE CASCADE,
    file_url TEXT, uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP, extracted_amount REAL, extracted_currency TEXT,
    content BLOB, content_type TEXT);
CREATE TABLE IF NOT EXISTS settlement(
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    payer_id INTEGER NOT NULL REFERENCES group_member(member_id), receiver_id INTEGER NOT NULL REFERENCES group_member(member_id),
    amount_base REAL NOT NULL, settlement_date TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'Completed', notes TEXT);
"""


INDEXES = """
CREATE INDEX IF NOT EXISTS ix_member_group ON group_member(group_id);
CREATE INDEX IF NOT EXISTS ix_member_user ON group_member(user_id);
CREATE INDEX IF NOT EXISTS ix_expense_group ON expense(group_id);
CREATE INDEX IF NOT EXISTS ix_split_expense ON expense_split(expense_id);
CREATE INDEX IF NOT EXISTS ix_settlement_group ON settlement(group_id);
CREATE INDEX IF NOT EXISTS ix_receipt_expense ON receipt(expense_id);
CREATE INDEX IF NOT EXISTS ix_receipt_url ON receipt(file_url);
CREATE INDEX IF NOT EXISTS ix_item_expense ON expense_item(expense_id);
"""


MIGRATIONS = {"group_member": [("invite_status", "TEXT"), ("invited_by", "INTEGER")],
              "receipt": [("content", "BLOB"), ("content_type", "TEXT")]}


_USER = re.compile(r"\buser\b")


def to_postgres(sql):
    sql = _USER.sub('"user"', sql)
    sql = sql.replace("%", "%%").replace("?", "%s")
    if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", sql, re.I):
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.I) + " ON CONFLICT DO NOTHING"
    if re.match(r"\s*INSERT\b", sql, re.I):
        sql += " RETURNING *"
    return sql


def postgres_schema(sql):
    sql = _USER.sub('"user"', sql)
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    return re.sub(r"\bBLOB\b", "BYTEA", re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql))


class _Result:
    def __init__(self, cur=None, lastrowid=None, rows=None):
        self._cur, self.lastrowid, self._rows = cur, lastrowid, rows

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        return self._cur.fetchone() if self._cur is not None and self._cur.description else None

    def fetchall(self):
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return self._cur.fetchall() if self._cur is not None and self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())


class PgDB:

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        if re.match(r"\s*(BEGIN|PRAGMA)\b", sql, re.I):
            return _Result()
        cur = self.conn.execute(to_postgres(sql), tuple(params))
        if re.match(r"\s*INSERT\b", sql, re.I):
            row = cur.fetchone() if cur.description else None
            return _Result(lastrowid=next(iter(row.values())) if row else None, rows=[row] if row else [])
        return _Result(cur)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()


def _pg_pool(url):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    return ConnectionPool(url, min_size=1, max_size=5, kwargs={"row_factory": dict_row, "autocommit": False},
                          check=ConnectionPool.check_connection, open=True)


def init_db(app, currencies):
    url = app.config.get("DATABASE_URL")
    if url:
        app.extensions["xsplit_pg"] = pool = _pg_pool(url)
        with pool.connection() as conn:
            conn.execute(postgres_schema(SCHEMA))
            for table, cols in MIGRATIONS.items():
                for name, typ in cols:
                    conn.execute(postgres_schema(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {typ}"))
            conn.execute(INDEXES)
            for c in currencies:
                conn.execute("INSERT INTO currency VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", c)
        return
    db = sqlite3.connect(app.config["DB_PATH"])
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    db.executescript(INDEXES)
    db.executemany("INSERT OR IGNORE INTO currency VALUES(?,?,?)", currencies)
    db.commit()
    db.close()


def get_db():
    if "db" not in g:
        pool = current_app.extensions.get("xsplit_pg")
        if pool:
            g.db = PgDB(pool.getconn())
        else:
            g.db = sqlite3.connect(current_app.config["DB_PATH"], timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is None:
        return
    if isinstance(db, PgDB):
        db.rollback()
        current_app.extensions["xsplit_pg"].putconn(db.conn)
    else:
        db.close()
