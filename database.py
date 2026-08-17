"""多租户数据库层（双模式：Postgres 生产 / SQLite 本地开发）。

数据模型（每个小店数据完全隔离）：
- shops      店铺（id, name, slug, 每日营业时间, 时段粒度, created_at）
- users      账号（id, shop_id, username, password_hash, role）
- services   服务（id, shop_id, name, duration_min, price）
- bookings   预约（id, shop_id, service_id, customer_name, customer_email, customer_phone, ...）
- blackout_dates  特定日期关店/休假

设计要点（v2 / v3）：
- 设了环境变量 DATABASE_URL 时连 Postgres（psycopg2），否则回退本地 SQLite。
  一套代码两种后端：本地开发零配置、生产用托管数据库不丢数据（解决 SQLite 跑在
  临时磁盘会被清空的致命问题）。
- _PGConn 适配层把 psycopg2 包装成与 sqlite3 一致的 API
  （conn.execute / fetchone / lastrowid / commit / rollback / close），
  所以 main.py 等调用方基本不用改；占位符 ? 自动转 %s，INSERT 自动补 RETURNING id。
- bookings 部分唯一索引 UNIQUE(shop_id, start_utc) WHERE status IN ('pending','confirmed')，
  从数据库层杜绝「同一时段被重复售卖」，同时允许取消/完成后的时段被重新预约。
- seed_demo_shop 用 BEGIN IMMEDIATE（SQLite）/ 事务（Postgres） + 重试，修复多 worker 启动竞态。
"""
import os
import re
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # psycopg2 仅生产需要；本地开发没有也能跑
    psycopg2 = None
    RealDictCursor = None

DATABASE_URL = os.getenv("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)

# SQLite 回退用的本地库路径（仅本地开发）
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "booking.db"))
SHOP_TZ = ZoneInfo(os.getenv("SHOP_TZ", "Pacific/Auckland"))

# 统一的主键/外键冲突异常类型（同时覆盖 sqlite3 与 psycopg2）
if psycopg2:
    INTEGRITY_ERROR = (sqlite3.IntegrityError, psycopg2.IntegrityError)
else:
    INTEGRITY_ERROR = sqlite3.IntegrityError

# 别名，供 main.py 等调用方 `from database import IntegrityError` 使用
IntegrityError = INTEGRITY_ERROR

# 默认营业时间：以「周一=0 … 周日=6」为键。值 [open, close] 为 "HH:MM"，None 表示当天休息。
# 老板可以在后台按天自定义；这里只是新店的初始值。
DEFAULT_OPENING_HOURS = {
    "0": ["09:00", "18:00"],  # Mon
    "1": ["09:00", "18:00"],  # Tue
    "2": ["09:00", "18:00"],  # Wed
    "3": ["09:00", "18:00"],  # Thu
    "4": ["09:00", "18:00"],  # Fri
    "5": ["09:00", "14:00"],  # Sat
    "6": None,                # Sun (closed)
}


# ---------- Postgres 适配层 ----------
class _PGConn:
    """把 psycopg2 连接包装成与 sqlite3 一致的接口，让上层代码无需区分后端。"""

    def __init__(self, raw):
        self._raw = raw
        self._cur = raw.cursor(cursor_factory=RealDictCursor)
        self._cur.lastrowid = None  # 兼容 app 里 cur.lastrowid 的写法

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        up = sql.strip().upper()
        # INSERT 自动补 RETURNING id，供 cur.lastrowid 使用（ON CONFLICT 分支不需要）
        if up.startswith("INSERT") and "RETURNING" not in up and "ON CONFLICT" not in up:
            sql += " RETURNING id"
        self._cur.execute(sql, params or ())
        if up.endswith("RETURNING ID"):
            row = self._cur.fetchone()
            self._cur.lastrowid = row["id"] if row else None
        return self._cur

    def executescript(self, sql):
        # Postgres 没有 executescript；按分号拆成多条语句依次执行
        for stmt in sql.split(";"):
            s = stmt.strip()
            if not s:
                continue
            self._cur.execute(s.replace("?", "%s"))
        return None

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass
        self._raw.close()


