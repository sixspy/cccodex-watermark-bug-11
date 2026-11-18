import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 批次状态：待盖印 → 已盖印；异常的“空边印”收进候审只读；作废为终态。
RUN_BATCH_STATUSES = ("待盖印", "已盖印", "候审只读", "已作废")

SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','候审只读','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  blocked_count INTEGER NOT NULL DEFAULT 0,
  review_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 写印裁定流水：每次点写印都落一条，允许/拒绝都可核。
CREATE TABLE IF NOT EXISTS stamp_adjudications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER,
  table_name TEXT,
  run_code TEXT,
  watermark_value TEXT,
  verdict TEXT NOT NULL CHECK (verdict IN ('allowed','rejected')),
  reason_code TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  fate TEXT,
  readiness_snapshot TEXT,
  operator TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_down ON lineage_edges(downstream_run_code);
CREATE INDEX IF NOT EXISTS idx_edges_up ON lineage_edges(upstream_run_code);
CREATE INDEX IF NOT EXISTS idx_batches_code ON run_batches(run_code);
CREATE INDEX IF NOT EXISTS idx_adj_batch ON stamp_adjudications(batch_id);
"""

# 库级闸门必须在 run_batches 迁移（重建表会连带丢触发器）之后再建。
GUARDS = """
-- 最后一道闸：即使绕过服务层，库也不许未齐备落印、不许双写。
CREATE TRIGGER IF NOT EXISTS trg_stamp_no_double
BEFORE UPDATE ON run_batches
WHEN NEW.status = '已盖印' AND OLD.status = '已盖印'
BEGIN
  SELECT RAISE(ABORT, 'ERR_DOUBLE_STAMP');
END;

CREATE TRIGGER IF NOT EXISTS trg_stamp_requires_ready
BEFORE UPDATE OF status ON run_batches
WHEN NEW.status = '已盖印'
  AND EXISTS (
    SELECT 1 FROM lineage_edges e
      LEFT JOIN run_batches u ON u.run_code = e.upstream_run_code
     WHERE e.downstream_run_code = NEW.run_code
       AND (u.id IS NULL OR u.status != '已盖印')
  )
BEGIN
  SELECT RAISE(ABORT, 'ERR_SOURCE_NOT_READY');
END;

-- 直达库的 INSERT 也不许带进未齐备的印。
CREATE TRIGGER IF NOT EXISTS trg_stamp_insert_requires_ready
BEFORE INSERT ON run_batches
WHEN NEW.status = '已盖印'
  AND EXISTS (
    SELECT 1 FROM lineage_edges e
      LEFT JOIN run_batches u ON u.run_code = e.upstream_run_code
     WHERE e.downstream_run_code = NEW.run_code
       AND (u.id IS NULL OR u.status != '已盖印')
  )
BEGIN
  SELECT RAISE(ABORT, 'ERR_SOURCE_NOT_READY');
END;
"""

# 重建 run_batches 时使用的现行定义（CHECK 已放宽到四种状态）。
RUN_BATCHES_DDL = """
CREATE TABLE run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','候审只读','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # timeout + busy_timeout：两个值班同时写印时，后到者先等锁再判，不双写。
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_tx(db_path: str | None = None):
    """写事务：BEGIN IMMEDIATE 先拿写锁，齐备判定与落印在同一事务内完成。"""
    conn = connect(db_path)
    try:
        conn.isolation_level = None  # 关掉隐式事务，改为显式控制
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_batches'"
    ).fetchone()
    if row and "候审只读" not in (row["sql"] or ""):
        _rebuild_run_batches(conn)

    report_cols = _columns(conn, "gap_reports")
    for col in ("blocked_count", "review_count"):
        if col not in report_cols:
            conn.execute(
                f"ALTER TABLE gap_reports ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
            )

    adj_cols = _columns(conn, "stamp_adjudications")
    if adj_cols and "operator" not in adj_cols:
        conn.execute(
            "ALTER TABLE stamp_adjudications ADD COLUMN operator TEXT NOT NULL DEFAULT 'admin'"
        )


def _rebuild_run_batches(conn: sqlite3.Connection) -> None:
    old_cols = _columns(conn, "run_batches")
    keep = [
        c
        for c in (
            "id",
            "table_name",
            "run_code",
            "biz_date",
            "status",
            "watermark_value",
            "stamped_at",
            "created_at",
        )
        if c in old_cols
    ]
    conn.execute("ALTER TABLE run_batches RENAME TO run_batches_legacy")
    conn.execute(RUN_BATCHES_DDL)
    if keep:
        cols = ",".join(keep)
        conn.execute(
            f"INSERT INTO run_batches({cols}) SELECT {cols} FROM run_batches_legacy"
        )
    conn.execute("DROP TABLE run_batches_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_code ON run_batches(run_code)")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(GUARDS)

        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            for table, col, ver, status in SEED_TEMPLATES:
                conn.execute(
                    "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                    "VALUES (?,?,?,?)",
                    (table, col, ver, status),
                )
            for table, code, biz, status, wm in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        table,
                        code,
                        biz,
                        status,
                        wm,
                        "2026-11-13 18:00:00" if status == "已盖印" else None,
                    ),
                )
            for up, down in SEED_EDGES:
                conn.execute(
                    "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                    (up, down),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
