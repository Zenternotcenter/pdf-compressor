# -*- coding: utf-8 -*-
"""
========================================================================================
เลเยอร์จัดการฐานข้อมูลแบบไฮบริด (Hybrid Database Layer: SQLite + Supabase PostgreSQL)
========================================================================================
สถาปัตยกรรม:
  1. Local Development / Offline: ใช้ SQLite (worklog.db) อัตโนมัติ รวดเร็ว ไม่ต้องต่อเน็ต
  2. Cloud Production / Serverless: ใช้ Supabase PostgreSQL 17 ผ่านตัวแปร DATABASE_URL
  3. Seamless Abstraction: โค้ดทั้งหมดใช้คำสั่ง SQL และ Interface แบบเดียวกันผ่าน Wrapper
  4. Serverless Optimization:
     - แปลง URL จาก Direct Domain (IPv6) ไปเป็น Supabase Transaction Pooler (IPv4 พอร์ต 6543)
       เพื่อรองรับ Vercel Serverless Function และ AWS Lambda ที่ไม่รองรับ IPv6 โดยตรง
     - สลับการเชื่อมต่อระหว่าง psycopg2 (C-Extension) และ pg8000 (Pure Python) อัตโนมัติ

โมดูลภายใน:
  - Task / Worklog: บันทึกและสรุปรายงานการปฏิบัติงานประจำวัน
  - User Authentication & RBAC: ระบบผู้ใช้งานและแยกสิทธิ์ (Super Admin / Staff)
  - Department Management: จัดการโครงสร้างแผนกในองค์กร
  - IT Asset Management: ทะเบียนทรัพย์สินไอที และระบบจับคู่ชุดโต๊ะทำงาน (Workstation Bundling)
========================================================================================
"""

import os
import re
import sqlite3
import hashlib
import shutil
import glob
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# 1. การกำหนดค่าการเชื่อมต่อฐานข้อมูล (Database Configuration)
# ============================================================

# ดึงค่า Connection String จาก Environment Variable (เช่น บน Vercel หรือ .env)
raw_db_url = os.environ.get("DATABASE_URL", "").strip()

# ตรวจสอบและตัดวงเล็บก้ามปู [password] กรณีผู้ใช้วางรหัสผ่านโดยไม่ลบวงเล็บตัวอย่างจาก Supabase
if ":[" in raw_db_url and "]@" in raw_db_url:
    raw_db_url = re.sub(r":\[(.*?)\]@", r":\1@", raw_db_url)

# แปลง Domain โดยตรงของ Supabase (db.xxx.supabase.co ซึ่งเป็น IPv6-only)
# ไปเป็น AWS IPv4 Connection Pooler พอร์ต 6543 (Transaction Mode) เพื่อป้องกันการเชื่อมต่อล้มเหลวบน Vercel/Lambda
if "@db." in raw_db_url and ".supabase.co" in raw_db_url:
    m = re.search(r"@db\.([a-zA-Z0-9_-]+)\.supabase\.co(?::\d+)?", raw_db_url)
    if m:
        ref = m.group(1)
        raw_db_url = re.sub(
            r"://([^:@]+):",
            lambda match: f"://{match.group(1)}.{ref}:" if "." not in match.group(1) else f"://{match.group(1)}:",
            raw_db_url
        )
        raw_db_url = re.sub(
            r"@db\.[a-zA-Z0-9_-]+\.supabase\.co(:\d+)?",
            r"@aws-0-ap-northeast-1.pooler.supabase.com:6543",
            raw_db_url
        )

# บังคับใช้โหมด Transaction Pooler (พอร์ต 6543) ซึ่งเหมาะสมที่สุดสำหรับ Serverless Architecture
if "pooler.supabase.com:5432" in raw_db_url:
    raw_db_url = raw_db_url.replace("pooler.supabase.com:5432", "pooler.supabase.com:6543")

# กำหนดให้ต้องเข้ารหัส SSL ในการเชื่อมต่อเสมอ
if raw_db_url and "?" not in raw_db_url:
    raw_db_url += "?sslmode=require"

DATABASE_URL = raw_db_url

# ตรวจสอบไดรเวอร์ PostgreSQL ที่ติดตั้งอยู่ในระบบ (รองรับทั้ง psycopg2 และ pg8000)
HAS_PSYCOPG2 = False
HAS_PG8000 = False
try:
    import psycopg2
    from psycopg2.extras import DictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    pass

try:
    import pg8000.dbapi
    HAS_PG8000 = True
except ImportError:
    pass