def get_conn():
    """每次返回一个全新的连接。生产用 Postgres，本地回退 SQLite。"""
    if IS_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=30)
        return _PGConn(conn)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn():
    """连接上下文管理器：退出时关闭连接，杜绝泄漏或「拿到已关闭连接」。"""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def slugify(name: str) -> str:
    """把店铺名变成 URL 友好的 slug，例如 "Browns Bay Barber" -> "browns-bay-barber"。"""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "shop"


def normalize_windows(entry):
    """把某天的营业时间定义统一成「窗口列表」[[open, close], ...]，休息返回 None。"""
    if not entry:
        return None
    if isinstance(entry, list):
        windows = []
        if len(entry) > 0 and isinstance(entry[0], list):
            for w in entry:
                if isinstance(w, list) and len(w) == 2 and all(isinstance(x, str) for x in w):
                    windows.append([w[0], w[1]])
        elif len(entry) == 2 and isinstance(entry[0], str):
            windows = [[entry[0], entry[1]]]
        return windows if windows else None
    return None


def parse_opening_hours(shop_row) -> dict:
    """把 shops.opening_hours（JSON 文本）解析成 {weekday: [[open, close], ...] | None}。"""
    raw = shop_row["opening_hours"] if "opening_hours" in shop_row.keys() else None
    out = {}
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k in range(7):
                    out[str(k)] = normalize_windows(data.get(str(k)))
                return out
        except Exception:
            pass
    for k in range(7):
        v = DEFAULT_OPENING_HOURS.get(str(k))
        out[str(k)] = normalize_windows(v)
    return out


def day_hours(shop_row, weekday: int):
    """返回某店某星期几的营业窗口列表 [(open_h, open_m, close_h, close_m), ...]，休息日返回 None。"""
    oh = parse_opening_hours(shop_row)
    wins = oh.get(str(weekday))
    if not wins:
        return None
    out = []
    for w in wins:
        try:
            oh_h, oh_m = _hhmm(w[0])
            ch_h, ch_m = _hhmm(w[1])
        except Exception:
            continue
        if ch_h * 60 + ch_m <= oh_h * 60 + oh_m:
            continue
        out.append((oh_h, oh_m, ch_h, ch_m))
    return out if out else None


def _hhmm(s: str):
    s = s.strip()
    if ":" in s:
        h, m = s.split(":")
        return int(h), int(m)
    return int(s), 0


def _add_column(conn, table: str, col: str, typedef: str):
    """跨后端安全地给表加列（不存在才加）。"""
    if IS_POSTGRES:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (table, col),
        ).fetchone()
        if not exists:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
    else:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")


