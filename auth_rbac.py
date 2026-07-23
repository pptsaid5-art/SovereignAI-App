# -*- coding: utf-8 -*-
"""
auth_rbac.py
============
نظام صلاحيات الموظفين (RBAC) الخاص بـ SovereignAI Node.

مبدأ التصميم الأساسي (Sovereignty-first):
------------------------------------------
- كل بيانات الحسابات والصلاحيات تُخزَّن فقط بملف SQLite محلي على جهاز
  الـ Host، بجانب باقي بيانات التطبيق (RAG DB، إلخ). لا يوجد أي اتصال
  بأي سيرفر خارجي (لا Railway ولا غيره) لهذا الجزء إطلاقاً.
- أجهزة الـ Client تتواصل مع هذه البيانات فقط عبر الشبكة المحلية (LAN)،
  بنفس فلسفة مشاركة chromadb الموجودة أصلاً بالتطبيق.
- لو جهاز الـ Host انطفأ/انقطع، الـ Clients تتوقف عن العمل (بالتصميم،
  حسب طلب المستخدم) لأن لا يوجد نسخة من بيانات الصلاحيات على أي جهاز آخر.

تسلسل الصلاحيات:
-----------------
admin   -> جهاز الـ Host نفسه فقط (لا يحتاج تسجيل دخول، هو صاحب الجهاز).
           هو الوحيد القادر على منح/تعديل/سحب الصلاحيات (assigned_tags).
employee -> يسجّل حسابه بنفسه (self-service) من أي جهاز Client، لكن
           الحساب يبقى بحالة "pending" (لا صلاحيات، لا وصول) حتى
           يوافق عليه الـ Admin ويحدد له الوسوم (tags) المسموحة.
"""

import sqlite3
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import List, Optional

# ---------------------------------------------------------------------------
# إعداد قاعدة البيانات المحلية (على جهاز الـ Host فقط)
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()
_DB_PATH: Optional[str] = None


def init_auth_db(base_dir: str) -> str:
    """
    يُستدعى مرة واحدة عند إقلاع التطبيق (من app.py) لتحديد مسار قاعدة
    بيانات الموظفين، وإنشاء الجداول إن لم تكن موجودة.
    base_dir: نفس المجلد المستخدم لتخزين بيانات RAG/الحالة المحلية.
    """
    global _DB_PATH
    _DB_PATH = os.path.join(base_dir, "employees.db")

    with _db_lock:
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | disabled
                    assigned_tags TEXT NOT NULL DEFAULT '',   -- comma-separated tags
                    created_at REAL NOT NULL,
                    approved_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    employee_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (employee_id) REFERENCES employees (id)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    return _DB_PATH


def _get_conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("auth_rbac: يجب استدعاء init_auth_db() أولاً عند إقلاع التطبيق")
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# تشفير كلمات المرور (PBKDF2 — بدون أي مكتبة خارجية إضافية)
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000
    ).hex()


def _verify_password(password: str, salt: str, stored_hash: str) -> bool:
    computed = _hash_password(password, salt)
    return hmac.compare_digest(computed, stored_hash)


# ---------------------------------------------------------------------------
# تسجيل حساب جديد (Self-service — أي موظف من جهازه، بدون تدخل الأدمن)
# ---------------------------------------------------------------------------

class RegisterResult:
    def __init__(self, success: bool, error: Optional[str] = None):
        self.success = success
        self.error = error


def register_employee(username: str, password: str) -> RegisterResult:
    username = username.strip().lower()

    if len(username) < 3:
        return RegisterResult(False, "اسم المستخدم يجب أن يكون 3 أحرف على الأقل")
    if len(password) < 6:
        return RegisterResult(False, "كلمة المرور يجب أن تكون 6 أحرف على الأقل")

    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)

    with _db_lock:
        conn = _get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM employees WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                return RegisterResult(False, "اسم المستخدم مستخدم بالفعل")

            conn.execute(
                """INSERT INTO employees
                   (username, password_hash, salt, status, assigned_tags, created_at)
                   VALUES (?, ?, ?, 'pending', '', ?)""",
                (username, password_hash, salt, time.time()),
            )
            conn.commit()
            return RegisterResult(True)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# تسجيل الدخول + الجلسات (Sessions)
# ---------------------------------------------------------------------------

class LoginResult:
    def __init__(
        self,
        success: bool,
        error: Optional[str] = None,
        token: Optional[str] = None,
        status: Optional[str] = None,
        assigned_tags: Optional[List[str]] = None,
    ):
        self.success = success
        self.error = error
        self.token = token
        self.status = status
        self.assigned_tags = assigned_tags or []


def login_employee(username: str, password: str) -> LoginResult:
    username = username.strip().lower()

    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM employees WHERE username = ?", (username,)
            ).fetchone()

            if not row:
                return LoginResult(False, "بيانات الدخول غير صحيحة")

            if not _verify_password(password, row["salt"], row["password_hash"]):
                return LoginResult(False, "بيانات الدخول غير صحيحة")

            if row["status"] == "disabled":
                return LoginResult(False, "تم تعطيل هذا الحساب من قِبل الأدمن")

            if row["status"] == "pending":
                # الدخول ينجح لكن بدون توكن صلاحية — الواجهة تعرض "بانتظار موافقة الأدمن"
                return LoginResult(True, status="pending")

            # status == 'approved'
            token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO sessions (token, employee_id, created_at) VALUES (?, ?, ?)",
                (token, row["id"], time.time()),
            )
            conn.commit()

            tags = [t for t in (row["assigned_tags"] or "").split(",") if t]
            return LoginResult(True, status="approved", token=token, assigned_tags=tags)
        finally:
            conn.close()


def get_session(token: str) -> Optional[dict]:
    """يرجع بيانات الموظف المرتبط بتوكن الجلسة، أو None لو التوكن غير صالح."""
    if not token:
        return None
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                """SELECT e.id, e.username, e.status, e.assigned_tags
                   FROM sessions s JOIN employees e ON s.employee_id = e.id
                   WHERE s.token = ?""",
                (token,),
            ).fetchone()
            if not row or row["status"] != "approved":
                return None
            return {
                "id": row["id"],
                "username": row["username"],
                "assigned_tags": [t for t in (row["assigned_tags"] or "").split(",") if t],
            }
        finally:
            conn.close()


def logout(token: str) -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# لوحة تحكم الأدمن (فقط جهاز الـ Host يستدعي هذه الدوال)
# ---------------------------------------------------------------------------

def list_employees() -> List[dict]:
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT id, username, status, assigned_tags, created_at, approved_at "
                "FROM employees ORDER BY created_at DESC"
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "username": r["username"],
                    "status": r["status"],
                    "assigned_tags": [t for t in (r["assigned_tags"] or "").split(",") if t],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()


def set_employee_access(employee_id: int, status: str, assigned_tags: List[str]) -> bool:
    """
    الأدمن فقط: يوافق/يعدّل/يعطّل حساب موظف ويحدد الوسوم المسموحة له.
    status: 'approved' | 'disabled' | 'pending'
    """
    if status not in ("approved", "disabled", "pending"):
        return False

    tags_str = ",".join(sorted(set(t.strip() for t in assigned_tags if t.strip())))

    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """UPDATE employees
                   SET status = ?, assigned_tags = ?,
                       approved_at = CASE WHEN ? = 'approved' THEN ? ELSE approved_at END
                   WHERE id = ?""",
                (status, tags_str, status, time.time(), employee_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_employee(employee_id: int) -> bool:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM sessions WHERE employee_id = ?", (employee_id,))
            cur = conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