# ตัวแปรสถานะ: ใช้งาน PostgreSQL หรือไม่
IS_POSTGRES = bool(
    DATABASE_URL and 
    (DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")) and 
    (HAS_PSYCOPG2 or HAS_PG8000)
)

# ที่อยู่ไฟล์ฐานข้อมูล SQLite กรณีรัน Local หรือออฟไลน์
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worklog.db")



# ============================================================
# 2. เลเยอร์แปลงไวยากรณ์ SQL และ Database Wrapper
# ============================================================

def translate_query(sql: str) -> str:
    """
    แปลงคำสั่ง SQL จากไวยากรณ์ SQLite ให้เป็นไวยากรณ์ PostgreSQL
    
    สิ่งที่ฟังก์ชันนี้ทำ:
      1. แทนที่ Placeholder พารามิเตอร์: แปลงเครื่องหมาย '?' ของ SQLite ให้เป็น '%s' ของ PostgreSQL
         โดยจะข้ามไม่แปลงหากเครื่องหมาย '?' นั้นอยู่ภายในข้อความ String Literal (ในเครื่องหมายคำพูดเดี่ยว)
      2. แปลงเงื่อนไข Case-Insensitive: แปลง 'COLLATE NOCASE' ของ SQLite ไปเป็นฟังก์ชัน LOWER()
         เพื่อให้การค้นหาข้อความแบบไม่คำนึงถึงตัวพิมพ์เล็กพิมพ์ใหญ่ทำงานได้เหมือนกันทั้งสองระบบ
         
    Args:
      sql (str): ข้อความคำสั่ง SQL ต้นฉบับ (รูปแบบ SQLite)
      
    Returns:
      str: คำสั่ง SQL ที่แปลงเป็นรูปแบบ PostgreSQL แล้ว
    """
    out = []
    in_quote = False
    for ch in sql:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif ch == "?" and not in_quote:
            out.append("%s")  # ใช้ %s สำหรับ PostgreSQL
        else:
            out.append(ch)
    translated = "".join(out)
    
    # จัดการการเปรียบเทียบ COLLATE NOCASE
    translated = re.sub(
        r"(\b\w+(?:\.\w+)?)\s*=\s*(%s|\?)\s*COLLATE\s+NOCASE",
        r"LOWER(\1) = LOWER(\2)",
        translated,
        flags=re.IGNORECASE
    )
    translated = re.sub(
        r"(\b\w+(?:\.\w+)?)\s*=\s*'([^']*)'\s*COLLATE\s+NOCASE",
        r"LOWER(\1) = LOWER('\2')",
        translated,
        flags=re.IGNORECASE
    )
    translated = re.sub(r"COLLATE\s+NOCASE", "", translated, flags=re.IGNORECASE)
    
    return translated


class PgCursorWrapper:
    """
    คลาส Wrapper สำหรับ Cursor ของ PostgreSQL
    ทำหน้าที่จำลอง Cursor API ของ SQLite (เช่น sqlite3.Cursor)
    เพื่อให้โค้ดที่เรียกใช้งานสามารถทำงานกับ PostgreSQL ได้อย่างโปร่งใสโดยไม่ต้องแก้โค้ดภายนอก
    
    ฟีเจอร์เด่น:
      - รองรับ property `lastrowid`: ใน SQLite เมื่อ INSERT จะได้ id อัตโนมัติ 
        สำหรับ PostgreSQL คลาสนี้จะยิงคำสั่ง 'SELECT LASTVAL()' เพื่อดึง ID ล่าสุดมาจำลองเป็น lastrowid ให้
      - รองรับ context manager (`with cursor:` ... )
      - ดึงข้อมูลได้ทั้ง fetchone, fetchall, fetchmany
    """
    def __init__(self, raw_cursor, conn_wrapper):
        self._cur = raw_cursor
        self._conn_wrapper = conn_wrapper
        self.lastrowid = None

    def execute(self, sql, params=None):
        """ประมวลผลคำสั่ง SQL โดยแปลงไวยากรณ์ก่อนส่งให้ไดรเวอร์ PostgreSQL จริง"""
        sql_pg = translate_query(sql)
        if params is None:
            self._cur.execute(sql_pg)
        else:
            self._cur.execute(sql_pg, tuple(params) if isinstance(params, (list, tuple)) else (params,))
            
        stripped = sql.strip().upper()
        # หากเป็นคำสั่ง INSERT ให้ดึง ID ล่าสุดมาเก็บไว้ใน lastrowid
        if stripped.startswith("INSERT"):
            try:
                with self._conn_wrapper._raw_conn.cursor() as id_cur:
                    id_cur.execute("SELECT LASTVAL()")
                    row = id_cur.fetchone()
                    self.lastrowid = row[0] if row else None
            except Exception:
                self.lastrowid = None
        else:
            self.lastrowid = None
        return self

    def executemany(self, sql, seq_of_parameters):
        """ประมวลผลคำสั่ง SQL เป็นชุดแบบ Batch"""
        sql_pg = translate_query(sql)
        self._cur.executemany(sql_pg, seq_of_parameters)
        return self

    def fetchone(self):
        """ดึงข้อมูลผลลัพธ์ 1 แถว"""
        return self._cur.fetchone()

    def fetchall(self):
        """ดึงข้อมูลผลลัพธ์ทั้งหมด"""
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        """ดึงข้อมูลผลลัพธ์ตามจำนวนที่ระบุ"""
        return self._cur.fetchmany(size) if size else self._cur.fetchmany()

    @property
    def description(self):
        """โครงสร้างคอลัมน์ของผลลัพธ์"""
        return self._cur.description

    @property
    def rowcount(self):
        """จำนวนแถวที่ได้รับผลกระทบจากคำสั่งล่าสุด"""
        return self._cur.rowcount

    def close(self):
        """ปิดการใช้งาน Cursor"""
        try:
            self._cur.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PgConnectionWrapper:
    """
    คลาส Wrapper สำหรับ Connection ของ PostgreSQL
    ทำหน้าที่จำลอง Connection API ของ SQLite (เช่น sqlite3.Connection)
    
    ฟีเจอร์เด่น:
      - ส่งคืน DictCursor (สามารถเข้าถึงคอลัมน์ด้วยชื่อ เช่น row['username'] ได้เหมือน sqlite3.Row)
      - รองรับคำสั่ง commit(), rollback(), close()
      - รองรับ Connection Pooling คืน connection กลับเข้า pool ทันทีเมื่อเสร็จสิ้น
      - รองรับ context manager (`with get_db() as conn:`) พร้อม auto-commit เมื่อไม่มี error และ rollback เมื่อเกิดข้อผิดพลาด
    """
    def __init__(self, raw_conn, pool=None):
        self._raw_conn = raw_conn
        self._pool = pool
        self.row_factory = None
        self._closed = False

    def cursor(self):
        """สร้าง Cursor จำลองที่เข้าถึงข้อมูลด้วยชื่อฟิลด์ได้"""
        if HAS_PSYCOPG2:
            return PgCursorWrapper(self._raw_conn.cursor(cursor_factory=DictCursor), self)
        else:
            return PgCursorWrapper(self._raw_conn.cursor(), self)

    def execute(self, sql, params=None):
        """รันคำสั่ง SQL ผ่าน Cursor อัตโนมัติ สะดวกเหมือน sqlite3.execute()"""
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        """ยืนยันการบันทึกข้อมูล (Commit Transaction)"""
        try:
            self._raw_conn.commit()
        except Exception:
            pass

    def rollback(self):
        """ยกเลิกการเปลี่ยนแปลงข้อมูล (Rollback Transaction)"""
        try:
            self._raw_conn.rollback()
        except Exception:
            pass

    def close(self):
        """ปิดหรือส่งคืนการเชื่อมต่อไปยัง Connection Pool"""
        if not self._closed:
            self._closed = True
            try:
                if self._pool:
                    self._pool.putconn(self._raw_conn)
                else:
                    self._raw_conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()


# ============================================================
# 3. ฟังก์ชันจัดการการเชื่อมต่อและการสำรองข้อมูล (Connection Factory & Backup)
# ============================================================

_pg_pool = None

def get_pg_pool():
    """
    สร้างหรือส่งคืน ThreadedConnectionPool สำหรับ PostgreSQL (Supabase)
    ช่วยให้ไม่ต้องทำ TCP Handshake และ SSL Handshake ใหม่ทุกครั้งที่คิวรี
    ลด Latency จาก ~250ms เหลือ <1ms ต่อคำขอ
    """
    global _pg_pool
    if _pg_pool is None and IS_POSTGRES and HAS_PSYCOPG2:
        try:
            import psycopg2.pool
            clean_url = DATABASE_URL
            if clean_url.startswith("postgres://"):
                clean_url = clean_url.replace("postgres://", "postgresql://", 1)
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=clean_url,
                connect_timeout=10
            )
        except Exception as e:
            print(f"[!] Warning: Failed to initialize ThreadedConnectionPool: {e}")
            _pg_pool = None
    return _pg_pool


def backup_database():
    """
    สำรองไฟล์ฐานข้อมูล worklog.db (เฉพาะระบบที่ใช้ SQLite)
    
    การทำงาน:
      - ทำงานเฉพาะโหมด Local SQLite (หากเป็น PostgreSQL จะข้ามทันที)
      - คัดลอกไฟล์ worklog.db ไปไว้ในโฟลเดอร์ backups/ พร้อมแนบ Timestamp
      - ทำการหมุนเวียนไฟล์สำรอง (Retention Policy): เก็บไว้สูงสุด 10 ไฟล์ล่าสุด 
        เพื่อป้องกันไม่ให้ขนาดโฟลเดอร์บวม
    """
    if IS_POSTGRES:
        return
    try:
        if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
            return
        if not os.access(os.path.dirname(os.path.abspath(__file__)), os.W_OK):
            return
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(backup_dir, f"worklog_backup_{timestamp}.db")
        shutil.copy2(DB_PATH, backup_file)
        
        # จัดการพื้นที่: เก็บสำรองย้อนหลัง 10 ไฟล์ล่าสุด
        existing_backups = sorted(glob.glob(os.path.join(backup_dir, "worklog_backup_*.db")))
        while len(existing_backups) > 10:
            try:
                os.remove(existing_backups.pop(0))
            except Exception:
                pass
    except Exception as e:
        print(f"[!] Warning: Auto database backup failed: {e}")