# 两种后端的建表 DDL（仅类型/默认值语法不同）
SCHEMA = {
    False: """
        CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            business_hours_start INTEGER DEFAULT 9,
            business_hours_end   INTEGER DEFAULT 18,
            slot_minutes         INTEGER DEFAULT 30,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'shop_owner',
            created_at TEXT NOT NULL,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            duration_min INTEGER NOT NULL,
            price REAL DEFAULT 0,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            customer_name TEXT,
            customer_phone TEXT,
            start_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT,
            reminded_24h INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (shop_id) REFERENCES shops(id),
            FOREIGN KEY (service_id) REFERENCES services(id)
        );
        CREATE TABLE IF NOT EXISTS blackout_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            date_str TEXT NOT NULL,
            note TEXT,
            UNIQUE(shop_id, date_str),
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
    """,
    True: """
        CREATE TABLE IF NOT EXISTS shops (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            business_hours_start INTEGER DEFAULT 9,
            business_hours_end   INTEGER DEFAULT 18,
            slot_minutes         INTEGER DEFAULT 30,
            created_at TEXT NOT NULL DEFAULT now()::text
        );
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            shop_id INTEGER NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'shop_owner',
            created_at TEXT NOT NULL DEFAULT now()::text,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        CREATE TABLE IF NOT EXISTS services (
            id SERIAL PRIMARY KEY,
            shop_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            duration_min INTEGER NOT NULL,
            price REAL DEFAULT 0,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            shop_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            customer_name TEXT,
            customer_phone TEXT,
            start_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT,
            reminded_24h INTEGER DEFAULT 0,
            created_at TEXT DEFAULT now()::text,
            FOREIGN KEY (shop_id) REFERENCES shops(id),
            FOREIGN KEY (service_id) REFERENCES services(id)
        );
        CREATE TABLE IF NOT EXISTS blackout_dates (
            id SERIAL PRIMARY KEY,
            shop_id INTEGER NOT NULL,
            date_str TEXT NOT NULL,
            note TEXT,
            UNIQUE(shop_id, date_str),
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
    """,
}


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA[IS_POSTGRES])
        # 多租户扩展列：每日营业时间（JSON） + 顾客邮箱 + 每日容量 + 自助管理令牌
        _add_column(conn, "shops", "opening_hours", "TEXT")
        _add_column(conn, "bookings", "customer_email", "TEXT")
        _add_column(conn, "shops", "daily_capacity", "INTEGER")
        _add_column(conn, "bookings", "manage_token", "TEXT")
        # 关键约束：同一小店同一 UTC 时段只允许一条「有效」预约，从数据库层杜绝重复售卖。
        # 用「部分唯一索引」只覆盖有效状态(pending/confirmed)；取消/完成/失约的时段
        # 视为已释放，可重新预约（与 _slot_free / occupied_intervals 的逻辑一致）。
        # 不能与现有重复数据冲突——若库里已有重复有效行则跳过并告警（需先清理旧数据）。
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_slot "
                "ON bookings(shop_id, start_utc) "
                "WHERE status IN ('pending', 'confirmed')"
            )
        except INTEGRITY_ERROR:
            print("[DB] 警告：bookings 中存在重复的 (shop_id, start_utc) 有效预约，"
                  "未创建唯一索引，请清理重复预约后再重启。")
        # 性能索引：按店铺/状态/邮箱查询预约更顺滑
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_shop_status "
                     "ON bookings(shop_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_shop_email "
                     "ON bookings(shop_id, customer_email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_start "
                     "ON bookings(start_utc)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- Blackout dates（特定日期关店 / 休假） ----------
def add_blackout(shop_id: int, date_str: str, note: str = None):
    with db_conn() as conn:
        # ON CONFLICT DO NOTHING 在 SQLite(>=3.24) 与 Postgres 均可用
        conn.execute(
            "INSERT INTO blackout_dates(shop_id, date_str, note) VALUES (?, ?, ?) "
            "ON CONFLICT (shop_id, date_str) DO NOTHING",
            (shop_id, date_str, note),
        )
        conn.commit()


def remove_blackout(shop_id: int, date_str: str):
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM blackout_dates WHERE shop_id = ? AND date_str = ?",
            (shop_id, date_str),
        )
        conn.commit()


def list_blackouts(shop_id: int):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT date_str, note FROM blackout_dates WHERE shop_id = ? ORDER BY date_str",
            (shop_id,),
        ).fetchall()
        return [{"date": r["date_str"], "note": r["note"]} for r in rows]


# ---------- 演示店铺种入（多 worker 安全） ----------
def seed_demo_shop():
    """首次运行种入一个演示店铺。
    用 BEGIN IMMEDIATE（SQLite）/ 事务（Postgres）获取写锁，使并发启动的多个 worker
    串行化：后到的 worker 会看到已存在的店铺而直接返回，避免 UNIQUE(shops.slug) 竞态崩溃。
    """
    from auth_utils import hash_password

    conn = get_conn()
    try:
        if not IS_POSTGRES:
            conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT id FROM shops LIMIT 1").fetchone():
            conn.rollback()
            return
        created = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO shops(name, slug, created_at) VALUES (?, ?, ?)",
            ("Demo Barber", "demo", created),
        )
        sid = cur.lastrowid
        conn.execute(
            "INSERT INTO users(shop_id, username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, 'shop_owner', ?)",
            (sid, "admin", hash_password("admin123"), created),
        )
        conn.execute(
            "INSERT INTO services(shop_id, name, duration_min, price) VALUES (?, 'Haircut', 30, 30)",
            (sid,),
        )
        conn.commit()
    except INTEGRITY_ERROR:
        # 并发情况下另一 worker 先写入了 demo 店铺：放弃本事务，安全退出。
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