def get_db():
    """
    Connection Factory: เปิดและส่งคืน Connection สำหรับใช้งานฐานข้อมูล
    
    ลำดับการเชื่อมต่อ:
      1. หากเปิดโหมด PostgreSQL (IS_POSTGRES=True):
         - ดึง Connection จาก ThreadedConnectionPool ก่อน (Reuse connection, latency <1ms)
         - พร้อมตรวจสอบความพร้อมใช้งาน (Liveness Health Check)
         - หากล้มเหลว จะสลับไปใช้ pg8000 หรือ direct connect อัตโนมัติ
         - ส่งคืน Connection ผ่าน PgConnectionWrapper
      2. หากไม่ได้เปิดโหมด PostgreSQL (Local SQLite):
         - เชื่อมต่อไฟล์ SQLite ในเครื่อง (DB_PATH)
         - บังคับใช้ WAL Mode (Write-Ahead Logging) เพื่อการเขียนที่รวดเร็วระดับ Sub-millisecond
         - กำหนด row_factory = sqlite3.Row เพื่อให้เข้าถึงคอลัมน์ด้วยชื่อฟิลด์ได้
         
    Returns:
      PgConnectionWrapper หรือ sqlite3.Connection
    """
    if IS_POSTGRES:
        clean_url = DATABASE_URL
        if clean_url.startswith("postgres://"):
            clean_url = clean_url.replace("postgres://", "postgresql://", 1)
        
        last_error = None
        # ลำดับที่ 1: พยายามใช้ ThreadedConnectionPool ผ่าน psycopg2
        if HAS_PSYCOPG2:
            pool = get_pg_pool()
            if pool:
                try:
                    raw_conn = pool.getconn()
                    # ตรวจสอบสถานะการเชื่อมต่อก่อนส่งคืน
                    is_alive = False
                    if not raw_conn.closed:
                        try:
                            with raw_conn.cursor() as test_cur:
                                test_cur.execute("SELECT 1")
                            is_alive = True
                        except Exception:
                            is_alive = False
                    if not is_alive:
                        try:
                            pool.putconn(raw_conn, close=True)
                        except Exception:
                            pass
                        raw_conn = pool.getconn()
                    return PgConnectionWrapper(raw_conn, pool=pool)
                except Exception as e:
                    last_error = e

            # Fallback หาก pool มีปัญหาให้ต่อตรง
            try:
                raw_conn = psycopg2.connect(clean_url, connect_timeout=10)
                return PgConnectionWrapper(raw_conn)
            except Exception as e:
                last_error = e

        # ลำดับที่ 2: Fallback ด้วย pg8000
        if HAS_PG8000:
            try:
                import urllib.parse
                p = urllib.parse.urlparse(clean_url)
                raw_conn = pg8000.dbapi.connect(
                    user=p.username,
                    password=p.password,
                    host=p.hostname,
                    port=p.port or 5432,
                    database=p.path.lstrip("/"),
                    ssl_context=True,
                    timeout=10
                )
                return PgConnectionWrapper(raw_conn)
            except Exception as e:
                last_error = e

        if last_error:
            raise last_error

    # SQLite Local (High-Performance WAL Mode)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
    except Exception:
        pass
    return conn


def hash_pin(pin: str) -> str:
    """แฮชรหัส PIN ด้วย SHA-256 ป้องกันการอ่านค่าโดยตรง"""
    return hashlib.sha256(str(pin).strip().encode("utf-8")).hexdigest()


def _init_postgres_db():
    """Initializes and verifies PostgreSQL (Supabase) tables and initial seeds"""
    with get_db() as conn:
        cursor = conn.cursor()
        # Fast check: หากตารางหลัก (tasks) มีอยู่แล้ว ให้ข้าม DDL ทั้งหมดเพื่อลด cold start latency บน Vercel
        try:
            cursor.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'tasks' LIMIT 1")
            if cursor.fetchone():
                return
        except Exception:
            pass
        # 1. Tasks
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                date VARCHAR(50) NOT NULL,
                title TEXT NOT NULL,
                category VARCHAR(50) DEFAULT 'ทั่วไป',
                priority VARCHAR(50) DEFAULT 'normal',
                status VARCHAR(50) DEFAULT 'todo',
                notes TEXT DEFAULT '',
                time_spent VARCHAR(50) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                creator_name TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_date ON tasks(user_id, date)")

        # 2. Settings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cursor.execute("SELECT value FROM settings WHERE key = 'pin'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings (key, value) VALUES ('pin', %s)", (hash_pin("1234"),))
            
        cursor.execute("SELECT value FROM settings WHERE key = 'user_name'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings (key, value) VALUES ('user_name', 'ผู้ปฏิบัติงาน')")

        cursor.execute("SELECT value FROM settings WHERE key = 'org_name'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings (key, value) VALUES ('org_name', 'บันทึกการปฏิบัติงานประจำวัน')")

        # 3. Users
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name VARCHAR(200) NOT NULL,
                department_id INTEGER,
                role VARCHAR(50) NOT NULL DEFAULT 'user',
                status VARCHAR(50) NOT NULL DEFAULT 'pending',
                request_reason TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                approved_by VARCHAR(100),
                last_login TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

        # Master Admin It / *Admin55*
        cursor.execute("SELECT id, role, status FROM users WHERE LOWER(username) = 'it'")
        admin_row = cursor.fetchone()
        if not admin_row:
            cursor.execute("""
                INSERT INTO users (username, password_hash, full_name, role, status, approved_at, approved_by)
                VALUES ('It', %s, 'ผู้ดูแลระบบ IT (Super Admin)', 'admin', 'active', CURRENT_TIMESTAMP, 'SYSTEM')
            """, (generate_password_hash("*Admin55*"),))
        else:
            cursor.execute("UPDATE users SET role = 'admin', status = 'active' WHERE id = %s", (admin_row["id"],))

        # 4. Departments
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                code VARCHAR(50) UNIQUE,
                floor_room VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 5. Assets
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id SERIAL PRIMARY KEY,
                asset_tag VARCHAR(100) UNIQUE,
                category VARCHAR(50) NOT NULL,
                brand_model VARCHAR(200) NOT NULL,
                serial_number VARCHAR(100),
                department_id INTEGER,
                assigned_user VARCHAR(200),
                workstation_label VARCHAR(100),
                parent_asset_id INTEGER,
                status VARCHAR(50) DEFAULT 'in_use',
                specs TEXT,
                ip_address VARCHAR(50),
                mac_address VARCHAR(50),
                purchase_date VARCHAR(50),
                warranty_expire VARCHAR(50),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (department_id) REFERENCES departments (id) ON DELETE SET NULL,
                FOREIGN KEY (parent_asset_id) REFERENCES assets (id) ON DELETE SET NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_dept ON assets(department_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_parent ON assets(parent_asset_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_tag ON assets(asset_tag)")
        conn.commit()


def init_db():
    """สร้างตารางและค่าเริ่มต้นของฐานข้อมูล พร้อมสำรองข้อมูลอัตโนมัติ"""
    if IS_POSTGRES:
        _init_postgres_db()
        return
    backup_database()
    with get_db() as conn:
        cursor = conn.cursor()
        
        # ตารางรายการงาน (Tasks)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT DEFAULT 'ทั่วไป',
                priority TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'todo',
                notes TEXT DEFAULT '',
                time_spent TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(date)")
        
        # ตารางการตั้งค่าระบบ (Settings)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # ตรวจสอบและตั้งค่า PIN เริ่มต้น (1234)
        cursor.execute("SELECT value FROM settings WHERE key = 'pin'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings (key, value) VALUES ('pin', ?)", (hash_pin("1234"),))
            
        cursor.execute("SELECT value FROM settings WHERE key = 'user_name'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings (key, value) VALUES ('user_name', 'ผู้ปฏิบัติงาน')")

        cursor.execute("SELECT value FROM settings WHERE key = 'org_name'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings (key, value) VALUES ('org_name', 'บันทึกการปฏิบัติงานประจำวัน')")

        # ============================================================
        # Users & Authentication Table (Master Admin: It / *Admin55*)
        # ============================================================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE COLLATE NOCASE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                department_id INTEGER,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending',
                request_reason TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                approved_by TEXT,
                last_login TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

        # ตรวจสอบและสร้างบัญชี Master Admin เริ่มต้น (Username: It, Password: *Admin55*)
        cursor.execute("SELECT id, role, status FROM users WHERE username = 'It' COLLATE NOCASE")
        admin_row = cursor.fetchone()
        if not admin_row:
            cursor.execute("""
                INSERT INTO users (username, password_hash, full_name, role, status, approved_at, approved_by)
                VALUES ('It', ?, 'ผู้ดูแลระบบ IT (Super Admin)', 'admin', 'active', CURRENT_TIMESTAMP, 'SYSTEM')
            """, (generate_password_hash("*Admin55*"),))
        else:
            # รับประกันว่าบัญชี It มีสิทธิ์ admin และสถานะ active เสมอ
            cursor.execute("UPDATE users SET role = 'admin', status = 'active' WHERE id = ?", (admin_row["id"],))

        # ============================================================
        # Task Migration: Ensure user_id and creator_name exist
        # ============================================================
        cursor.execute("PRAGMA table_info(tasks)")
        task_columns = [row["name"] for row in cursor.fetchall()]
        if "user_id" not in task_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER REFERENCES users(id)")
        if "creator_name" not in task_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN creator_name TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_date ON tasks(user_id, date)")

        # Backfill existing legacy tasks where user_id IS NULL to Master Admin 'It'
        cursor.execute("""
            UPDATE tasks 
            SET user_id = (SELECT id FROM users WHERE username = 'It' COLLATE NOCASE),
                creator_name = 'ผู้ดูแลระบบ IT (Super Admin)'
            WHERE user_id IS NULL
        """)

        # ============================================================
        # IT Asset Management Tables (Phase 1)
        # ============================================================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT UNIQUE,
                floor_room TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_tag TEXT UNIQUE,
                category TEXT NOT NULL,
                brand_model TEXT NOT NULL,
                serial_number TEXT,
                department_id INTEGER,
                assigned_user TEXT,
                workstation_label TEXT,
                parent_asset_id INTEGER,
                status TEXT DEFAULT 'in_use',
                specs TEXT,
                ip_address TEXT,
                mac_address TEXT,
                purchase_date TEXT,
                warranty_expire TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (department_id) REFERENCES departments (id) ON DELETE SET NULL,
                FOREIGN KEY (parent_asset_id) REFERENCES assets (id) ON DELETE SET NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_dept ON assets(department_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_parent ON assets(parent_asset_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_assets_tag ON assets(asset_tag)")

        # ค่าเริ่มต้นแผนก (Default Departments)
        cursor.execute("SELECT COUNT(*) FROM departments")
        if cursor.fetchone()[0] == 0:
            default_depts = [
                ('แผนกไอทีและระบบสารสนเทศ', 'IT', 'ชั้น 2 โซนระบบ'),
                ('แผนกบัญชีและการเงิน', 'ACC', 'ชั้น 3'),
                ('แผนกทรัพยากรบุคคล', 'HR', 'ชั้น 3'),
                ('แผนกการตลาดและงานขาย', 'MKT', 'ชั้น 4'),
                ('ส่วนกลางและสำนักงาน', 'CENTRAL', 'ชั้น 1')
            ]
            cursor.executemany("INSERT INTO departments (name, code, floor_room) VALUES (?, ?, ?)", default_depts)

        # ข้อมูลตัวอย่างเริ่มต้น (Seed Initial Workstation if empty)
        cursor.execute("SELECT COUNT(*) FROM assets")
        if cursor.fetchone()[0] == 0:
            # ดึง id แผนกบัญชี และ ไอที
            cursor.execute("SELECT id FROM departments WHERE code = 'ACC'")
            acc_dept = cursor.fetchone()
            acc_id = acc_dept[0] if acc_dept else 1

            cursor.execute("SELECT id FROM departments WHERE code = 'IT'")
            it_dept = cursor.fetchone()
            it_id = it_dept[0] if it_dept else 1

            # 1. เครื่องหลัก คุณสมชาย (โน้ตบุ๊ก)
            cursor.execute("""
                INSERT INTO assets (asset_tag, category, brand_model, serial_number, department_id, assigned_user, workstation_label, status, specs, ip_address)
                VALUES ('IT-NB-001', 'laptop', 'Lenovo ThinkPad T14 Gen 3', 'PF-2ABC123', ?, 'สมชาย วงศ์สวัสดิ์', 'โต๊ะ ACC-01', 'in_use', 'Core i7-1260P / 16GB RAM / 512GB NVMe SSD / Windows 11 Pro', '192.168.1.45')
            """, (acc_id,))
            parent_id = cursor.lastrowid

            # 2. จอพ่วงของคุณสมชาย
            cursor.execute("""
                INSERT INTO assets (asset_tag, category, brand_model, serial_number, department_id, assigned_user, workstation_label, parent_asset_id, status, specs)
                VALUES ('IT-MON-001', 'monitor', 'Dell 24" P2419H IPS', 'CN-098XYZ', ?, 'สมชาย วงศ์สวัสดิ์', 'โต๊ะ ACC-01', ?, 'in_use', 'Full HD 1080p / HDMI & DisplayPort / ขาปรับระดับได้')
            """, (acc_id, parent_id))

            # 3. UPS สำรองไฟของคุณสมชาย
            cursor.execute("""
                INSERT INTO assets (asset_tag, category, brand_model, serial_number, department_id, assigned_user, workstation_label, parent_asset_id, status, specs)
                VALUES ('IT-UPS-001', 'ups', 'APC Back-UPS 800VA / 480W', '4B1948X99', ?, 'สมชาย วงศ์สวัสดิ์', 'โต๊ะ ACC-01', ?, 'in_use', '800VA สำรองไฟ 15-20 นาที')
            """, (acc_id, parent_id))

            # 4. เครื่องสำรองในคลังไอที (Spare)
            cursor.execute("""
                INSERT INTO assets (asset_tag, category, brand_model, serial_number, department_id, assigned_user, workstation_label, status, specs)
                VALUES ('IT-PC-002', 'pc', 'Dell OptiPlex 7090 Tower', 'DL-8899AA', ?, 'เครื่องสำรอง', 'ห้องเซิร์ฟเวอร์ IT', 'spare', 'Core i5-11500 / 16GB / 512GB SSD / Intel UHD 750')
            """, (it_id,))
            
        conn.commit()


# ============================================================
# Task Operations (CRUD with Multi-User Data Isolation)
# ============================================================

def get_tasks(user_id=None, date=None, start_date=None, end_date=None, status=None):
    """ดึงรายการงานตามเงื่อนไข (วันที่ หรือ ช่วงวันที่ และกรองตามผู้ใช้)"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT t.*, 
                   u.username AS creator_username, 
                   COALESCE(u.full_name, t.creator_name, 'ไม่ระบุผู้ใช้') AS creator_display_name,
                   d.name AS creator_department_name
            FROM tasks t
            LEFT JOIN users u ON t.user_id = u.id
            LEFT JOIN departments d ON u.department_id = d.id
            WHERE 1=1
        """
        params = []
        
        # กรองผู้ใช้: หากระบุ user_id และไม่ใช่ 'all' ให้ดึงเฉพาะของคนนั้น
        if user_id is not None and str(user_id).strip().lower() not in ("all", "", "none"):
            query += " AND t.user_id = ?"
            params.append(int(user_id))
            
        if date:
            query += " AND t.date = ?"
            params.append(date)
        elif start_date and end_date:
            query += " AND t.date >= ? AND t.date <= ?"
            params.extend([start_date, end_date])
            
        if status:
            query += " AND t.status = ?"
            params.append(status)
            
        query += " ORDER BY CASE t.priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END, t.id ASC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_task(task_id: int):
    """ดึงข้อมูลงานตาม ID พร้อมข้อมูลผู้สร้าง"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.*, 
                   u.username AS creator_username, 
                   COALESCE(u.full_name, t.creator_name, 'ไม่ระบุผู้ใช้') AS creator_display_name,
                   d.name AS creator_department_name
            FROM tasks t
            LEFT JOIN users u ON t.user_id = u.id
            LEFT JOIN departments d ON u.department_id = d.id
            WHERE t.id = ?
        """, (task_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def create_task(date: str, title: str, category="ทั่วไป", priority="normal", status="todo", notes="", time_spent="", user_id=None, creator_name=None):
    """สร้างงานใหม่พร้อมบันทึก user_id และชื่อผู้สร้าง"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tasks (date, title, category, priority, status, notes, time_spent, user_id, creator_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, title.strip(), category, priority, status, notes.strip(), time_spent.strip(), user_id, creator_name, now, now))
        conn.commit()
        return cursor.lastrowid


def update_task(task_id: int, requesting_user_id=None, is_admin=False, **kwargs):
    """อัปเดตข้อมูลงาน พร้อมตรวจสอบสิทธิ์ความเป็นเจ้าของ (Admin แก้ได้ทุกคน, User แก้ได้เฉพาะของตนเอง)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        if not task:
            return False
            
        # ตรวจสอบสิทธิ์: ถ้าไม่ใช่ admin ต้องเป็นเจ้าของงาน
        if not is_admin and requesting_user_id is not None:
            if task["user_id"] is not None and task["user_id"] != int(requesting_user_id):
                return False

        allowed_keys = ["date", "title", "category", "priority", "status", "notes", "time_spent"]
        fields = []
        values = []
        
        for k, v in kwargs.items():
            if k in allowed_keys and v is not None:
                fields.append(f"{k} = ?")
                values.append(v.strip() if isinstance(v, str) else v)
                
        if not fields:
            return False
            
        fields.append("updated_at = ?")
        values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        values.append(task_id)
        
        cursor.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        return cursor.rowcount > 0


def delete_task(task_id: int, requesting_user_id=None, is_admin=False):
    """ลบงาน พร้อมตรวจสอบสิทธิ์ความเป็นเจ้าของ"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        if not task:
            return False
            
        if not is_admin and requesting_user_id is not None:
            if task["user_id"] is not None and task["user_id"] != int(requesting_user_id):
                return False

        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cursor.rowcount > 0


def toggle_task(task_id: int, requesting_user_id=None, is_admin=False):
    """สลับสถานะ เสร็จสิ้น (done) <-> ยังไม่เสร็จ (todo) พร้อมตรวจสอบสิทธิ์"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, status FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        if not task:
            return None
            
        if not is_admin and requesting_user_id is not None:
            if task["user_id"] is not None and task["user_id"] != int(requesting_user_id):
                return None

        current = task["status"]
        new_status = "todo" if current == "done" else "done"
        cursor.execute("""
            UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?
        """, (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
        conn.commit()
        return new_status


def get_daily_stats(date: str, user_id=None):
    """คำนวณสถิติภาพรวมประจำวัน (แยกตาม user_id หรือภาพรวมทุกคน)"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN status = 'todo' THEN 1 ELSE 0 END) as todo
            FROM tasks WHERE date = ?
        """
        params = [date]
        if user_id is not None and str(user_id).strip().lower() not in ("all", "", "none"):
            query += " AND user_id = ?"
            params.append(int(user_id))
            
        cursor.execute(query, params)
        row = cursor.fetchone()
        total = row["total"] or 0
        done = row["done"] or 0
        in_progress = row["in_progress"] or 0
        todo = row["todo"] or 0
        percent = round((done / total * 100)) if total > 0 else 0
        return {
            "total": total,
            "done": done,
            "in_progress": in_progress,
            "todo": todo,
            "percent": percent
        }


def get_dates_with_tasks(year_month: str, user_id=None):
    """ดึงรายการวันที่ในเดือนนั้นๆ ที่มีงานอยู่ สำหรับแสดงจุดบนปฏิทิน"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT date, COUNT(*) as count,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done_count
            FROM tasks
            WHERE date LIKE ?
        """
        params = [f"{year_month}%"]
        if user_id is not None and str(user_id).strip().lower() not in ("all", "", "none"):
            query += " AND user_id = ?"
            params.append(int(user_id))
            
        query += " GROUP BY date"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return {row["date"]: {"total": row["count"], "done": row["done_count"]} for row in rows}


def get_month_tasks_data(year_month: str, user_id=None):
    """ดึงรายการงานทั้งหมดและสรุปสถิติรายวันในเดือนนั้นๆ สำหรับแสดงผลบนปฏิทิน (แยกรายคน หรือภาพรวม)"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT t.id, t.date, t.title, t.category, t.priority, t.status, t.notes, t.time_spent,
                   t.user_id, COALESCE(u.full_name, t.creator_name, 'ไม่ระบุผู้ใช้') AS creator_display_name
            FROM tasks t
            LEFT JOIN users u ON t.user_id = u.id
            WHERE t.date LIKE ?
        """
        params = [f"{year_month}%"]
        if user_id is not None and str(user_id).strip().lower() not in ("all", "", "none"):
            query += " AND t.user_id = ?"
            params.append(int(user_id))
            
        query += " ORDER BY t.date ASC, CASE t.priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END, t.id ASC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        days = {}
        for row in rows:
            d = dict(row)
            dt = d["date"]
            if dt not in days:
                days[dt] = {"total": 0, "done": 0, "todo": 0, "tasks": []}
            days[dt]["total"] += 1
            if d["status"] == "done":
                days[dt]["done"] += 1
            else:
                days[dt]["todo"] += 1
            days[dt]["tasks"].append(d)
        return days


def get_active_task_users():
    """ดึงรายชื่อผู้ใช้ที่สถานะ active สำหรับ Dropdown ตัวกรองงานของ Admin"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.full_name, u.role, d.name AS department_name
            FROM users u
            LEFT JOIN departments d ON u.department_id = d.id
            WHERE u.status = 'active'
            ORDER BY CASE WHEN u.role = 'admin' THEN 0 ELSE 1 END, u.full_name ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


# ============================================================
# User Authentication & Management (Master Admin: It / *Admin55*)
# ============================================================

def authenticate_user(username: str, password: str) -> tuple[bool, dict | None, str]:
    """ตรวจสอบการเข้าสู่ระบบด้วย Username & Password"""
    u = str(username or "").strip()
    p = str(password or "")
    if not u or not p:
        return False, None, "กรุณาระบุชื่อผู้ใช้และรหัสผ่าน"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.password_hash, u.full_name, u.full_name AS display_name,
                   u.role, u.status, u.department_id, d.name AS department_name
            FROM users u
            LEFT JOIN departments d ON u.department_id = d.id
            WHERE u.username = ? COLLATE NOCASE
        """, (u,))
        user = cursor.fetchone()

        if not user:
            return False, None, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"

        user_dict = dict(user)
        # ตรวจสอบรหัสผ่าน
        if not check_password_hash(user_dict["password_hash"], p):
            return False, None, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"

        # ตรวจสอบสถานะการอนุมัติ
        if user_dict["status"] == "pending":
            return False, None, "บัญชีของคุณอยู่ระหว่างรอผู้ดูแลระบบ (IT) ยืนยันการอนุมัติ"
        elif user_dict["status"] == "rejected":
            return False, None, "คำขอเปิดบัญชีนี้ไม่ผ่านการอนุมัติ กรุณาติดต่อฝ่าย IT"
        elif user_dict["status"] == "suspended":
            return False, None, "บัญชีนี้ถูกระงับการใช้งานชั่วคราว กรุณาติดต่อฝ่าย IT"
        elif user_dict["status"] != "active":
            return False, None, "สถานะบัญชีไม่พร้อมใช้งาน"

        # อัปเดตเวลาเข้าสู่ระบบล่าสุด
        try:
            cursor.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user_dict["id"],))
            conn.commit()
        except Exception:
            pass

        # ปลอดภัย: ไม่ส่ง password_hash กลับไป
        user_dict.pop("password_hash", None)
        return True, user_dict, "เข้าสู่ระบบสำเร็จ"


def validate_password_strength(password: str) -> tuple[bool, str]:
    """ตรวจสอบความปลอดภัยของรหัสผ่าน: อย่างน้อย 8 ตัวอักษร และมีทั้งตัวอักษรและตัวเลข"""
    p = str(password or "")
    if len(p) < 8:
        return False, "รหัสผ่านต้องมีความยาวอย่างน้อย 8 ตัวอักษร"
    has_letter = any(c.isalpha() for c in p)
    has_digit = any(c.isdigit() for c in p)
    if not (has_letter and has_digit):
        return False, "รหัสผ่านต้องประกอบด้วยตัวอักษรและตัวเลขผสมกัน เพื่อความปลอดภัย"
    return True, "รหัสผ่านปลอดภัย"


def register_user(username: str, password: str, display_name: str, department_id=None, request_reason: str = "") -> tuple[bool, str]:
    """ผู้ใช้ทั่วไปขอเปิดบัญชีใหม่ (สถานะเริ่มต้น: pending รออนุมัติ)"""
    u = str(username or "").strip()
    p = str(password or "")
    name = str(display_name or "").strip()
    reason = str(request_reason or "").strip()

    if len(u) < 2:
        return False, "ชื่อผู้ใช้ต้องมีความยาวอย่างน้อย 2 ตัวอักษร"
    
    valid, msg = validate_password_strength(p)
    if not valid:
        return False, msg

    if not name:
        name = u

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (u,))
        if cursor.fetchone():
            return False, f"ชื่อผู้ใช้ '{u}' มีอยู่ในระบบแล้ว กรุณาเลือกชื่ออื่น"

        dept_id = int(department_id) if department_id and str(department_id).isdigit() else None
        cursor.execute("""
            INSERT INTO users (username, password_hash, full_name, department_id, role, status, request_reason)
            VALUES (?, ?, ?, ?, 'user', 'pending', ?)
        """, (u, generate_password_hash(p), name, dept_id, reason))
        conn.commit()
        return True, "ส่งคำขอเปิดบัญชีเรียบร้อยแล้ว กรุณารอผู้ดูแลระบบ (IT) อนุมัติการเข้าใช้งาน"


def get_all_users() -> list[dict]:
    """ดึงรายชื่อผู้ใช้ทั้งหมดในระบบ (สำหรับบัญชี Master Admin)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.full_name, u.full_name AS display_name, u.role, u.status, u.department_id,
                   u.request_reason, d.name AS department_name, u.created_at, u.approved_at, u.approved_by, u.last_login
            FROM users u
            LEFT JOIN departments d ON u.department_id = d.id
            ORDER BY CASE WHEN u.status = 'pending' THEN 0 ELSE 1 END, u.created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def get_user_by_id(user_id: int) -> dict | None:
    """ดึงข้อมูลผู้ใช้ตาม ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.full_name, u.full_name AS display_name, u.role, u.status, u.department_id,
                   u.request_reason, d.name AS department_name, u.created_at, u.approved_at, u.approved_by, u.last_login
            FROM users u
            LEFT JOIN departments d ON u.department_id = d.id
            WHERE u.id = ?
        """, (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def approve_user(user_id: int, approved_by: str = "It", role: str = "user") -> tuple[bool, str]:
    """อนุมัติบัญชีผู้ใช้ใหม่"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, status FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, "ไม่พบข้อมูลผู้ใช้นี้"

        assigned_role = 'admin' if role == 'admin' else 'user'
        cursor.execute("""
            UPDATE users
            SET status = 'active', role = ?, approved_at = CURRENT_TIMESTAMP, approved_by = ?
            WHERE id = ?
        """, (assigned_role, approved_by, user_id))
        conn.commit()
        return True, f"อนุมัติบัญชี '{user['username']}' เรียบร้อยแล้ว"


def reject_user(user_id: int) -> tuple[bool, str]:
    """ปฏิเสธคำขอเปิดบัญชี (ลบออกจากระบบ)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, "ไม่พบข้อมูลผู้ใช้นี้"
        if user["username"].lower() == "it":
            return False, "ไม่อนุญาตให้ลบหรือปฏิเสธบัญชีผู้ดูแลระบบหลัก (It)"

        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return True, f"ปฏิเสธและลบคำขอของ '{user['username']}' เรียบร้อยแล้ว"


def change_user_status(user_id: int, status: str) -> tuple[bool, str]:
    """เปลี่ยนสถานะผู้ใช้ (active / suspended)"""
    if status not in ("active", "suspended", "pending"):
        return False, "สถานะไม่ถูกต้อง"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, "ไม่พบข้อมูลผู้ใช้นี้"
        if user["username"].lower() == "it" and status != "active":
            return False, "ไม่อนุญาตให้ระงับบัญชีผู้ดูแลระบบหลัก (It)"

        cursor.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        conn.commit()
        label = "เปิดใช้งาน" if status == "active" else "ระงับการใช้งาน"
        return True, f"{label}บัญชี '{user['username']}' เรียบร้อยแล้ว"


def admin_create_user(username: str, password: str, display_name: str, department_id=None, role: str = "user") -> tuple[bool, str]:
    """ผู้ดูแลระบบสร้างบัญชีผู้ใช้ใหม่โดยตรง (สถานะ: active ทันที)"""
    u = str(username or "").strip()
    p = str(password or "")
    name = str(display_name or "").strip()

    if len(u) < 2:
        return False, "ชื่อผู้ใช้ต้องมีความยาวอย่างน้อย 2 ตัวอักษร"
    
    valid, msg = validate_password_strength(p)
    if not valid:
        return False, msg

    if not name:
        name = u

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (u,))
        if cursor.fetchone():
            return False, f"ชื่อผู้ใช้ '{u}' มีอยู่ในระบบแล้ว"

        dept_id = int(department_id) if department_id and str(department_id).isdigit() else None
        user_role = 'admin' if role == 'admin' else 'user'
        cursor.execute("""
            INSERT INTO users (username, password_hash, full_name, department_id, role, status, approved_at, approved_by)
            VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, 'It')
        """, (u, generate_password_hash(p), name, dept_id, user_role))
        conn.commit()
        return True, f"สร้างบัญชีผู้ใช้ '{u}' เรียบร้อยแล้ว"


def admin_reset_password(user_id: int, new_password: str) -> tuple[bool, str]:
    """ผู้ดูแลระบบรีเซ็ตรหัสผ่านให้ผู้ใช้"""
    p = str(new_password or "")
    valid, msg = validate_password_strength(p)
    if not valid:
        return False, msg

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, "ไม่พบข้อมูลผู้ใช้นี้"

        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(p), user_id))
        conn.commit()
        return True, f"รีเซ็ตรหัสผ่านให้บัญชี '{user['username']}' เรียบร้อยแล้ว"


def change_user_password(user_id: int, old_password: str, new_password: str) -> tuple[bool, str]:
    """ผู้ใช้งานเปลี่ยนรหัสผ่านของตนเอง (ตรวจสอบรหัสผ่านเดิม)"""
    old_p = str(old_password or "")
    new_p = str(new_password or "").strip()
    if not old_p or not new_p:
        return False, "กรุณากรอกรหัสผ่านเดิมและรหัสผ่านใหม่"
        
    valid, msg = validate_password_strength(new_p)
    if not valid:
        return False, msg

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, "ไม่พบข้อมูลผู้ใช้นี้"

        if not check_password_hash(user["password_hash"], old_p):
            return False, "รหัสผ่านปัจจุบันไม่ถูกต้อง"

        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_p), user_id))
        conn.commit()
        return True, "เปลี่ยนรหัสผ่านเรียบร้อยแล้ว"


def get_pending_users_count() -> int:
    """นับจำนวนบัญชีที่รอการอนุมัติ"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM users WHERE status = 'pending'")
        row = cursor.fetchone()
        return row["c"] if row else 0


# ============================================================
# Settings & Authentication (Legacy PIN & General Settings)
# ============================================================

def verify_pin(input_pin: str) -> bool:
    """ตรวจสอบรหัส PIN"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'pin'")
        row = cursor.fetchone()
        if not row:
            return str(input_pin).strip() == "1234"
        return row["value"] == hash_pin(input_pin)


def set_pin(old_pin: str, new_pin: str) -> tuple[bool, str]:
    """เปลี่ยนรหัส PIN ใหม่"""
    if not verify_pin(old_pin):
        return False, "รหัส PIN เดิมไม่ถูกต้อง"
    
    new_pin_clean = str(new_pin).strip()
    if len(new_pin_clean) < 4:
        return False, "รหัส PIN ต้องมีความยาวอย่างน้อย 4 หลัก"
        
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE settings SET value = ? WHERE key = 'pin'", (hash_pin(new_pin_clean),))
        conn.commit()
        return True, "เปลี่ยนรหัส PIN สำเร็จ"


def get_setting(key: str, default=""):
    """ดึงค่าการตั้งค่า"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    """บันทึกค่าการตั้งค่า"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value).strip()))
        conn.commit()


# ============================================================
# IT Asset Management (Departments & Assets) CRUD Operations
# ============================================================

def get_departments() -> list[dict]:
    """ดึงรายชื่อแผนกทั้งหมด พร้อมนับจำนวนอุปกรณ์ในแต่ละแผนก"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.*, 
                   COUNT(a.id) AS total_assets,
                   SUM(CASE WHEN a.status = 'in_use' THEN 1 ELSE 0 END) AS in_use_assets
            FROM departments d
            LEFT JOIN assets a ON d.id = a.department_id
            GROUP BY d.id
            ORDER BY d.id ASC
        """)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_department(dept_id: int):
    """ดึงข้อมูลแผนกตาม ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM departments WHERE id = ?", (dept_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def create_department(name: str, code: str = "", floor_room: str = "") -> int:
    """เพิ่มแผนกใหม่"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO departments (name, code, floor_room)
            VALUES (?, ?, ?)
        """, (name.strip(), code.strip().upper(), floor_room.strip()))
        conn.commit()
        return cursor.lastrowid


def update_department(dept_id: int, name: str, code: str = "", floor_room: str = "") -> bool:
    """แก้ไขข้อมูลแผนก"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE departments 
            SET name = ?, code = ?, floor_room = ?
            WHERE id = ?
        """, (name.strip(), code.strip().upper(), floor_room.strip(), dept_id))
        conn.commit()
        return cursor.rowcount > 0


def delete_department(dept_id: int) -> tuple[bool, str]:
    """ลบแผนก (หากไม่มีอุปกรณ์ผูกอยู่)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM assets WHERE department_id = ?", (dept_id,))
        count = cursor.fetchone()[0]
        if count > 0:
            return False, f"ไม่สามารถลบได้ เนื่องจากยังมีอุปกรณ์ผูกกับแผนกนี้อยู่ {count} รายการ"
            
        cursor.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
        conn.commit()
        return True, "ลบแผนกเรียบร้อย"


def generate_asset_tag(category: str) -> str:
    """สร้างรหัสทรัพย์สินอัตโนมัติตามประเภทอุปกรณ์ เช่น IT-PC-003, IT-NB-004"""
    prefix_map = {
        'laptop': 'IT-NB',
        'pc': 'IT-PC',
        'monitor': 'IT-MON',
        'ups': 'IT-UPS',
        'printer': 'IT-PRN',
        'network': 'IT-NET',
        'server': 'IT-SRV'
    }
    prefix = prefix_map.get(str(category).lower(), 'IT-DEV')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT asset_tag FROM assets WHERE asset_tag LIKE ? ORDER BY id DESC LIMIT 100", (f"{prefix}-%",))
        rows = cursor.fetchall()
        
        max_num = 0
        for r in rows:
            tag = r[0]
            try:
                parts = tag.split('-')
                if len(parts) >= 3 and parts[-1].isdigit():
                    num = int(parts[-1])
                    if num > max_num:
                        max_num = num
            except Exception:
                continue
                
        new_num = max_num + 1
        return f"{prefix}-{new_num:03d}"


def get_assets(dept_id=None, status=None, category=None, search=None, parent_id=None) -> list[dict]:
    """ดึงรายการอุปกรณ์ทั้งหมดตามเงื่อนไข ค้นหา และตัวกรอง"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT a.*, 
                   d.name AS department_name, 
                   d.code AS department_code,
                   p.brand_model AS parent_model,
                   p.asset_tag AS parent_tag
            FROM assets a
            LEFT JOIN departments d ON a.department_id = d.id
            LEFT JOIN assets p ON a.parent_asset_id = p.id
            WHERE 1=1
        """
        params = []
        
        if dept_id:
            query += " AND a.department_id = ?"
            params.append(dept_id)
            
        if status:
            query += " AND a.status = ?"
            params.append(status)
            
        if category:
            query += " AND a.category = ?"
            params.append(category)
            
        if parent_id is not None:
            if parent_id == 0:
                query += " AND (a.parent_asset_id IS NULL OR a.parent_asset_id = 0)"
            else:
                query += " AND a.parent_asset_id = ?"
                params.append(parent_id)
                
        if search:
            search_str = f"%{str(search).strip()}%"
            query += """ AND (
                a.asset_tag LIKE ? OR 
                a.brand_model LIKE ? OR 
                a.serial_number LIKE ? OR 
                a.assigned_user LIKE ? OR 
                a.workstation_label LIKE ? OR 
                a.specs LIKE ? OR 
                a.ip_address LIKE ?
            )"""
            params.extend([search_str] * 7)
            
        query += " ORDER BY a.department_id ASC, CASE WHEN a.parent_asset_id IS NULL THEN 0 ELSE 1 END, a.id DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_asset(asset_id: int):
    """ดึงข้อมูลอุปกรณ์ 1 ชิ้น พร้อมข้อมูลแผนกและเครื่องหลัก (ถ้ามี)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT a.*, 
                   d.name AS department_name, 
                   d.code AS department_code,
                   p.brand_model AS parent_model,
                   p.asset_tag AS parent_tag
            FROM assets a
            LEFT JOIN departments d ON a.department_id = d.id
            LEFT JOIN assets p ON a.parent_asset_id = p.id
            WHERE a.id = ?
        """, (asset_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_parent_candidates(exclude_id: int = None) -> list[dict]:
    """ดึงรายชื่ออุปกรณ์หลักที่สามารถนำอุปกรณ์อื่นมาพ่วงได้ (PC, Laptop, Server)"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT a.id, a.asset_tag, a.category, a.brand_model, a.assigned_user, a.workstation_label, d.name AS department_name
            FROM assets a
            LEFT JOIN departments d ON a.department_id = d.id
            WHERE a.category IN ('pc', 'laptop', 'server')
        """
        params = []
        if exclude_id:
            query += " AND a.id != ?"
            params.append(exclude_id)
            
        query += " ORDER BY a.id ASC"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def create_asset(data: dict) -> int:
    """เพิ่มอุปกรณ์ใหม่ลงในระบบ"""
    category = data.get("category", "pc").strip().lower()
    asset_tag = data.get("asset_tag", "").strip()
    if not asset_tag:
        asset_tag = generate_asset_tag(category)
        
    parent_id = data.get("parent_asset_id")
    if parent_id in ("", None, "0", 0):
        parent_id = None
    else:
        parent_id = int(parent_id)

    dept_id = data.get("department_id")
    if dept_id in ("", None, "0", 0):
        dept_id = None
    else:
        dept_id = int(dept_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO assets (
                asset_tag, category, brand_model, serial_number, department_id,
                assigned_user, workstation_label, parent_asset_id, status,
                specs, ip_address, mac_address, purchase_date, warranty_expire, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asset_tag,
            category,
            data.get("brand_model", "").strip(),
            data.get("serial_number", "").strip(),
            dept_id,
            data.get("assigned_user", "").strip(),
            data.get("workstation_label", "").strip(),
            parent_id,
            data.get("status", "in_use").strip(),
            data.get("specs", "").strip(),
            data.get("ip_address", "").strip(),
            data.get("mac_address", "").strip(),
            data.get("purchase_date", "").strip(),
            data.get("warranty_expire", "").strip(),
            data.get("notes", "").strip()
        ))
        conn.commit()
        return cursor.lastrowid


def update_asset(asset_id: int, data: dict) -> bool:
    """แก้ไขข้อมูลอุปกรณ์"""
    parent_id = data.get("parent_asset_id")
    if parent_id in ("", None, "0", 0):
        parent_id = None
    else:
        parent_id = int(parent_id)
        if parent_id == asset_id:
            parent_id = None  # ป้องกัน circular parent

    dept_id = data.get("department_id")
    if dept_id in ("", None, "0", 0):
        dept_id = None
    else:
        dept_id = int(dept_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE assets SET
                asset_tag = ?,
                category = ?,
                brand_model = ?,
                serial_number = ?,
                department_id = ?,
                assigned_user = ?,
                workstation_label = ?,
                parent_asset_id = ?,
                status = ?,
                specs = ?,
                ip_address = ?,
                mac_address = ?,
                purchase_date = ?,
                warranty_expire = ?,
                notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            data.get("asset_tag", "").strip(),
            data.get("category", "pc").strip().lower(),
            data.get("brand_model", "").strip(),
            data.get("serial_number", "").strip(),
            dept_id,
            data.get("assigned_user", "").strip(),
            data.get("workstation_label", "").strip(),
            parent_id,
            data.get("status", "in_use").strip(),
            data.get("specs", "").strip(),
            data.get("ip_address", "").strip(),
            data.get("mac_address", "").strip(),
            data.get("purchase_date", "").strip(),
            data.get("warranty_expire", "").strip(),
            data.get("notes", "").strip(),
            asset_id
        ))
        conn.commit()
        return cursor.rowcount > 0


def delete_asset(asset_id: int) -> bool:
    """ลบอุปกรณ์ (อุปกรณ์พ่วงจะถูกปลด parent เป็น NULL โดยอัตโนมัติ)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assets SET parent_asset_id = NULL WHERE parent_asset_id = ?", (asset_id,))
        cursor.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_asset_status(asset_id: int, status: str) -> bool:
    """เปลี่ยนสถานะอุปกรณ์แบบด่วน (in_use, spare, repair, retired)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assets SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status.strip(), asset_id))
        conn.commit()
        return cursor.rowcount > 0


def get_workstations(dept_id=None, search=None) -> list[dict]:
    """
    จัดกลุ่มข้อมูลแบบชุดโต๊ะทำงาน (Workstation Bundles)
    ดึงเครื่องหลัก (PC/Laptop/Server หรืออุปกรณ์ที่ไม่มี parent) 
    และแนบรายการอุปกรณ์ต่อพ่วง (Monitors, UPS, Printers) ที่ parent_asset_id ชี้มาที่เครื่องนี้
    """
    # 1. ดึงอุปกรณ์ทั้งหมดตาม filter
    all_items = get_assets(dept_id=dept_id, search=search)
    
    # 2. แยก parent กับ children
    # primary_items: parent_asset_id is None หรือ category เป็น pc/laptop/server
    # children_map: {parent_id: [child1, child2]}
    children_map = {}
    primary_items = []
    orphaned_accessories = []
    
    for item in all_items:
        pid = item.get("parent_asset_id")
        if pid:
            if pid not in children_map:
                children_map[pid] = []
            children_map[pid].append(item)
        else:
            primary_items.append(item)

    # 3. ประกอบร่าง workstation bundles
    bundles = []
    for item in primary_items:
        item_id = item["id"]
        children = children_map.get(item_id, [])
        bundles.append({
            "primary": item,
            "children": children,
            "total_items": 1 + len(children)
        })

    # 4. กรณีที่มีอุปกรณ์ที่มี parent_asset_id แต่ตัวแม่ไม่ติดใน filter search ให้แสดงเป็น standalone bundle
    primary_ids = {p["id"] for p in primary_items}
    for pid, c_list in children_map.items():
        if pid not in primary_ids:
            for orphan in c_list:
                bundles.append({
                    "primary": orphan,
                    "children": [],
                    "total_items": 1,
                    "is_orphan": True
                })

    return bundles


def get_asset_stats(dept_id=None) -> dict:
    """สรุปสถิติจำนวนอุปกรณ์แยกตามสถานะและประเภท"""
    with get_db() as conn:
        cursor = conn.cursor()
        where_clause = "WHERE department_id = ?" if dept_id else ""
        params = [dept_id] if dept_id else []
        
        cursor.execute(f"""
            SELECT 
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'in_use' THEN 1 ELSE 0 END) AS in_use,
                SUM(CASE WHEN status = 'spare' THEN 1 ELSE 0 END) AS spare,
                SUM(CASE WHEN status = 'repair' THEN 1 ELSE 0 END) AS repair,
                SUM(CASE WHEN status = 'retired' THEN 1 ELSE 0 END) AS retired,
                SUM(CASE WHEN category = 'laptop' THEN 1 ELSE 0 END) AS laptops,
                SUM(CASE WHEN category = 'pc' THEN 1 ELSE 0 END) AS pcs,
                SUM(CASE WHEN category = 'monitor' THEN 1 ELSE 0 END) AS monitors,
                SUM(CASE WHEN category = 'ups' THEN 1 ELSE 0 END) AS ups_count,
                SUM(CASE WHEN category = 'printer' THEN 1 ELSE 0 END) AS printers
            FROM assets {where_clause}
        """, params)
        row = cursor.fetchone()
        
        return {
            "total": row[0] or 0,
            "in_use": row[1] or 0,
            "spare": row[2] or 0,
            "repair": row[3] or 0,
            "retired": row[4] or 0,
            "laptops": row[5] or 0,
            "pcs": row[6] or 0,
            "monitors": row[7] or 0,
            "ups": row[8] or 0,
            "printers": row[9] or 0
        }


# เริ่มต้นฐานข้อมูลอัตโนมัติเมื่อโหลดโมดูล (ป้องกัน serverless crash หากเครือข่าย cold-start ช้า)
try:
    init_db()
except Exception as _e:
    print(f"[!] Warning: init_db encountered an exception during startup: {_e}")

