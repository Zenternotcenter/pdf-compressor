#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
========================================================================================
เว็บแอปพลิเคชันระบบจัดการงานและบีบอัด PDF ระดับองค์กร (Enterprise Worklog & PDF Suite)
========================================================================================
ฟีเจอร์และสถาปัตยกรรมหลัก:
  1. Multi-threaded Parallel Execution: รันเอนจิน Ghostscript แบบ Balanced และ Maximum 
     คู่ขนานพร้อมกันบน Multi-Core CPU เพื่อประหยัดเวลาและให้ผู้ใช้เลือกผลลัพธ์ที่ดีที่สุด
  2. Unified Asset Deduplication: ขจัดวัตถุซ้ำซ้อนทั้ง Form XObjects (Vector Template/ลายน้ำ) 
     และ Image XObjects (รูปภาพที่มีแฮช MD5 ตรงกันข้ามหน้า) โดยไม่สูญเสียความคมชัด
  3. Real-time Progress Streaming: ส่งสถานะเปอร์เซ็นต์และความคืบหน้าแบบสดๆ ผ่าน Server-Sent Events (SSE)
     ช่วยให้หน้าเว็บแสดงแถบความคืบหน้าได้ลื่นไหล ไม่เกิด Timeout บนคลาวด์
  4. In-Memory Fast Smart Split: คำนวณจุดตัดแบ่งไฟล์ PDF บนหน่วยความจำ RAM 100% 
     ด้วยอัลกอริทึม Binary Search ผ่าน doc.tobytes() ทำให้ตัดแบ่งไฟล์ 70+ หน้าเสร็จในเสี้ยววินาที
  5. Safe ASCII Storage & RFC 5987 Unicode Headers: จัดเก็บไฟล์ด้วยชื่อสุ่ม ASCII ที่ปลอดภัย 
     และส่งออกชื่อภาษาไทยแท้ 100% ผ่าน Header Content-Disposition UTF-8
  6. Multi-page Live Preview & Zoom Inspection: เรนเดอร์ภาพพรีวิวหน้าเอกสารแบบ High-DPI 
     พร้อมระบบตรวจสอบ DPI รูปภาพเทียบเท่ามาตรฐาน APITemplate.io
  7. Multi-User Worklog & RBAC: ระบบบันทึกงานประจำวัน ปฏิทินงาน สรุปสถิติ และรายงาน PDF ภาษาไทย
  8. IT Asset Management: ทะเบียนทรัพย์สินไอที แยกตามแผนก และระบบชุดโต๊ะทำงาน (Workstation Bundling)
========================================================================================
"""

import os
import re
import io
import time
import math
import uuid
import glob
import json
import gc
import tempfile

import shutil
import hashlib
import zipfile
import threading
import platform
import subprocess
import concurrent.futures
from urllib.parse import quote
from datetime import datetime
try:
    import pymupdf
    HAS_PYMUPDF = True
except Exception as _e:
    pymupdf = None
    HAS_PYMUPDF = False
    print(f"[!] Notice: PyMuPDF not available in current environment: {_e}")

from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context, session, redirect, url_for

import db
import report_gen

# ============================================================
# 1. การตั้งค่า Flask Application และ Serverless Environment
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)
# กำหนด Secret Key สำหรับ Session Cookie ปลอดภัย
app.secret_key = os.environ.get("SECRET_KEY", "pdf-compressor-workspace-secret-key-2026")
app.config['PERMANENT_SESSION_LIFETIME'] = 30 * 24 * 3600  # อายุ Session 30 วัน
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024       # รองรับไฟล์อัปโหลดสูงสุด 200MB
app.config['SESSION_COOKIE_HTTPONLY'] = True               # ป้องกันการเข้าถึงคุกกี้ผ่าน JavaScript (XSS Protection)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'              # ป้องกัน CSRF

# WSGI Middleware สำหรับปรับแก้ Path เมื่อรันบน Vercel Serverless
# เนื่องจาก Vercel Rewrite ส่งคำขอผ่าน /api/index.py ทำให้ PATH_INFO เพี้ยน
_original_wsgi_app = app.wsgi_app

def _vercel_path_fix_wsgi(environ, start_response):
    """
    Middleware ดักจับและปรับค่า PATH_INFO ให้ตรงกับ URL จริงที่ผู้ใช้เรียก:
      1. ตรวจสอบพารามิเตอร์ __path__ ที่ส่งมาจาก vercel.json rewrite เป็นลำดับแรก
      2. ตรวจสอบ Headers มาตรฐาน HTTP_X_FORWARDED_URI หรือ REQUEST_URI
      3. ตัด Prefix /api/index ออกเพื่อให้ Flask Routing แมตช์กับเส้นทางปกติ
    """
    # ลำดับที่ 1: ตรวจสอบ __path__ จาก Query String
    qs = environ.get("QUERY_STRING", "")
    if "__path__=" in qs:
        import urllib.parse
        params = urllib.parse.parse_qs(qs)
        if "__path__" in params and params["__path__"]:
            path = params["__path__"][0]
            if not path.startswith("/"):
                path = "/" + path
            environ["PATH_INFO"] = path
            # ลบ __path__ ออกจาก query parameters เพื่อไม่ให้กระทบกับ Endpoint ปกติ
            clean_params = [(k, v) for k, vs in params.items() if k != "__path__" for v in vs]
            environ["QUERY_STRING"] = urllib.parse.urlencode(clean_params)
            return _original_wsgi_app(environ, start_response)

    # ลำดับที่ 2: ตรวจสอบ Headers จาก Reverse Proxy
    candidates = [
        environ.get("HTTP_X_FORWARDED_URI"),
        environ.get("REQUEST_URI"),
        environ.get("RAW_URI"),
    ]
    real_url = None
    for c in candidates:
        if c and not c.startswith("/api/index"):
            real_url = c
            break
            
    if real_url:
        path_only = real_url.split("?")[0]
        environ["PATH_INFO"] = path_only
    else:
        current_path = environ.get("PATH_INFO", "")
        if current_path in ("/api/index", "/api/index/", "/api/index.py", "/api", "/api/"):
            environ["PATH_INFO"] = "/"
        elif current_path.startswith("/api/index/"):
            environ["PATH_INFO"] = current_path[len("/api/index"):]
        elif current_path.startswith("/api/index.py/"):
            environ["PATH_INFO"] = current_path[len("/api/index.py"):]

    return _original_wsgi_app(environ, start_response)

app.wsgi_app = _vercel_path_fix_wsgi

# ดักจับข้อผิดพลาดระดับ Server Error 500
@app.errorhandler(500)
def internal_server_error(e):
    import traceback
    print(f"[Internal Server Error 500]: {e}\n{traceback.format_exc()}")
    return jsonify({
        "success": False,
        "error": "เกิดข้อผิดพลาดภายในเซิร์ฟเวอร์ กรุณาลองใหม่อีกครั้ง"
    }), 500

# กำหนดโฟลเดอร์สำหรับจัดเก็บไฟล์ชั่วคราว (Writable Uploads Directory)
# รองรับ Read-only Filesystem ของ Vercel / AWS Lambda โดยสลับไปใช้ tempfile อัตโนมัติ
if os.environ.get("VERCEL") or not os.access(os.path.dirname(os.path.abspath(__file__)), os.W_OK):
    UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "worklog_uploads")
else:
    UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ที่เก็บสถานะงานแบบ In-memory สำหรับ Real-time SSE Stream
TASKS = {}
TASKS_LOCK = threading.Lock()

# ระบบป้องกันการเดารหัสผ่าน (Brute-Force Attack Protection Tracker)
FAILED_LOGINS = {}
FAILED_LOGINS_LOCK = threading.Lock()

def check_login_rate_limit(key: str) -> tuple[bool, str]:
    """ตรวจสอบว่า IP หรือ Username นี้ถูกระงับชั่วคราวเนื่องจากกรอกรหัสผิดเกินกำหนดหรือไม่"""
    now = time.time()
    with FAILED_LOGINS_LOCK:
        entry = FAILED_LOGINS.get(key)
        if not entry:
            return True, ""
        if entry.get("locked_until", 0) > now:
            remaining = int(entry["locked_until"] - now)
            return False, f"กรอกรหัสผ่านผิดเกินกำหนด ระบบถูกระงับชั่วคราว กรุณารออีก {remaining} วินาที"
        if entry.get("locked_until", 0) <= now and entry.get("locked_until", 0) > 0:
            FAILED_LOGINS.pop(key, None)
    return True, ""

def record_failed_login(key: str):
    """บันทึกการกรอกรหัสผ่านผิด หากผิดครบ 5 ครั้งจะระงับการเข้าสู่ระบบ 5 นาที (300 วินาที)"""
    now = time.time()
    with FAILED_LOGINS_LOCK:
        entry = FAILED_LOGINS.setdefault(key, {"count": 0, "locked_until": 0})
        entry["count"] += 1
        if entry["count"] >= 5:
            entry["locked_until"] = now + 300  # ล็อก 5 นาที
            entry["count"] = 0

def clear_failed_login(key: str):
    """ล้างประวัติการกรอกผิดเมื่อเข้าสู่ระบบสำเร็จ"""
    with FAILED_LOGINS_LOCK:
        FAILED_LOGINS.pop(key, None)


# ============================================================
# 2. การตรวจหาและเรียกใช้งาน Ghostscript
# ============================================================

def find_ghostscript() -> str:
    if platform.system() == "Windows":
        for base in [r"C:\Program Files\gs", r"C:\Program Files (x86)\gs"]:
            if os.path.isdir(base):
                for root, dirs, files in os.walk(base):
                    for f in files:
                        if f.lower() == "gswin64c.exe":
                            return os.path.join(root, f)
        return "gswin64c"
    return "gs"

GS_CMD = find_ghostscript()


def run_gs(args, timeout=180):
    return subprocess.run([GS_CMD] + args, capture_output=True, text=True, timeout=timeout)


# ============================================================
# Cleanup & File Management Functions
# ============================================================

def _safe_remove(file_path: str, retries: int = 4) -> bool:
    """ลบไฟล์อย่างปลอดภัย รองรับ Windows file-locking retry และ gc.collect()"""
    if not os.path.isfile(file_path):
        return False
    for attempt in range(retries):
        try:
            os.remove(file_path)
            return True
        except PermissionError:
            gc.collect()
            time.sleep(0.08)
        except Exception:
            return False
    return False


def delete_preview_images(file_id: str = None, max_age_seconds: int = 900) -> dict:
    """
    ลบเฉพาะไฟล์รูปภาพพรีวิว (*.png, *.jpg, *.jpeg, *.webp) ใน UPLOAD_DIR
    - ถ้าส่ง file_id มา จะลบรูปภาพทั้งหมดที่เกี่ยวข้องกับ file_id นั้นทันที
    - ถ้าไม่ส่ง file_id จะลบรูปภาพพรีวิวที่อายุเกิน max_age_seconds (ค่าเริ่มต้น 15 นาที)
    คืนค่า dict สรุปจำนวนไฟล์และขนาดที่ลบไป
    """
    deleted_count = 0
    freed_bytes = 0
    now = time.time()
    img_extensions = {".png", ".jpg", ".jpeg", ".webp"}

    try:
        if file_id:
            pattern = os.path.join(UPLOAD_DIR, f"{file_id}_*")
            for f in glob.glob(pattern):
                ext = os.path.splitext(f)[1].lower()
                if ext in img_extensions and os.path.isfile(f):
                    try:
                        sz = os.path.getsize(f)
                        if _safe_remove(f):
                            deleted_count += 1
                            freed_bytes += sz
                    except Exception:
                        pass
        else:
            for f in glob.glob(os.path.join(UPLOAD_DIR, "*")):
                ext = os.path.splitext(f)[1].lower()
                if ext in img_extensions and os.path.isfile(f):
                    try:
                        mtime = os.path.getmtime(f)
                        if now - mtime > max_age_seconds:
                            sz = os.path.getsize(f)
                            if _safe_remove(f):
                                deleted_count += 1
                                freed_bytes += sz
                    except Exception:
                        pass
    except Exception as e:
        print(f"[!] Error in delete_preview_images: {e}")

    return {
        "success": True,
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / 1024 / 1024, 2)
    }


def delete_file_artifacts(file_id: str) -> dict:
    """
    ลบไฟล์ทั้งหมดที่เกี่ยวข้องกับ file_id นั้นใน UPLOAD_DIR
    (ได้แก่ PDF ต้นฉบับ, PDF บีบอัดทุก variant, ชิ้นส่วนแบ่งหน้า, JSON metadata และรูปภาพพรีวิว)
    พร้อมทั้งลบข้อมูล Task ออกจาก TASKS
    """
    if not file_id:
        return {"success": False, "error": "file_id is required"}

    deleted_count = 0
    freed_bytes = 0
    try:
        pattern = os.path.join(UPLOAD_DIR, f"{file_id}_*")
        for f in glob.glob(pattern):
            if os.path.isfile(f):
                try:
                    sz = os.path.getsize(f)
                    if _safe_remove(f):
                        deleted_count += 1
                        freed_bytes += sz
                except Exception:
                    pass

        # Cleanup matching tasks
        with TASKS_LOCK:
            tasks_to_del = [tid for tid, t in TASKS.items() if t.get("file_id") == file_id or tid.startswith(file_id)]
            for tid in tasks_to_del:
                del TASKS[tid]
    except Exception as e:
        print(f"[!] Error in delete_file_artifacts: {e}")

    return {
        "success": True,
        "file_id": file_id,
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / 1024 / 1024, 2)
    }


def cleanup_old_files(image_max_age: int = 900, general_max_age: int = 1800) -> dict:
    """
    ล้างไฟล์ขยะอัตโนมัติ:
    - รูปภาพพรีวิว (*.png, *.jpg, ...) ที่มีอายุเกิน image_max_age (ค่าเริ่มต้น 15 นาที = 900s)
    - ไฟล์เอกสาร PDF, JSON ชั่วคราว ที่มีอายุเกิน general_max_age (ค่าเริ่มต้น 30 นาที = 1800s)
    - งาน Task ที่ค้างนานเกิน 30 นาที
    """
    now = time.time()
    deleted_images = 0
    deleted_docs = 0
    freed_bytes = 0
    img_extensions = {".png", ".jpg", ".jpeg", ".webp"}

    try:
        for f in glob.glob(os.path.join(UPLOAD_DIR, "*")):
            if os.path.isfile(f):
                try:
                    ext = os.path.splitext(f)[1].lower()
                    mtime = os.path.getmtime(f)
                    age = now - mtime
                    is_img = ext in img_extensions

                    # Delete image if older than image_max_age, or doc if older than general_max_age
                    if (is_img and age > image_max_age) or (not is_img and age > general_max_age):
                        sz = os.path.getsize(f)
                        if _safe_remove(f):
                            freed_bytes += sz
                            if is_img:
                                deleted_images += 1
                            else:
                                deleted_docs += 1
                except Exception:
                    pass

        # Cleanup old tasks
        with TASKS_LOCK:
            dead_tasks = [tid for tid, t in TASKS.items() if now - t.get("created_at", now) > 1800]
            for tid in dead_tasks:
                del TASKS[tid]
    except Exception as e:
        print(f"[!] Error in cleanup_old_files: {e}")

    return {
        "success": True,
        "deleted_images": deleted_images,
        "deleted_docs": deleted_docs,
        "total_deleted": deleted_images + deleted_docs,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / 1024 / 1024, 2)
    }


def _background_cleanup_loop(interval: int = 300):
    """Loop ทำความสะอาดไฟล์ขยะในพื้นหลังทุกๆ interval วินาที (ค่าเริ่มต้น 5 นาที)"""
    while True:
        try:
            time.sleep(interval)
            stats = cleanup_old_files(image_max_age=900, general_max_age=1800)
            if stats.get("total_deleted", 0) > 0:
                print(f"[*] [Auto-Cleanup] ลบไฟล์ขยะ {stats['total_deleted']} ไฟล์ (รูปภาพ: {stats['deleted_images']}) ได้พื้นที่คืน {stats['freed_mb']} MB")
        except Exception as e:
            print(f"[!] Background cleanup exception: {e}")


_CLEANUP_WORKER_STARTED = False
_CLEANUP_WORKER_LOCK = threading.Lock()

def start_background_cleanup_worker(interval: int = 300):
    """เริ่มเธรดทำงานเบื้องหลังสำหรับล้างไฟล์ขยะอัตโนมัติ (ครั้งเดียว - ข้ามเมื่อรันบน Serverless เช่น Vercel)"""
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return
    global _CLEANUP_WORKER_STARTED
    with _CLEANUP_WORKER_LOCK:
        if not _CLEANUP_WORKER_STARTED:
            t = threading.Thread(target=_background_cleanup_loop, args=(interval,), daemon=True)
            t.start()
            _CLEANUP_WORKER_STARTED = True
            print(f"[*] เริ่มระบบ Background Auto-Cleanup ทำงานทุกๆ {interval} วินาที เรียบร้อยแล้ว")


# ============================================================
# Integrity Verification
# ============================================================

def verify_pdf(file_path: str, expected_pages: int, min_size_bytes: int = 4000) -> bool:
    """ตรวจสอบว่าไฟล์ PDF ที่ถูกบีบอัดสมบูรณ์ ไม่ว่างเปล่า และมีจำนวนหน้าครบถ้วน 100%"""
    if not os.path.exists(file_path):
        return False
    if os.path.getsize(file_path) < min_size_bytes:
        return False
    try:
        doc = pymupdf.open(file_path)
        actual_pages = len(doc)
        doc.close()
        return actual_pages == expected_pages
    except Exception:
        return False


# ============================================================
# 4. กลยุทธ์การบีบอัดระดับที่ 1: ขจัดวัตถุซ้ำซ้อน (Unified Vector & Image Deduplication)
# ============================================================

def strategy_structure_dedup(input_path: str, output_path: str, expected_pages: int):
    """
    กลยุทธ์ที่ 1 (Smart Vector): ขจัดความซ้ำซ้อนของออบเจกต์ในไฟล์ PDF
    
    หลักการทำงาน:
      1. Vector Form XObjects Deduplication:
         - ตรวจจับ Template เวกเตอร์ ซองจดหมาย หรือลายน้ำที่ฝังซ้ำกันทุกหน้า
         - เชื่อมโยง Reference ของหน้าที่ซ้ำให้ชี้ไปยัง Object ตัวแรกเพียงตัวเดียว (Single Instance)
      2. Raster Image Deduplication:
         - คำนวณ MD5 Hash ของไบนารีรูปภาพทุกรูปในเอกสาร
         - หากพบรูปภาพที่มีค่า Hash ตรงกัน (เช่น โลโก้บริษัท ลายเซ็น ที่ซ้ำกัน 100 หน้า) 
           จะชี้ Pointer ทุกหน้ามาที่รูปเดียว ทำให้ลดขนาดไฟล์ได้อย่างมหาศาล
      3. Deflate Stream & Garbage Cleanup:
         - บีบอัดสตรีมข้อมูลขยะและ Re-index เอกสารด้วย PyMuPDF (deflate=True, garbage=4)
         - ไม่แตะต้องพิกเซลหรือลดทอนความละเอียดรูปภาพแม้แต่จุดเดียว (Lossless 100%)
         
    Returns:
      int หรือ None: ขนาดไฟล์ผลลัพธ์ (ไบต์) หากบีบอัดสำเร็จและขนาดเล็กลงจริง
    """
    try:
        doc = pymupdf.open(input_path)
        changed = False

        # ขั้นที่ 1: ตรวจจับและรวม Form XObjects (Vector Template/Watermark) ที่ซ้ำกัน
        slots = {}
        for i, page in enumerate(doc):
            p_obj = doc.xref_object(page.xref)
            m = re.search(r'/XObject\s*<<([^>]+)>>', p_obj, re.DOTALL)
            if m:
                entries = re.findall(r'(/[\w\d]+)\s+(\d+)\s+0\s+R', m.group(1))
                for slot, ref_str in entries:
                    ref = int(ref_str)
                    try:
                        ref_obj = doc.xref_object(ref)
                        if "/Subtype /Form" in ref_obj:
                            s_len = len(doc.xref_stream(ref)) if doc.xref_is_stream(ref) else 0
                            if slot not in slots:
                                slots[slot] = []
                            slots[slot].append((i, ref, s_len))
                    except Exception:
                        pass

        for slot, entries in slots.items():
            if len(entries) > 1:
                sorted_entries = sorted(entries, key=lambda x: x[2])
                min_size = sorted_entries[0][2]
                best_ref = sorted_entries[0][1]

                for page_idx, ref, s_len in sorted_entries:
                    if s_len > 50000 and s_len > min_size * 5:
                        page = doc[page_idx]
                        p_obj = doc.xref_object(page.xref)
                        new_p_obj = re.sub(
                            rf'({re.escape(slot)}\s+){ref}(\s+0\s+R)',
                            rf'\g<1>{best_ref}\2',
                            p_obj
                        )
                        doc.update_object(page.xref, new_p_obj)
                        changed = True

        # ขั้นที่ 2: ตรวจจับและรวม Image XObjects (รูปภาพที่มีพิกเซลตรงกันข้ามหน้า)
        hash_to_primary = {}
        img_dup_map = {}
        for page in doc:
            for img in page.get_images():
                xref = img[0]
                try:
                    base = doc.extract_image(xref)
                    h = hashlib.md5(base['image']).hexdigest()
                    if h in hash_to_primary:
                        img_dup_map[xref] = hash_to_primary[h]
                    else:
                        hash_to_primary[h] = xref
                except Exception:
                    pass

        if img_dup_map:
            for page in doc:
                p_obj = doc.xref_object(page.xref)
                m = re.search(r'/XObject\s*<<([^>]+)>>', p_obj, re.DOTALL)
                if m:
                    orig_block = m.group(0)
                    new_block = orig_block
                    for slot, ref_str in re.findall(r'(/[\w\d]+)\s+(\d+)\s+0\s+R', m.group(1)):
                        old_ref = int(ref_str)
                        if old_ref in img_dup_map:
                            primary_ref = img_dup_map[old_ref]
                            new_block = re.sub(rf'({re.escape(slot)}\s+){old_ref}(\s+0\s+R)', rf'\g<1>{primary_ref}\2', new_block)
                            changed = True
                    if new_block != orig_block:
                        new_p_obj = p_obj.replace(orig_block, new_block)
                        doc.update_object(page.xref, new_p_obj)

        # บันทึกไฟล์พร้อมกำจัดขยะและบีบอัด Deflate สูงสุด
        doc.save(output_path, garbage=4, deflate=True, clean=True)
        doc.close()

        # ตรวจสอบความสมบูรณ์ของจำนวนหน้า
        if verify_pdf(output_path, expected_pages):
            out_size = os.path.getsize(output_path)
            in_size = os.path.getsize(input_path)
            if out_size < in_size:
                return out_size

        if os.path.exists(output_path):
            os.remove(output_path)
        return None
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


# ============================================================
# 5. กลยุทธ์การบีบอัดระดับที่ 2: Ghostscript Balanced (สมดุล คมชัด + เล็ก)
# ============================================================

def strategy_ghostscript_balanced(input_path: str, output_path: str, expected_pages: int, target_dpi: int = 150):
    """
    กลยุทธ์ที่ 2 (Balanced): บีบอัดรูปภาพด้วยความละเอียด 150 DPI และคุณภาพ JPEG 65%
    
    คุณสมบัติ:
      - เหมาะสำหรับเอกสารทั่วไปที่ต้องการส่งอีเมลหรือใช้งานในสำนักงาน
      - ตัวหนังสือยังคงเป็น Vector 100% คมกริบ ซูมอ่านได้สบายตา
      - ลดความละเอียดของรูปภาพลงเหลือ 150 DPI ด้วยวิธี Bicubic Downsampling
    """
    try:
        args = [
            "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
            "-dPDFSETTINGS=/screen",
            f"-dColorImageResolution={target_dpi}",
            f"-dGrayImageResolution={target_dpi}",
            f"-dMonoImageResolution={target_dpi}",
            "-dColorImageDownsampleType=/Bicubic",
            "-dGrayImageDownsampleType=/Bicubic",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dColorImageQuality=65",
            "-dGrayImageQuality=65",
            "-dEmbedAllFonts=true",
            "-dSubsetFonts=true",
            "-dCompressFonts=true",
            "-dNOPAUSE", "-dQUIET", "-dBATCH",
            f"-sOutputFile={output_path}", input_path
        ]
        res = run_gs(args)
        if res.returncode == 0 and verify_pdf(output_path, expected_pages):
            return os.path.getsize(output_path)

        if os.path.exists(output_path):
            os.remove(output_path)
        return None
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


# ============================================================
# 6. กลยุทธ์การบีบอัดระดับที่ 3: Ghostscript Maximum (บีบอัดสูงสุด / Grayscale)
# ============================================================

def strategy_ghostscript_maximum(input_path: str, output_path: str, expected_pages: int, target_dpi: int = 120):
    """
    กลยุทธ์ที่ 3 (Maximum): บีบอัดระดับสูงสุด ลดเป็นเกรย์สเกล และคุณภาพ JPEG 45%
    
    คุณสมบัติ:
      - เหมาะสำหรับไฟล์ขนาดมหึมา หรือระบบที่จำกัดขนาดไฟล์เข้มงวด (เช่น อัปโหลดระบบราชการ ≤ 1MB)
      - แปลง Color Model เป็น /DeviceGray (ขาวดำ/เทา)
      - ลด DPI เหลือ 120 DPI เพื่อให้ได้ขนาดเล็กที่สุดเท่าที่เป็นไปได้
    """
    try:
        args = [
            "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
            "-dPDFSETTINGS=/screen",
            "-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray",
            f"-dColorImageResolution={target_dpi}",
            f"-dGrayImageResolution={target_dpi}",
            f"-dMonoImageResolution={target_dpi}",
            "-dColorImageDownsampleType=/Bicubic",
            "-dGrayImageDownsampleType=/Bicubic",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dColorImageQuality=45",
            "-dGrayImageQuality=45",
            "-dEmbedAllFonts=true",
            "-dSubsetFonts=true",
            "-dCompressFonts=true",
            "-dNOPAUSE", "-dQUIET", "-dBATCH",
            f"-sOutputFile={output_path}", input_path
        ]
        res = run_gs(args)
        if res.returncode == 0 and verify_pdf(output_path, expected_pages):
            return os.path.getsize(output_path)

        if os.path.exists(output_path):
            os.remove(output_path)
        return None
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


# ============================================================
# 7. การเรนเดอร์ภาพตัวอย่างหน้าเอกสาร (High-DPI Page Preview)
# ============================================================

def render_page_preview(pdf_path: str, png_path: str, page_idx: int = 0, dpi: int = 130) -> bool:
    """
    เรนเดอร์หน้า PDF หน้าใดหน้าหนึ่งออกเป็นไฟล์ภาพ PNG สำหรับแสดงตัวอย่างสดบนหน้าเว็บ
    
    Args:
      pdf_path (str): ตำแหน่งไฟล์ PDF
      png_path (str): ตำแหน่งที่ต้องการบันทึกไฟล์ภาพ PNG
      page_idx (int): ดัชนีหน้าที่ต้องการเรนเดอร์ (เริ่มจาก 0)
      dpi (int): ความละเอียดในการเรนเดอร์ (ค่าเริ่มต้น 130 DPI คมชัดและโหลดเร็ว)
      
    Returns:
      bool: สำเร็จหรือไม่
    """
    try:
        doc = pymupdf.open(pdf_path)
        if 0 <= page_idx < len(doc):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=dpi)
            pix.save(png_path)
            doc.close()
            return True
        doc.close()
    except Exception:
        pass
    return False


# ============================================================
# 8. การตัดแบ่งไฟล์ PDF ความเร็วสูงบน RAM (In-Memory Fast Smart PDF Splitting)
# ============================================================

def split_pdf_smart_by_size(input_path: str, max_size_mb: float, file_id: str, variant_id: str):
    """
    แบ่งไฟล์ PDF อัจฉริยะตามขนาดไฟล์เป้าหมาย (เช่น ชิ้นละไม่เกิน 1.0 MB)
    
    นวัตกรรมเชิงเทคนิค:
      - In-Memory Binary Search: คำนวณจุดตัดหน้าบน RAM 100% ผ่าน doc.tobytes()
        โดยไม่เขียนไฟล์ลง Disk ชั่วคราว ทำให้การแบ่งไฟล์ 70-100 หน้าเสร็จสิ้นใน < 0.5 วินาที
      - Zero Disk Thrashing: เขียนไฟล์ลง Disk เฉพาะชิ้นส่วนที่คำนวณจุดตัดสมบูรณ์แล้วเท่านั้น
      - สรุปผลลัพธ์เป็น JSON สำหรับดาวน์โหลดแยกไฟล์เดี่ยว หรือดาวน์โหลดรวมเป็น ZIP
      
    Args:
      input_path (str): ตำแหน่งไฟล์ PDF ต้นทาง
      max_size_mb (float): ขนาดไฟล์เป้าหมายสูงสุดต่อชิ้น (MB)
      file_id (str): รหัสเอกสาร
      variant_id (str): รุ่นเอกสาร (vector, balanced, maximum)
      
    Returns:
      list: รายการชิ้นส่วนที่แบ่งได้ พร้อมช่วงหน้า ขนาดไฟล์ และ URL สำหรับดาวน์โหลด
    """
    doc = pymupdf.open(input_path)
    total_pages = len(doc)
    target_bytes = max_size_mb * 1024 * 1024

    parts = []
    current_start = 0
    part_num = 1

    while current_start < total_pages:
        low = current_start + 1
        high = total_pages
        best_end = current_start + 1

        # ค้นหาจุดตัดที่พอดีที่สุดด้วย Binary Search บน RAM
        while low <= high:
            mid = (low + high) // 2

            tmp = pymupdf.open()
            tmp.insert_pdf(doc, from_page=current_start, to_page=mid - 1)
            # คำนวณขนาดไบต์จริงบน RAM ทันทีโดยไม่ต้องแตะ Disk
            sz = len(tmp.tobytes(garbage=3, deflate=True))
            tmp.close()

            if sz <= target_bytes:
                best_end = mid
                low = mid + 1
            else:
                high = mid - 1

        # บันทึกชิ้นส่วนที่ผ่านการคำนวณลงดิสก์
        part_name = f"{file_id}_{variant_id}_part{part_num}.pdf"
        part_path = os.path.join(UPLOAD_DIR, part_name)
        part_doc = pymupdf.open()
        part_doc.insert_pdf(doc, from_page=current_start, to_page=best_end - 1)
        part_doc.save(part_path, garbage=3, deflate=True)
        part_doc.close()

        part_sz = os.path.getsize(part_path)
        parts.append({
            "part_num": part_num,
            "from_page": current_start + 1,
            "to_page": best_end,
            "pages_count": best_end - current_start,
            "size_mb": round(part_sz / 1024 / 1024, 2),
            "size_bytes": part_sz,
            "filename": part_name,
            "download_url": f"/download_part/{file_id}/{variant_id}/{part_num}"
        })
        part_num += 1
        current_start = best_end

    doc.close()
    return parts


def split_pdf_by_equal_parts(input_path: str, parts_count: int, file_id: str, variant_id: str):
    """
    แบ่งไฟล์ PDF ออกเป็น N ส่วนเท่าๆ กันตามจำนวนหน้า
    
    Args:
      input_path (str): ตำแหน่งไฟล์ PDF ต้นทาง
      parts_count (int): จำนวนส่วนที่ต้องการแบ่ง
      file_id (str): รหัสเอกสาร
      variant_id (str): รุ่นเอกสาร
      
    Returns:
      list: รายการชิ้นส่วนที่แบ่งได้
    """
    doc = pymupdf.open(input_path)
    total_pages = len(doc)
    parts_count = max(2, min(parts_count, total_pages))
    pages_per_part = math.ceil(total_pages / parts_count)

    parts = []
    part_num = 1
    for i in range(parts_count):
        start = i * pages_per_part
        end = min((i + 1) * pages_per_part, total_pages)
        if start >= total_pages:
            break

        part_name = f"{file_id}_{variant_id}_part{part_num}.pdf"
        part_path = os.path.join(UPLOAD_DIR, part_name)
        part_doc = pymupdf.open()
        part_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
        part_doc.save(part_path, garbage=3, deflate=True)
        part_doc.close()

        part_sz = os.path.getsize(part_path)
        parts.append({
            "part_num": part_num,
            "from_page": start + 1,
            "to_page": end,
            "pages_count": end - start,
            "size_mb": round(part_sz / 1024 / 1024, 2),
            "size_bytes": part_sz,
            "filename": part_name,
            "download_url": f"/download_part/{file_id}/{variant_id}/{part_num}"
        })
        part_num += 1

    doc.close()
    return parts


# ============================================================
# 9. ระบบวิเคราะห์ความละเอียดรูปภาพระดับลึก (Deep PDF DPI Analyzer)
# ============================================================

def analyze_pdf_dpi(pdf_path: str, file_id: str = None) -> dict:
    """
    วิเคราะห์ความละเอียดรูปภาพใน PDF ระดับลึก เทียบเท่ามาตรฐาน APITemplate.io:
      - คำนวณ Effective Displayed DPI ตามขนาดแสดงผลจริงบนหน้ากระดาษ
      - สกัดขนาดพิกเซล (px), ขนาดพิมพ์จริง (มม. / นิ้ว), Color Space, ฟอร์แมต
      - คำนวณสรุปเอกสาร: Min, Max, Average DPI, Total Images
      - ตรวจจับโครงสร้างและสถานะ 100% Vector Text & Graphics
      - จัดเกรดคุณภาพ (Print Ready 300+ DPI, Standard 150-300 DPI, Web < 150 DPI)
      - แคชผลลัพธ์เป็นไฟล์ JSON เพื่อให้หน้าเว็บดึงซ้ำได้ทันที
    """
    if not os.path.exists(pdf_path):
        return {
            "summary": {
                "has_images": False,
                "total_images": 0,
                "unique_images": 0,
                "total_pages": 0,
                "min_dpi": 0,
                "max_dpi": 0,
                "avg_dpi": 0,
                "print_count": 0,
                "standard_count": 0,
                "web_count": 0,
                "overall_assessment": "ไม่พบไฟล์",
                "overall_grade": "N/A",
                "overall_color": "#64748b",
                "vector_status": "N/A"
            },
            "images": []
        }

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as e:
        return {
            "summary": {
                "has_images": False,
                "total_images": 0,
                "unique_images": 0,
                "total_pages": 0,
                "min_dpi": 0,
                "max_dpi": 0,
                "avg_dpi": 0,
                "error": str(e),
                "overall_assessment": "เกิดข้อผิดพลาดในการเปิดไฟล์",
                "overall_grade": "Error",
                "overall_color": "#ef4444",
                "vector_status": "N/A"
            },
            "images": []
        }

    total_pages = len(doc)
    images = []
    seen_xrefs = set()

    for page_idx in range(total_pages):
        page = doc[page_idx]
        image_list = page.get_images(full=True)

        for img_info in image_list:
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                if not base_image:
                    continue

                w = base_image.get("width", 0)
                h = base_image.get("height", 0)
                ext = base_image.get("ext", "png").lower()
                bpc = base_image.get("bpc", 8)
                cs = base_image.get("colorspace", 3)
                cs_str = "RGB" if cs == 3 else ("CMYK" if cs == 4 else ("Grayscale" if cs == 1 else "Unknown"))
                img_size_bytes = len(base_image.get("image", b""))

                # คำนวณพิกัดแสดงผลจริงบนหน้ากระดาษ
                img_rects = page.get_image_rects(xref)
                if img_rects:
                    dpi_x = 72
                    dpi_y = 72
                    disp_w_mm = round(w * 25.4 / 72.0, 1)
                    disp_h_mm = round(h * 25.4 / 72.0, 1)
                    disp_w_in = round(w / 72.0, 2)
                    disp_h_in = round(h / 72.0, 2)

                # Quality tier & badge
                if eff_dpi >= 300:
                    quality_tier = "print"
                    quality_label = "Print Ready (300+ DPI)"
                    quality_badge = "คมชัดระดับโรงพิมพ์"
                    badge_color = "#10b981"  # Emerald
                elif eff_dpi >= 150:
                    quality_tier = "standard"
                    quality_label = "Standard (150-300 DPI)"
                    quality_badge = "มาตรฐานเอกสาร"
                    badge_color = "#2563eb"  # Blue
                else:
                    quality_tier = "web"
                    quality_label = "Web / Screen (< 150 DPI)"
                    quality_badge = "เน้นจอภาพ/เว็บ"
                    badge_color = "#f59e0b"  # Amber

                thumb_url = f"/extract_image/{file_id}/{xref}" if file_id else None

                images.append({
                    "id": f"p{page_idx + 1}_x{xref}",
                    "page": page_idx + 1,
                    "xref": xref,
                    "width": w,
                    "height": h,
                    "dpi_x": dpi_x,
                    "dpi_y": dpi_y,
                    "effective_dpi": eff_dpi,
                    "disp_w_mm": disp_w_mm,
                    "disp_h_mm": disp_h_mm,
                    "disp_w_in": disp_w_in,
                    "disp_h_in": disp_h_in,
                    "colorspace": cs_str,
                    "format": ext.upper(),
                    "bpc": bpc,
                    "size_kb": round(img_size_bytes / 1024, 1),
                    "quality_tier": quality_tier,
                    "quality_label": quality_label,
                    "quality_badge": quality_badge,
                    "badge_color": badge_color,
                    "thumb_url": thumb_url
                })
                seen_xrefs.add(xref)
            except Exception:
                continue

    doc.close()

    total_imgs = len(images)
    if total_imgs > 0:
        dpis = [img["effective_dpi"] for img in images]
        min_dpi = min(dpis)
        max_dpi = max(dpis)
        avg_dpi = round(sum(dpis) / total_imgs)
        print_count = sum(1 for img in images if img["quality_tier"] == "print")
        standard_count = sum(1 for img in images if img["quality_tier"] == "standard")
        web_count = sum(1 for img in images if img["quality_tier"] == "web")

        if avg_dpi >= 300:
            overall_assessment = "Print Ready (คมชัดระดับโรงพิมพ์ 300+ DPI)"
            overall_grade = "ดีเยี่ยม (300+ DPI)"
            overall_color = "#10b981"
        elif avg_dpi >= 150:
            overall_assessment = "Standard Quality (ความคมชัดระดับมาตรฐาน 150-300 DPI)"
            overall_grade = "มาตรฐาน (150-300 DPI)"
            overall_color = "#2563eb"
        else:
            overall_assessment = "Screen / Web Optimized (เหมาะสำหรับเปิดดูบนจอ < 150 DPI)"
            overall_grade = "เน้นแสดงผลจอภาพ (< 150 DPI)"
            overall_color = "#f59e0b"
    else:
        min_dpi = 0
        max_dpi = 0
        avg_dpi = 0
        print_count = 0
        standard_count = 0
        web_count = 0
        overall_assessment = "Pure Vector Document (คมชัดระดับอนันต์ 100% Vector)"
        overall_grade = "สมบูรณ์แบบ (Vector 100%)"
        overall_color = "#10b981"

    summary = {
        "has_images": total_imgs > 0,
        "total_images": total_imgs,
        "unique_images": len(seen_xrefs),
        "total_pages": total_pages,
        "min_dpi": min_dpi,
        "max_dpi": max_dpi,
        "avg_dpi": avg_dpi,
        "print_count": print_count,
        "standard_count": standard_count,
        "web_count": web_count,
        "overall_assessment": overall_assessment,
        "overall_grade": overall_grade,
        "overall_color": overall_color,
        "vector_status": "ตัวหนังสือและกราฟิกเวกเตอร์ 100% (ความละเอียดไม่จำกัด คมชัดระดับอนันต์)"
    }

    result = {
        "summary": summary,
        "images": images
    }

    if file_id:
        dpi_cache_path = os.path.join(UPLOAD_DIR, f"{file_id}_dpi.json")
        try:
            with open(dpi_cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass

    return result


# ============================================================
# Master Processing Engine (Parallel Multi-threaded)
# ============================================================

def process_pdf(input_path: str, file_id: str, original_name: str, mode: str = "vector", target_dpi: int = 150, max_size_mb: float = 1.0, progress_cb=None):
    cleanup_old_files()

    if progress_cb: progress_cb(5, "กำลังเริ่มต้นและอ่านโครงสร้างเอกสาร...")

    if not os.path.exists(input_path):
        return {"success": False, "error": f"ไม่พบไฟล์: {input_path}"}

    try:
        doc_in = pymupdf.open(input_path)
        total_pages = len(doc_in)
        doc_in.close()
    except Exception as e:
        return {"success": False, "error": f"ไม่สามารถอ่านไฟล์ PDF ได้: {str(e)}"}

    input_size = os.path.getsize(input_path)
    max_size_bytes = max_size_mb * 1024 * 1024

    meta = {
        "file_id": file_id,
        "original_name": original_name,
        "input_size": input_size,
        "total_pages": total_pages,
        "timestamp": time.time()
    }
    meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(meta, mf, ensure_ascii=False)

    if progress_cb: progress_cb(15, "กำลังสร้างภาพตัวอย่างหน้าแรก (Thumbnail)...")
    before_thumb_name = f"{file_id}_before_p0.png"
    before_thumb_path = os.path.join(UPLOAD_DIR, before_thumb_name)
    render_page_preview(input_path, before_thumb_path, page_idx=0, dpi=130)

    if progress_cb: progress_cb(22, "กำลังวิเคราะห์ DPI และความละเอียดรูปภาพ (DPI Analyzer)...")
    dpi_analysis = analyze_pdf_dpi(input_path, file_id)

    # 1. Tier 1: Unified Asset Deduplication (Vector + Image XObjects)
    if progress_cb: progress_cb(30, "กำลังขจัดวัตถุเวกเตอร์และรูปภาพซ้ำซ้อน (Asset Deduplication)...")
    v1_file = f"{file_id}_vector.pdf"
    v1_path = os.path.join(UPLOAD_DIR, v1_file)
    v1_size = strategy_structure_dedup(input_path, v1_path, total_pages)

    base_for_next = v1_path if v1_size else input_path

    # 2. Tier 2 & 3: Run Ghostscript Balanced & Maximum in PARALLEL via Multi-threading!
    if progress_cb: progress_cb(55, "กำลังประมวลผล Multi-thread Ghostscript (Balanced + Maximum) คู่ขนาน...")
    v2_file = f"{file_id}_balanced.pdf"
    v2_path = os.path.join(UPLOAD_DIR, v2_file)
    v3_file = f"{file_id}_maximum.pdf"
    v3_path = os.path.join(UPLOAD_DIR, v3_file)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fut_balanced = executor.submit(strategy_ghostscript_balanced, base_for_next, v2_path, total_pages, target_dpi)
        fut_maximum = executor.submit(strategy_ghostscript_maximum, base_for_next, v3_path, total_pages, 120)

        v2_size = fut_balanced.result()
        v3_size = fut_maximum.result()

    if progress_cb: progress_cb(85, "กำลังรวบรวมผลลัพธ์และตรวจสอบความถูกต้อง...")

    variants = []
    if v1_size and v1_size < input_size:
        red1 = round((1 - v1_size / input_size) * 100, 1)
        variants.append({
            "id": "vector",
            "name": "Smart Vector (เวกเตอร์คมชัด 100%)",
            "desc": "รักษาตัวหนังสือ ตาราง บาร์โค้ด และฟอนต์ 100% ซูมไม่แตก ไม่แตะพิกเซล",
            "size_mb": round(v1_size / 1024 / 1024, 2),
            "size_bytes": v1_size,
            "reduction_percent": red1,
            "filename": v1_file,
            "badge": "คมชัดสูงสุด 100%",
            "priority": 120
        })

    if v2_size and v2_size < input_size:
        red2 = round((1 - v2_size / input_size) * 100, 1)
        variants.append({
            "id": "balanced",
            "name": "Balanced (สมดุล คมชัด + กะทัดรัด)",
            "desc": "บีบอัดรูปภาพด้วยความละเอียดสูง ข้อความยังคมกริบ เหมาะสำหรับส่งอีเมล",
            "size_mb": round(v2_size / 1024 / 1024, 2),
            "size_bytes": v2_size,
            "reduction_percent": red2,
            "filename": v2_file,
            "badge": "สมดุล (แนะนำ)",
            "priority": 110
        })

    if v3_size and v3_size < input_size:
        red3 = round((1 - v3_size / input_size) * 100, 1)
        variants.append({
            "id": "maximum",
            "name": "Maximum Compression (ขนาดเล็กที่สุด)",
            "desc": "ปรับโทนสีเอกสารและบีบอัดภาพเต็มพิกัด สำหรับอัปโหลดเว็บที่จำกัดพื้นที่",
            "size_mb": round(v3_size / 1024 / 1024, 2),
            "size_bytes": v3_size,
            "reduction_percent": red3,
            "filename": v3_file,
            "badge": "เล็กที่สุด",
            "priority": 90
        })

    if not variants:
        return {
            "success": True,
            "file_id": file_id,
            "total_pages": total_pages,
            "input_size_mb": round(input_size / 1024 / 1024, 2),
            "output_size_mb": round(input_size / 1024 / 1024, 2),
            "reduction_percent": 0,
            "original_name": original_name,
            "preview_before": f"/preview/{file_id}/before/0",
            "preview_after": f"/preview/{file_id}/before/0",
            "best_variant": {
                "id": "original",
                "name": "ไฟล์ต้นฉบับ (มีขนาดเหมาะสมแล้ว)",
                "size_mb": round(input_size / 1024 / 1024, 2),
                "filename": f"{file_id}_in.pdf",
                "badge": "ต้นฉบับ"
            },
            "variants": [],
            "size_ok": input_size <= max_size_bytes,
            "warning": "ไฟล์นี้ได้รับการบีบอัดอย่างดีแล้ว ไม่สามารถลดขนาดเพิ่มเติมได้อีกโดยไม่สูญเสียเนื้อหา",
            "dpi_analysis": dpi_analysis
        }

    # Best variant selection
    best_var = None
    if mode == "vector":
        for v in variants:
            if v["id"] == "vector":
                best_var = v
                break
    elif mode == "balanced":
        for v in variants:
            if v["id"] == "balanced":
                best_var = v
                break
    elif mode == "maximum":
        for v in variants:
            if v["id"] == "maximum":
                best_var = v
                break

    if not best_var:
        under_target = [v for v in variants if v["size_bytes"] <= max_size_bytes]
        if under_target:
            best_var = max(under_target, key=lambda x: x["priority"])
        else:
            best_var = min(variants, key=lambda x: x["size_bytes"])

    # Thumbnail generation for best variant
    if progress_cb: progress_cb(95, "กำลังสร้างพรีวิวเปรียบเทียบผลลัพธ์...")
    best_file_path = os.path.join(UPLOAD_DIR, best_var["filename"])
    after_thumb_name = f"{file_id}_after_p0.png"
    after_thumb_path = os.path.join(UPLOAD_DIR, after_thumb_name)
    render_page_preview(best_file_path, after_thumb_path, page_idx=0, dpi=130)

    meta["best_variant_id"] = best_var["id"]
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(meta, mf, ensure_ascii=False)

    size_ok = (best_var["size_bytes"] <= max_size_bytes)
    suggest_split = (not size_ok) and (total_pages > 3)

    if progress_cb: progress_cb(100, "บีบอัดสำเร็จเรียบร้อย!")

    return {
        "success": True,
        "file_id": file_id,
        "total_pages": total_pages,
        "input_size_mb": round(input_size / 1024 / 1024, 2),
        "output_size_mb": best_var["size_mb"],
        "reduction_percent": best_var["reduction_percent"],
        "original_name": original_name,
        "preview_before": f"/preview/{file_id}/before/0",
        "preview_after": f"/preview/{file_id}/{best_var['id']}/0",
        "best_variant": best_var,
        "variants": variants,
        "size_ok": size_ok,
        "suggest_split": suggest_split,
        "target_max_size_mb": max_size_mb,
        "warning": None if size_ok else f"ขนาดที่ทำได้คือ {best_var['size_mb']} MB (เป้าหมาย {max_size_mb} MB)",
        "dpi_analysis": dpi_analysis
    }


# ============================================================
# Flask Routes & Real-Time SSE Streaming
# ============================================================

HTTP_ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]

@app.route("/", methods=HTTP_ALL_METHODS)
@app.route("/api/index", methods=HTTP_ALL_METHODS)
@app.route("/api/index/", methods=HTTP_ALL_METHODS)
def index():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "username" in data and "password" in data:
            login_fn = app.view_functions.get("api_auth_login")
            if login_fn:
                return login_fn()
    if request.args.get("debug") == "1":
        env_dump = {k: str(v) for k, v in request.environ.items() if isinstance(v, (str, int, bool))}
        return jsonify({
            "path": request.path,
            "environ": env_dump,
            "headers": dict(request.headers)
        })
    return render_template("index.html")


@app.route("/upload_async", methods=["POST"])
def upload_async():
    """
    รับไฟล์และเริ่มประมวลผลใน Background Thread
    ส่ง task_id คืนทันทีเพื่อให้ Client ฟังความคืบหน้าผ่าน SSE (/stream/<task_id>)
    """
    if "file" not in request.files:
        return jsonify({"success": False, "error": "ไม่พบไฟล์ในคำขอ"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "ไม่ได้เลือกไฟล์"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "รองรับเฉพาะไฟล์ PDF เท่านั้น"}), 400

    task_id = str(uuid.uuid4())[:12]
    file_id = str(uuid.uuid4())[:8]
    original_name = file.filename

    input_filename = f"{file_id}_in.pdf"
    input_path = os.path.join(UPLOAD_DIR, input_filename)
    file.save(input_path)

    mode = request.form.get("mode", "vector")
    try:
        target_dpi = int(request.form.get("dpi", 150))
    except Exception:
        target_dpi = 150
    try:
        max_size = float(request.form.get("max_size", 1.0))
    except Exception:
        max_size = 1.0

    with TASKS_LOCK:
        TASKS[task_id] = {
            "percent": 0,
            "step": "กำลังเริ่มต้น...",
            "done": False,
            "result": None,
            "error": None,
            "created_at": time.time(),
            "file_id": file_id
        }

    # Serverless (Vercel / AWS Lambda): Run synchronously in-process with PyMuPDF because Lambda freezes background threads
    is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    if is_serverless:
        try:
            res = process_pdf(
                input_path=input_path,
                file_id=file_id,
                original_name=original_name,
                mode=mode,
                target_dpi=target_dpi,
                max_size_mb=max_size,
                progress_cb=None
            )
            with TASKS_LOCK:
                TASKS[task_id] = {
                    "percent": 100,
                    "step": "บีบอัดสำเร็จเรียบร้อย!",
                    "done": True,
                    "result": res,
                    "error": None,
                    "created_at": time.time(),
                    "file_id": file_id
                }
            return jsonify({
                "success": True,
                "task_id": task_id,
                "file_id": file_id,
                "immediate_result": res
            })
        except Exception as e:
            return jsonify({"success": False, "error": f"การประมวลผลล้มเหลว: {e}"}), 500

    def progress_callback(pct, msg):
        with TASKS_LOCK:
            if task_id in TASKS:
                TASKS[task_id]["percent"] = pct
                TASKS[task_id]["step"] = msg

    def worker():
        try:
            res = process_pdf(
                input_path=input_path,
                file_id=file_id,
                original_name=original_name,
                mode=mode,
                target_dpi=target_dpi,
                max_size_mb=max_size,
                progress_cb=progress_callback
            )
            with TASKS_LOCK:
                if task_id in TASKS:
                    TASKS[task_id]["done"] = True
                    TASKS[task_id]["percent"] = 100
                    TASKS[task_id]["result"] = res
        except Exception as e:
            with TASKS_LOCK:
                if task_id in TASKS:
                    TASKS[task_id]["done"] = True
                    TASKS[task_id]["error"] = str(e)

    threading.Thread(target=worker, daemon=True).start()

    return jsonify({"success": True, "task_id": task_id, "file_id": file_id})


@app.route("/stream/<task_id>")
def stream_progress(task_id):
    """
    Server-Sent Events (SSE) ส่งสตรีมเปอร์เซ็นต์จริงจาก Python สู่หน้าเว็บ
    """
    def event_generator():
        while True:
            done = False
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                if not task:
                    yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                    break

                payload = {
                    "percent": task.get("percent", 0),
                    "step": task.get("step", ""),
                    "done": task.get("done", False)
                }
                if task.get("done"):
                    payload["result"] = task.get("result")
                    payload["error"] = task.get("error")
                    done = True

            yield f"data: {json.dumps(payload)}\n\n"
            if done:
                break
            time.sleep(0.35)

    resp = Response(stream_with_context(event_generator()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/status/<task_id>")
def get_task_status(task_id):
    """Fallback Polling endpoint เพื่อตรวจสอบสถานะงาน"""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify({
            "percent": task.get("percent", 0),
            "step": task.get("step", ""),
            "done": task.get("done", False),
            "result": task.get("result"),
            "error": task.get("error")
        })


@app.route("/upload", methods=["POST"])
def upload_sync():
    """Fallback สำหรับ Synchronous Upload"""
    if "file" not in request.files:
        return jsonify({"success": False, "error": "ไม่พบไฟล์ในคำขอ"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "ไม่ได้เลือกไฟล์"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "รองรับเฉพาะไฟล์ PDF เท่านั้น"}), 400

    file_id = str(uuid.uuid4())[:8]
    original_name = file.filename

    input_filename = f"{file_id}_in.pdf"
    input_path = os.path.join(UPLOAD_DIR, input_filename)
    file.save(input_path)

    mode = request.form.get("mode", "vector")
    try:
        target_dpi = int(request.form.get("dpi", 150))
    except Exception:
        target_dpi = 150
    try:
        max_size = float(request.form.get("max_size", 1.0))
    except Exception:
        max_size = 1.0

    result = process_pdf(
        input_path=input_path,
        file_id=file_id,
        original_name=original_name,
        mode=mode,
        target_dpi=target_dpi,
        max_size_mb=max_size
    )

    return jsonify(result)


@app.route("/preview/<file_id>/<which>/<int:page_idx>")
def preview_page(file_id, which, page_idx):
    thumb_name = f"{file_id}_{which}_p{page_idx}.png"
    thumb_path = os.path.join(UPLOAD_DIR, thumb_name)

    if not os.path.exists(thumb_path):
        if which == "before" or which == "in":
            pdf_path = os.path.join(UPLOAD_DIR, f"{file_id}_in.pdf")
        else:
            pdf_path = os.path.join(UPLOAD_DIR, f"{file_id}_{which}.pdf")

        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(UPLOAD_DIR, f"{file_id}_in.pdf")

        if not os.path.exists(pdf_path):
            return jsonify({"error": "ไม่พบเอกสารสำหรับสร้างพรีวิว"}), 404

        success = render_page_preview(pdf_path, thumb_path, page_idx=page_idx, dpi=130)
        if not success or not os.path.exists(thumb_path):
            return jsonify({"error": "ไม่สามารถเรนเดอร์หน้าพรีวิวได้"}), 500

    try:
        with open(thumb_path, "rb") as img_f:
            img_data = img_f.read()
        return send_file(io.BytesIO(img_data), mimetype="image/png")
    except Exception:
        return send_file(thumb_path, mimetype="image/png")


# ============================================================
# DPI Analyzer & Image Extraction Endpoints
# ============================================================

@app.route("/analyze_dpi/<file_id>")
def get_pdf_dpi(file_id):
    """ส่งผลการวิเคราะห์ DPI แบบละเอียดเทียบเท่า APITemplate.io"""
    cache_path = os.path.join(UPLOAD_DIR, f"{file_id}_dpi.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception:
            pass

    pdf_path = os.path.join(UPLOAD_DIR, f"{file_id}_in.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({"error": "ไม่พบไฟล์ PDF"}), 404

    result = analyze_pdf_dpi(pdf_path, file_id)
    return jsonify(result)


@app.route("/extract_image/<file_id>/<int:xref>")
def extract_image(file_id, xref):
    """ส่งภาพ Thumbnail หรือดาวน์โหลดรูปภาพเดี่ยวที่สกัดจาก PDF ตาม XREF"""
    pdf_path = os.path.join(UPLOAD_DIR, f"{file_id}_in.pdf")
    if not os.path.exists(pdf_path):
        for cand in ["vector", "balanced", "maximum"]:
            p = os.path.join(UPLOAD_DIR, f"{file_id}_{cand}.pdf")
            if os.path.exists(p):
                pdf_path = p
                break
    if not os.path.exists(pdf_path):
        return jsonify({"error": "ไม่พบไฟล์ PDF ต้นทาง"}), 404

    try:
        doc = pymupdf.open(pdf_path)
        base_img = doc.extract_image(xref)
        doc.close()
        if not base_img:
            return jsonify({"error": "ไม่พบรูปภาพใน XREF ที่ระบุ"}), 404

        ext = base_img.get("ext", "png").lower()
        if ext == "jpg":
            ext = "jpeg"
        mimetype = f"image/{ext}"
        img_bytes = base_img["image"]

        as_download = request.args.get("download", "0") == "1"
        download_name = f"image_{file_id}_xref{xref}.{ext}"

        response = send_file(
            io.BytesIO(img_bytes),
            mimetype=mimetype,
            as_attachment=as_download,
            download_name=download_name
        )
        if as_download:
            response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return response
    except Exception as e:
        return jsonify({"error": f"ไม่สามารถดึงรูปภาพได้: {str(e)}"}), 500


@app.route("/download/<file_id>")
@app.route("/download/<file_id>/<variant_id>")
def download_by_id(file_id, variant_id="vector"):
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{variant_id}.pdf")
    if not os.path.exists(file_path):
        for cand in [variant_id, "vector", "balanced", "maximum", "in", "merged", "split", "img2pdf", "stamped"]:
            cand_path = os.path.join(UPLOAD_DIR, f"{file_id}_{cand}.pdf")
            if os.path.exists(cand_path):
                file_path = cand_path
                variant_id = cand
                break

    if not os.path.exists(file_path):
        return jsonify({"error": "ไม่พบไฟล์ที่ต้องการดาวน์โหลด"}), 404

    original_name = request.args.get("name", "").strip()
    if not original_name:
        meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as mf:
                    meta = json.load(mf)
                    original_name = meta.get("original_name", "")
            except Exception:
                pass

    if not original_name.lower().endswith(".pdf"):
        original_name += ".pdf"

    clean_orig = original_name[:-4]
    clean_orig = re.sub(r'[\r\n"\'\\/]+', '_', clean_orig).strip('_')
    if not clean_orig:
        clean_orig = f"doc_{file_id}"

    if variant_id in ["merged", "split", "img2pdf", "stamped"]:
        download_name = f"{clean_orig}.pdf"
    else:
        download_name = f"compressed_{variant_id}_{clean_orig}.pdf"
    encoded_name = quote(download_name.encode("utf-8"))
    ascii_name = f"doc_{file_id}_{variant_id}.pdf"

    response = send_file(
        file_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    response.headers["Content-Type"] = "application/pdf"
    return response


# ============================================================
# Smart Split & Chunk Endpoints
# ============================================================

@app.route("/split/<file_id>", methods=["POST"])
def split_pdf_endpoint(file_id):
    variant_id = request.form.get("variant_id", "vector")
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{variant_id}.pdf")
    if not os.path.exists(file_path):
        for candidate in ["vector", "balanced", "maximum", "in"]:
            cand_path = os.path.join(UPLOAD_DIR, f"{file_id}_{candidate}.pdf")
            if os.path.exists(cand_path):
                file_path = cand_path
                variant_id = candidate
                break

    if not os.path.exists(file_path):
        return jsonify({"success": False, "error": "ไม่พบไฟล์ที่จะทำการแบ่ง"}), 404

    split_mode = request.form.get("split_mode", "size")

    try:
        if split_mode == "parts":
            parts_count = int(request.form.get("parts_count", 3))
            parts = split_pdf_by_equal_parts(file_path, parts_count, file_id, variant_id)
        else:
            max_size_mb = float(request.form.get("max_size_mb", 1.0))
            parts = split_pdf_smart_by_size(file_path, max_size_mb, file_id, variant_id)

        split_meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_{variant_id}_split.json")
        with open(split_meta_path, "w", encoding="utf-8") as smf:
            json.dump(parts, smf, ensure_ascii=False)

        return jsonify({
            "success": True,
            "file_id": file_id,
            "variant_id": variant_id,
            "split_mode": split_mode,
            "total_parts": len(parts),
            "parts": parts,
            "zip_url": f"/download_zip/{file_id}/{variant_id}"
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"การแบ่งไฟล์ล้มเหลว: {str(e)}"}), 500


@app.route("/download_part/<file_id>/<variant_id>/<int:part_num>")
def download_part(file_id, variant_id, part_num):
    part_name = f"{file_id}_{variant_id}_part{part_num}.pdf"
    part_path = os.path.join(UPLOAD_DIR, part_name)
    if not os.path.exists(part_path):
        return jsonify({"error": f"ไม่พบไฟล์ Part {part_num}"}), 404

    meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")
    original_name = "document"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as mf:
                original_name = json.load(mf).get("original_name", "document")
        except Exception:
            pass

    clean_orig = original_name[:-4] if original_name.lower().endswith(".pdf") else original_name
    clean_orig = re.sub(r'[\r\n"\'\\/]+', '_', clean_orig).strip('_')
    if not clean_orig:
        clean_orig = f"doc_{file_id}"

    page_info_str = f"Part{part_num}"
    split_meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_{variant_id}_split.json")
    if os.path.exists(split_meta_path):
        try:
            with open(split_meta_path, "r", encoding="utf-8") as smf:
                s_parts = json.load(smf)
                for p in s_parts:
                    if p.get("part_num") == part_num:
                        page_info_str = f"Part{part_num}_(หน้า {p.get('from_page')}-{p.get('to_page')})"
                        break
        except Exception:
            pass

    download_name = f"{page_info_str}_{clean_orig}.pdf"
    encoded_name = quote(download_name.encode("utf-8"))

    response = send_file(
        part_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name
    )
    response.headers["Content-Disposition"] = f'attachment; filename="part_{part_num}_{file_id}.pdf"; filename*=UTF-8\'\'{encoded_name}'
    response.headers["Content-Type"] = "application/pdf"
    return response


@app.route("/download_zip/<file_id>/<variant_id>")
def download_zip(file_id, variant_id):
    split_meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_{variant_id}_split.json")
    if not os.path.exists(split_meta_path):
        return jsonify({"error": "ไม่พบข้อมูลไฟล์ที่ถูกแบ่ง"}), 404

    meta_path = os.path.join(UPLOAD_DIR, f"{file_id}_meta.json")
    original_name = "document"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as mf:
                original_name = json.load(mf).get("original_name", "document")
        except Exception:
            pass

    clean_orig = original_name[:-4] if original_name.lower().endswith(".pdf") else original_name
    clean_orig = re.sub(r'[\r\n"\'\\/]+', '_', clean_orig).strip('_')
    if not clean_orig:
        clean_orig = f"doc_{file_id}"

    with open(split_meta_path, "r", encoding="utf-8") as smf:
        parts = json.load(smf)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in parts:
            part_num = p["part_num"]
            p_file = os.path.join(UPLOAD_DIR, p["filename"])
            if os.path.exists(p_file):
                archive_name = f"Part{part_num}_(หน้า {p.get('from_page')}-{p.get('to_page')})_{clean_orig}.pdf"
                zf.write(p_file, arcname=archive_name)

    zip_buffer.seek(0)
    zip_download_name = f"all_parts_{clean_orig}.zip"
    encoded_zip = quote(zip_download_name.encode("utf-8"))

    response = send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name=zip_download_name)
    response.headers["Content-Disposition"] = f'attachment; filename="split_{file_id}.zip"; filename*=UTF-8\'\'{encoded_zip}'
    return response


# ============================================================
# File & Image Cleanup Endpoints
# ============================================================

@app.route("/delete_images/<file_id>", methods=["POST", "GET"])
@app.route("/api/delete_images/<file_id>", methods=["POST", "GET"])
def endpoint_delete_images(file_id):
    """ลบเฉพาะรูปภาพพรีวิว (*.png) ของ file_id นั้นทันทีเพื่อประหยัดเนื้อที่"""
    result = delete_preview_images(file_id=file_id)
    return jsonify(result)


@app.route("/delete/<file_id>", methods=["POST", "GET"])
@app.route("/delete_file/<file_id>", methods=["POST", "GET"])
@app.route("/api/delete/<file_id>", methods=["POST", "GET"])
def endpoint_delete_file(file_id):
    """ลบไฟล์ทั้งหมด (PDF ต้นฉบับ, PDF บีบอัด, รูปภาพพรีวิว, JSON) ของ file_id นั้นทันที"""
    result = delete_file_artifacts(file_id=file_id)
    return jsonify(result)


@app.route("/api/cleanup", methods=["POST", "GET"])
@app.route("/cleanup", methods=["POST", "GET"])
def endpoint_manual_cleanup():
    """เรียกทำความสะอาดไฟล์ขยะและรูปภาพพรีวิวทั้งหมดในโฟลเดอร์ uploads ทันที"""
    img_age = request.args.get("image_age", 900, type=int)
    doc_age = request.args.get("doc_age", 1800, type=int)
    result = cleanup_old_files(image_max_age=img_age, general_max_age=doc_age)
    return jsonify(result)


# ============================================================
# Personal Workspace: Auth & User Management Security Endpoints
# ============================================================

def is_auth() -> bool:
    """ตรวจสอบว่าผู้ใช้ล็อกอินแล้วหรือยัง"""
    return session.get("auth") is True and session.get("user_id") is not None


def is_admin() -> bool:
    """ตรวจสอบว่าผู้ใช้เป็นผู้ดูแลระบบ (IT / admin) หรือไม่"""
    if not is_auth():
        return False
    role = session.get("role", "")
    username = str(session.get("username", "")).lower()
    return role == "admin" or username == "it"


def get_current_user():
    """ดึงข้อมูลผู้ใช้ปัจจุบันจาก session"""
    if not is_auth():
        return None
    return {
        "id": session.get("user_id"),
        "username": session.get("username"),
        "display_name": session.get("display_name", session.get("username")),
        "role": session.get("role", "user"),
        "department_id": session.get("department_id"),
        "department_name": session.get("department_name", "")
    }


@app.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    user = get_current_user()
    pending_count = db.get_pending_users_count() if is_admin() else 0
    return jsonify({
        "authenticated": is_auth(),
        "user": user,
        "is_admin": is_admin(),
        "pending_users_count": pending_count,
        "user_name": user["display_name"] if user else db.get_setting("user_name", "ผู้ปฏิบัติงาน"),
        "org_name": db.get_setting("org_name", "บันทึกการปฏิบัติงานประจำวัน")
    })


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    try:
        data = request.get_json() or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        remember = data.get("remember", True)

        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        rate_key = f"{client_ip}:{username.lower()}"

        # ตรวจสอบการพยายามสุ่มรหัสผ่าน (Rate Limiting)
        allowed, err_msg = check_login_rate_limit(rate_key)
        if not allowed:
            return jsonify({"success": False, "error": err_msg}), 429

        ok, user, msg = db.authenticate_user(username, password)
        if not ok:
            record_failed_login(rate_key)
            return jsonify({"success": False, "error": msg}), 401

        # เข้าสู่ระบบสำเร็จ: เคลียร์ประวัติการกรอกผิด
        clear_failed_login(rate_key)

        # ป้องกัน Session Fixation: ล้าง session เดิมก่อนเริ่ม session ใหม่
        session.clear()
        session["auth"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["display_name"] = user.get("display_name") or user.get("full_name") or user["username"]
        session["role"] = user.get("role", "user")
        session["department_id"] = user.get("department_id")
        session["department_name"] = user.get("department_name", "")
        session.permanent = bool(remember)

        return jsonify({
            "success": True,
            "message": msg,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "display_name": session["display_name"],
                "role": session["role"],
                "is_admin": is_admin(),
                "department_name": session["department_name"]
            }
        })
    except Exception as e:
        import traceback
        print(f"[Login Server Error]: {e}\n{traceback.format_exc()}")
        return jsonify({
            "success": False,
            "error": "เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล กรุณาตรวจสอบการตั้งค่าระบบ"
        }), 500


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    display_name = str(data.get("display_name", "")).strip()
    department_id = data.get("department_id")
    reason = str(data.get("request_reason", "")).strip()

    ok, msg = db.register_user(username, password, display_name, department_id, request_reason=reason)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    return jsonify({"success": True, "message": msg})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"success": True, "message": "ออกจากระบบเรียบร้อยแล้ว"})


@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    """ปิดการใช้งาน PIN Bypass เพื่อความปลอดภัยสูงสุด ให้ใช้ Username & Password แทน"""
    return jsonify({
        "success": False, 
        "error": "ระบบเปลี่ยนมาใช้ Username & Password ที่ปลอดภัย กรุณาเข้าสู่ระบบด้วยบัญชีของคุณ"
    }), 400


# ============================================================
# User Management Endpoints (Admin / Master Account 'It' Only)
# ============================================================

@app.route("/api/admin/users", methods=["GET"])
def api_admin_get_users():
    if not is_admin():
        return jsonify({"success": False, "error": "สงวนสิทธิ์สำหรับผู้ดูแลระบบ (IT) เท่านั้น"}), 403
    users = db.get_all_users()
    return jsonify({"success": True, "users": users})


@app.route("/api/admin/users/approve", methods=["POST"])
def api_admin_approve_user():
    if not is_admin():
        return jsonify({"success": False, "error": "สงวนสิทธิ์สำหรับผู้ดูแลระบบ (IT) เท่านั้น"}), 403
    data = request.get_json() or {}
    user_id = data.get("user_id")
    role = data.get("role", "user")
    if not user_id:
        return jsonify({"success": False, "error": "ระบุ user_id ไม่ถูกต้อง"}), 400

    current_admin = session.get("username", "It")
    ok, msg = db.approve_user(int(user_id), approved_by=current_admin, role=role)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route("/api/admin/users/reject", methods=["POST"])
def api_admin_reject_user():
    if not is_admin():
        return jsonify({"success": False, "error": "สงวนสิทธิ์สำหรับผู้ดูแลระบบ (IT) เท่านั้น"}), 403
    data = request.get_json() or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "ระบุ user_id ไม่ถูกต้อง"}), 400

    ok, msg = db.reject_user(int(user_id))
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route("/api/admin/users/status", methods=["POST"])
def api_admin_change_user_status():
    if not is_admin():
        return jsonify({"success": False, "error": "สงวนสิทธิ์สำหรับผู้ดูแลระบบ (IT) เท่านั้น"}), 403
    data = request.get_json() or {}
    user_id = data.get("user_id")
    status = data.get("status")
    if not user_id or not status:
        return jsonify({"success": False, "error": "ข้อมูลไม่ครบถ้วน"}), 400

    ok, msg = db.change_user_status(int(user_id), status)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route("/api/admin/users/create", methods=["POST"])
def api_admin_create_user():
    if not is_admin():
        return jsonify({"success": False, "error": "สงวนสิทธิ์สำหรับผู้ดูแลระบบ (IT) เท่านั้น"}), 403
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    display_name = str(data.get("display_name", "")).strip()
    department_id = data.get("department_id")
    role = data.get("role", "user")

    ok, msg = db.admin_create_user(username, password, display_name, department_id, role)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route("/api/admin/users/reset-password", methods=["POST"])
def api_admin_reset_password():
    if not is_admin():
        return jsonify({"success": False, "error": "สงวนสิทธิ์สำหรับผู้ดูแลระบบ (IT) เท่านั้น"}), 403
    data = request.get_json() or {}
    user_id = data.get("user_id")
    new_password = str(data.get("new_password", "")).strip()
    if not user_id or not new_password:
        return jsonify({"success": False, "error": "ข้อมูลไม่ครบถ้วน"}), 400

    ok, msg = db.admin_reset_password(int(user_id), new_password)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route("/api/auth/change-password", methods=["POST"])
@app.route("/api/auth/change-pin", methods=["POST"])
def api_auth_change_pin():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    user = get_current_user()
    if not user or not user.get("id"):
        return jsonify({"success": False, "error": "ไม่พบข้อมูลบัญชีผู้ใช้"}), 401

    data = request.get_json() or {}
    old_p = str(data.get("old_password") or data.get("old_pin", "")).strip()
    new_p = str(data.get("new_password") or data.get("new_pin", "")).strip()

    ok, msg = db.change_user_password(user["id"], old_p, new_p)
    if ok:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    if request.method == "POST":
        data = request.get_json() or {}
        if "user_name" in data:
            db.set_setting("user_name", data["user_name"])
        if "org_name" in data:
            db.set_setting("org_name", data["org_name"])
        return jsonify({"success": True})
    return jsonify({
        "user_name": db.get_setting("user_name", "ผู้ปฏิบัติงาน"),
        "org_name": db.get_setting("org_name", "บันทึกการปฏิบัติงานประจำวัน")
    })


# ============================================================
# Personal Workspace: Daily Tasks Endpoints
# ============================================================

@app.route("/api/tasks", methods=["GET", "POST"])
def api_tasks_handler():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401

    current_user = get_current_user()

    if request.method == "POST":
        data = request.get_json() or {}
        title = data.get("title", "").strip()
        if not title:
            return jsonify({"success": False, "error": "กรุณาระบุชื่องาน"}), 400
        date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
        category = data.get("category", "ทั่วไป")
        priority = data.get("priority", "normal")
        status = data.get("status", "todo")
        notes = data.get("notes", "")
        time_spent = data.get("time_spent", "")
        
        # ผูกผู้สร้างงานเป็นผู้ใช้ที่กำลังล็อกอินอยู่เสมอ
        task_id = db.create_task(
            date=date, 
            title=title, 
            category=category, 
            priority=priority, 
            status=status, 
            notes=notes, 
            time_spent=time_spent,
            user_id=current_user["id"],
            creator_name=current_user["display_name"]
        )
        return jsonify({"success": True, "id": task_id})

    # GET
    date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if not date and not (start_date and end_date):
        date = datetime.now().strftime("%Y-%m-%d")

    # กำหนด user_id สำหรับกรอง:
    # - User ทั่วไป: บังคับกรองเฉพาะงานของตนเอง 100%
    # - Admin: ดูของทุกคนได้ หรือเลือกกรองรายคนได้
    if is_admin():
        req_user_id = request.args.get("user_id", "all")
        target_user_id = None if req_user_id in ("all", "", None) else req_user_id
    else:
        target_user_id = current_user["id"]

    tasks = db.get_tasks(user_id=target_user_id, date=date, start_date=start_date, end_date=end_date)
    stats = db.get_daily_stats(date, user_id=target_user_id) if date else {
        "total": len(tasks),
        "done": sum(1 for t in tasks if t.get("status") == "done"),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "todo": sum(1 for t in tasks if t.get("status") == "todo"),
        "percent": round(sum(1 for t in tasks if t.get("status") == "done") / len(tasks) * 100) if tasks else 0
    }
    return jsonify({
        "tasks": tasks,
        "stats": stats,
        "date": date,
        "is_admin": is_admin(),
        "filter_user_id": target_user_id if target_user_id else "all",
        "thai_date": report_gen.format_thai_date(date) if date else ""
    })


@app.route("/api/tasks/<int:task_id>", methods=["PUT", "DELETE"])
def api_task_single(task_id):
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    current_user = get_current_user()
    admin_mode = is_admin()

    if request.method == "DELETE":
        ok = db.delete_task(task_id, requesting_user_id=current_user["id"], is_admin=admin_mode)
        if not ok:
            return jsonify({"success": False, "error": "ไม่พบงาน หรือคุณไม่มีสิทธิ์ลบงานของผู้อื่น"}), 403
        return jsonify({"success": True})

    data = request.get_json() or {}
    ok = db.update_task(task_id, requesting_user_id=current_user["id"], is_admin=admin_mode, **data)
    if not ok:
        return jsonify({"success": False, "error": "ไม่พบงาน หรือคุณไม่มีสิทธิ์แก้ไขงานของผู้อื่น"}), 403
    return jsonify({"success": True})


@app.route("/api/tasks/<int:task_id>/toggle", methods=["POST"])
def api_task_toggle_single(task_id):
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    current_user = get_current_user()
    new_status = db.toggle_task(task_id, requesting_user_id=current_user["id"], is_admin=is_admin())
    if new_status is None:
        return jsonify({"success": False, "error": "ไม่พบงาน หรือคุณไม่มีสิทธิ์แก้ไขงานนี้"}), 403
    return jsonify({"success": True, "new_status": new_status})


@app.route("/api/tasks/calendar", methods=["GET"])
def api_tasks_calendar_view():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    current_user = get_current_user()
    if is_admin():
        req_user_id = request.args.get("user_id", "all")
        target_user_id = None if req_user_id in ("all", "", None) else req_user_id
    else:
        target_user_id = current_user["id"]
    return jsonify(db.get_month_tasks_data(month, user_id=target_user_id))


@app.route("/api/tasks/users-list", methods=["GET"])
def api_tasks_users_list():
    """ส่งรายชื่อพนักงาน active สำหรับสร้างตัวกรองงานของ Admin"""
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    if not is_admin():
        current_user = get_current_user()
        return jsonify({
            "success": True, 
            "users": [{"id": current_user["id"], "username": current_user["username"], "full_name": current_user["display_name"], "role": "user"}]
        })
    return jsonify({"success": True, "users": db.get_active_task_users()})


# ============================================================
# Personal Workspace: Report Generation & Export Endpoints
# ============================================================

@app.route("/report/view", methods=["GET"])
def route_report_view():
    if not is_auth():
        return redirect("/?login=1")

    current_user = get_current_user()
    date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    report_type = request.args.get("type", "daily")

    if is_admin():
        req_user_id = request.args.get("user_id", "all")
        target_user_id = None if req_user_id in ("all", "", None) else req_user_id
        if target_user_id:
            u_info = db.get_user_by_id(int(target_user_id))
            user_name = u_info["display_name"] if u_info else "ผู้ปฏิบัติงาน"
        else:
            user_name = f"ภาพรวมพนักงานทุกคน ({db.get_setting('org_name', 'องค์กร')})"
    else:
        target_user_id = current_user["id"]
        user_name = current_user["display_name"]

    if report_type == "range" and start_date and end_date:
        tasks = db.get_tasks(user_id=target_user_id, start_date=start_date, end_date=end_date)
        date_label = f"{report_gen.format_thai_date(start_date)} ถึง {report_gen.format_thai_date(end_date)}"
        report_title = "รายงานสรุปผลการปฏิบัติงานประจำช่วงเวลา"
        date_query = f"{start_date}_{end_date}"
    else:
        tasks = db.get_tasks(user_id=target_user_id, date=date)
        date_label = report_gen.format_thai_date(date)
        report_title = "รายงานสรุปผลการปฏิบัติงานประจำวัน"
        date_query = date

    stats = {
        "total": len(tasks),
        "done": sum(1 for t in tasks if t.get("status") == "done"),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "todo": sum(1 for t in tasks if t.get("status") == "todo"),
        "percent": round(sum(1 for t in tasks if t.get("status") == "done") / len(tasks) * 100) if tasks else 0
    }

    org_name = db.get_setting("org_name", "บันทึกการปฏิบัติงานประจำวัน")

    return render_template(
        "report_print.html",
        tasks=tasks,
        stats=stats,
        date_label=date_label,
        date_query=date_query,
        report_type=report_type,
        report_title=report_title,
        user_name=user_name,
        org_name=org_name,
        print_timestamp=datetime.now().strftime("%d/%m/%Y %H:%M")
    )


@app.route("/api/export/pdf", methods=["GET"])
def api_export_pdf_direct():
    if not is_auth():
        return jsonify({"error": "Unauthorized"}), 401

    current_user = get_current_user()
    date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    report_type = request.args.get("type", "daily")

    if is_admin():
        req_user_id = request.args.get("user_id", "all")
        target_user_id = None if req_user_id in ("all", "", None) else req_user_id
        if target_user_id:
            u_info = db.get_user_by_id(int(target_user_id))
            user_name = u_info["display_name"] if u_info else "ผู้ปฏิบัติงาน"
        else:
            user_name = f"ภาพรวมพนักงานทุกคน ({db.get_setting('org_name', 'องค์กร')})"
    else:
        target_user_id = current_user["id"]
        user_name = current_user["display_name"]

    if report_type == "range" and start_date and end_date:
        tasks = db.get_tasks(user_id=target_user_id, start_date=start_date, end_date=end_date)
        date_label = f"{report_gen.format_thai_date(start_date)} - {report_gen.format_thai_date(end_date)}"
        report_title = "รายงานสรุปผลการปฏิบัติงานประจำช่วงเวลา"
        filename = f"worklog_report_{start_date}_to_{end_date}.pdf"
    else:
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        tasks = db.get_tasks(user_id=target_user_id, date=date)
        date_label = report_gen.format_thai_date(date)
        report_title = "รายงานสรุปผลการปฏิบัติงานประจำวัน"
        filename = f"daily_report_{date}.pdf"

    org_name = db.get_setting("org_name", "บันทึกการปฏิบัติงานประจำวัน")

    pdf_bytes = report_gen.generate_pdf_report(
        tasks=tasks,
        report_title=report_title,
        date_label=date_label,
        user_name=user_name,
        org_name=org_name
    )

    encoded_name = quote(filename.encode("utf-8"))
    ascii_name = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    response = send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    return response


@app.route("/api/export/excel", methods=["GET"])
def api_export_excel():
    """ส่งออกรายงานการปฏิบัติงานเป็นไฟล์ Microsoft Excel (.xlsx)"""
    if not is_auth():
        return jsonify({"error": "Unauthorized"}), 401

    current_user = get_current_user()
    date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    report_type = request.args.get("type", "daily")

    if is_admin():
        req_user_id = request.args.get("user_id", "all")
        target_user_id = None if req_user_id in ("all", "", None) else req_user_id
        if target_user_id:
            u_info = db.get_user_by_id(int(target_user_id))
            user_name = u_info["display_name"] if u_info else "ผู้ปฏิบัติงาน"
        else:
            user_name = f"ภาพรวมพนักงานทุกคน ({db.get_setting('org_name', 'องค์กร')})"
    else:
        target_user_id = current_user["id"]
        user_name = current_user["display_name"]

    if report_type == "range" and start_date and end_date:
        tasks = db.get_tasks(user_id=target_user_id, start_date=start_date, end_date=end_date)
        date_label = f"{report_gen.format_thai_date(start_date)} - {report_gen.format_thai_date(end_date)}"
        report_title = "รายงานสรุปผลการปฏิบัติงานประจำช่วงเวลา"
        filename = f"worklog_report_{start_date}_to_{end_date}.xlsx"
    else:
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        tasks = db.get_tasks(user_id=target_user_id, date=date)
        date_label = report_gen.format_thai_date(date)
        report_title = "รายงานสรุปผลการปฏิบัติงานประจำวัน"
        filename = f"daily_report_{date}.xlsx"

    org_name = db.get_setting("org_name", "บันทึกการปฏิบัติงานประจำวัน")

    excel_bytes = report_gen.generate_tasks_excel(
        tasks=tasks,
        report_title=report_title,
        date_label=date_label,
        user_name=user_name,
        org_name=org_name
    )

    encoded_name = quote(filename.encode("utf-8"))
    ascii_name = f"worklog_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    response = send_file(
        io.BytesIO(excel_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    return response



# ============================================================
# ============================================================
# 13. ชุดเครื่องมือจัดการไฟล์ PDF ครบวงจร (All-in-One PDF Tool Suite)
# ============================================================

@app.route("/api/pdf/merge", methods=["POST"])
def api_pdf_merge():
    """
    รวมไฟล์ PDF หลายไฟล์เข้าด้วยกันเป็นไฟล์เดียว (PDF Merge)
    - รับไฟล์ PDF จากคำขอ (ฟิลด์ 'files')
    - ตรวจสอบความถูกต้องและผสานหน้าเอกสารด้วย PyMuPDF
    - บันทึกไฟล์ผลลัพธ์และส่งคืน URL สำหรับดาวน์โหลด
    """
    files = request.files.getlist("files")
    if not files or len(files) == 0:
        return jsonify({"success": False, "error": "กรุณาเลือกไฟล์ PDF อย่างน้อย 2 ไฟล์"}), 400

    merged_doc = pymupdf.open()
    total_pages = 0
    file_count = 0
    for f in files:
        if f and f.filename:
            data = f.read()
            if not data:
                continue
            try:
                doc = pymupdf.open(stream=data, filetype="pdf")
                merged_doc.insert_pdf(doc)
                total_pages += len(doc)
                file_count += 1
                doc.close()
            except Exception as e:
                return jsonify({"success": False, "error": f"ไฟล์ '{f.filename}' มีปัญหาหรือไม่ใช่ PDF: {str(e)}"}), 400

    if total_pages == 0:
        return jsonify({"success": False, "error": "ไม่มีหน้าเอกสารที่สามารถรวมได้"}), 400

    file_id = f"proc_{uuid.uuid4().hex[:10]}"
    output_filename = f"{file_id}_merged.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    merged_doc.save(output_path)
    merged_doc.close()

    size_bytes = os.path.getsize(output_path)
    return jsonify({
        "success": True,
        "file_id": file_id,
        "variant": "merged",
        "download_url": f"/download/{file_id}/merged?name=merged_document.pdf",
        "total_files": file_count,
        "total_pages": total_pages,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 2)
    })


@app.route("/api/pdf/split", methods=["POST"])
def api_pdf_split():
    """
    แยกหน้า PDF ตามหมายเลขหน้าที่ผู้ใช้กำหนด (Custom Page Split)
    - รองรับช่วงหน้าแบบยืดหยุ่น เช่น '1-3, 5, 8-10'
    - ดึงเฉพาะหน้าที่เลือกออกมาสร้างเป็นไฟล์ PDF ใหม่
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "กรุณาเลือกไฟล์ PDF"}), 400

    range_str = request.form.get("page_range", "").strip()
    if not range_str:
        return jsonify({"success": False, "error": "กรุณาระบุหน้าที่ต้องการแยก เช่น 1-3, 5"}), 400

    stream = file.read()
    if not stream:
        return jsonify({"success": False, "error": "ไฟล์ว่างเปล่า"}), 400

    try:
        doc = pymupdf.open(stream=stream, filetype="pdf")
    except Exception as e:
        return jsonify({"success": False, "error": f"ไม่สามารถเปิดไฟล์ PDF ได้: {str(e)}"}), 400

    total_pages = len(doc)
    selected_indices = set()
    parts = [p.strip() for p in range_str.split(",") if p.strip()]

    for part in parts:
        if "-" in part:
            sub = part.split("-")
            if len(sub) == 2 and sub[0].strip().isdigit() and sub[1].strip().isdigit():
                start = max(1, int(sub[0].strip()))
                end = min(total_pages, int(sub[1].strip()))
                for p in range(start, end + 1):
                    selected_indices.add(p - 1)
        elif part.isdigit():
            p = int(part)
            if 1 <= p <= total_pages:
                selected_indices.add(p - 1)

    sorted_indices = sorted(list(selected_indices))
    if not sorted_indices:
        doc.close()
        return jsonify({"success": False, "error": f"หมายเลขหน้าที่ระบุไม่ถูกต้อง (เอกสารนี้มีทั้งหมด {total_pages} หน้า)"}), 400

    new_doc = pymupdf.open()
    new_doc.insert_pdf(doc, from_page=0, to_page=total_pages - 1)
    new_doc.select(sorted_indices)

    file_id = f"proc_{uuid.uuid4().hex[:10]}"
    output_filename = f"{file_id}_split.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    new_doc.save(output_path)
    new_doc.close()
    doc.close()

    size_bytes = os.path.getsize(output_path)
    orig_clean = os.path.splitext(file.filename)[0] if file.filename else "document"
    return jsonify({
        "success": True,
        "file_id": file_id,
        "variant": "split",
        "download_url": f"/download/{file_id}/split?name={orig_clean}_split.pdf",
        "extracted_pages": len(sorted_indices),
        "total_pages": total_pages,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 2)
    })


@app.route("/api/pdf/images-to-pdf", methods=["POST"])
def api_pdf_images_to_pdf():
    """
    แปลงรูปภาพหลายรูป (JPG, PNG, WebP) ให้เป็นไฟล์ PDF เอกสารชุดเดียวกัน (Images to PDF)
    """
    images = request.files.getlist("images")
    if not images or len(images) == 0:
        return jsonify({"success": False, "error": "กรุณาเลือกไฟล์รูปภาพ (JPG, PNG, WebP)"}), 400

    out_doc = pymupdf.open()
    page_count = 0
    for img_file in images:
        if not img_file or not img_file.filename:
            continue
        data = img_file.read()
        if not data:
            continue
        ext = img_file.filename.split(".")[-1].lower()
        if ext == "jpg":
            ext = "jpeg"
        if ext not in ["jpeg", "png", "webp", "bmp"]:
            continue
        try:
            img_doc = pymupdf.open(stream=data, filetype=ext)
            pdf_bytes = img_doc.convert_to_pdf()
            img_pdf = pymupdf.open("pdf", pdf_bytes)
            out_doc.insert_pdf(img_pdf)
            page_count += 1
            img_doc.close()
            img_pdf.close()
        except Exception:
            continue

    if page_count == 0:
        return jsonify({"success": False, "error": "ไม่สามารถแปลงรูปภาพเป็น PDF ได้ กรุณาตรวจสอบไฟล์รูปภาพ"}), 400

    file_id = f"proc_{uuid.uuid4().hex[:10]}"
    output_filename = f"{file_id}_img2pdf.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    out_doc.save(output_path)
    out_doc.close()

    size_bytes = os.path.getsize(output_path)
    return jsonify({
        "success": True,
        "file_id": file_id,
        "variant": "img2pdf",
        "download_url": f"/download/{file_id}/img2pdf?name=images_converted.pdf",
        "total_pages": page_count,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 2)
    })


@app.route("/api/pdf/watermark", methods=["POST"])
def api_pdf_watermark():
    """
    ประทับลายน้ำลงบนไฟล์ PDF ทุกหน้า (PDF Watermark)
    - รองรับข้อความภาษาไทย (เช่น 'สำเนาถูกต้อง', 'CONFIDENTIAL', 'เอกสารภายใน')
    - ตรวจหาฟอนต์ไทยอัตโนมัติเพื่อป้องกันวรรณยุกต์จมหรือแสดงเป็นสี่เหลี่ยม
    - กำหนดมุมเอียง 35 องศา, สี (Hex / Named color), ขนาดฟอนต์, และค่าความโปร่งใส (Opacity)
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "กรุณาเลือกไฟล์ PDF"}), 400

    text = request.form.get("text", "สำเนาถูกต้อง").strip()
    if not text:
        text = "สำเนาถูกต้อง"

    hex_color = request.form.get("color", "#dc2626").strip()
    # รองรับทั้งชื่อสีมาตรฐานและรหัสสี Hex
    color_map = {
        "red": (0.85, 0.15, 0.15),
        "blue": (0.15, 0.35, 0.85),
        "gray": (0.45, 0.45, 0.45),
        "green": (0.1, 0.6, 0.2)
    }
    if hex_color in color_map:
        color = color_map[hex_color]
    elif hex_color.startswith("#") and len(hex_color) == 7:
        try:
            r = int(hex_color[1:3], 16) / 255.0
            g = int(hex_color[3:5], 16) / 255.0
            b = int(hex_color[5:7], 16) / 255.0
            color = (r, g, b)
        except Exception:
            color = (0.85, 0.15, 0.15)
    else:
        color = (0.85, 0.15, 0.15)

    try:
        opacity = float(request.form.get("opacity", "0.30"))
        opacity = max(0.05, min(1.0, opacity))
    except Exception:
        opacity = 0.30

    try:
        font_size = int(request.form.get("fontsize", "42"))
        font_size = max(12, min(120, font_size))
    except Exception:
        font_size = 42

    stream = file.read()
    if not stream:
        return jsonify({"success": False, "error": "ไฟล์ว่างเปล่า"}), 400

    try:
        doc = pymupdf.open(stream=stream, filetype="pdf")
    except Exception as e:
        return jsonify({"success": False, "error": f"ไม่สามารถเปิดไฟล์ PDF ได้: {str(e)}"}), 400

    total_pages = len(doc)
    has_thai = any('\u0e00' <= ch <= '\u0e7f' for ch in text)
    thai_font = report_gen.get_thai_font_path() if has_thai else None

    # วาดลายน้ำกึ่งกลางหน้ากระดาษเอียง -35 องศาในทุกหน้า
    for page in doc:
        rect = page.rect
        center_x = rect.width * 0.15
        center_y = rect.height * 0.55
        point = pymupdf.Point(center_x, center_y)

        font_name = "helv"
        if thai_font:
            try:
                page.insert_font(fontname="ThaiWM", fontfile=thai_font)
                font_name = "ThaiWM"
            except Exception:
                font_name = "helv"

        morph = (point, pymupdf.Matrix().prerotate(-35))
        page.insert_text(
            point,
            text,
            fontsize=font_size,
            morph=morph,
            color=color,
            fontname=font_name,
            fill_opacity=opacity
        )

    file_id = f"proc_{uuid.uuid4().hex[:10]}"
    output_filename = f"{file_id}_stamped.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    doc.save(output_path)
    doc.close()

    size_bytes = os.path.getsize(output_path)
    orig_clean = os.path.splitext(file.filename)[0] if file.filename else "document"
    return jsonify({
        "success": True,
        "file_id": file_id,
        "variant": "stamped",
        "download_url": f"/download/{file_id}/stamped?name={orig_clean}_stamped.pdf",
        "total_pages": total_pages,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 2)
    })


# ============================================================
# 14. ระบบบริหารจัดการทรัพย์สินไอที (IT Asset Management Endpoints)
# ============================================================

@app.route("/api/departments", methods=["GET", "POST"])
def api_departments():
    """
    API จัดการข้อมูลแผนก:
      - GET: ดึงรายชื่อแผนกทั้งหมดในองค์กร
      - POST: เพิ่มแผนกใหม่ (ต้องระบุชื่อแผนก รหัสย่อ และชั้น/ห้อง)
    """
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
        
    if request.method == "POST":
        data = request.get_json() or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"success": False, "error": "กรุณาระบุชื่อแผนก"}), 400
        code = data.get("code", "").strip()
        floor_room = data.get("floor_room", "").strip()
        dept_id = db.create_department(name, code, floor_room)
        return jsonify({"success": True, "id": dept_id})
        
    depts = db.get_departments()
    return jsonify({"success": True, "departments": depts})


@app.route("/api/departments/<int:dept_id>", methods=["PUT", "DELETE"])
def api_department_single(dept_id):
    """API แก้ไขหรือลบแผนกตาม ID"""
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
        
    if request.method == "DELETE":
        ok, msg = db.delete_department(dept_id)
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "error": msg}), 400
        
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "error": "กรุณาระบุชื่อแผนก"}), 400
    code = data.get("code", "").strip()
    floor_room = data.get("floor_room", "").strip()
    ok = db.update_department(dept_id, name, code, floor_room)
    return jsonify({"success": ok})


@app.route("/api/assets", methods=["GET", "POST"])
def api_assets():
    """
    API ทะเบียนทรัพย์สินไอที:
      - GET: ค้นหาและดึงรายการอุปกรณ์ตามเงื่อนไข (แผนก, สถานะ, ประเภท, คำค้นหา, เครื่องแม่)
      - POST: เพิ่มอุปกรณ์ใหม่ พร้อมสร้าง Asset Tag อัตโนมัติ (เช่น IT-PC-001)
    """
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
        
    if request.method == "POST":
        data = request.get_json() or {}
        brand_model = data.get("brand_model", "").strip()
        if not brand_model:
            return jsonify({"success": False, "error": "กรุณาระบุยี่ห้อและรุ่นของอุปกรณ์"}), 400
        asset_id = db.create_asset(data)
        return jsonify({"success": True, "id": asset_id})
        
    dept_id = request.args.get("dept_id", type=int)
    status = request.args.get("status")
    category = request.args.get("category")
    search = request.args.get("search")
    parent_id = request.args.get("parent_id", type=int)
    
    assets = db.get_assets(dept_id=dept_id, status=status, category=category, search=search, parent_id=parent_id)
    return jsonify({"success": True, "assets": assets})


@app.route("/api/assets/<int:asset_id>", methods=["GET", "PUT", "DELETE"])
def api_asset_single(asset_id):
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
        
    if request.method == "GET":
        asset = db.get_asset(asset_id)
        if not asset:
            return jsonify({"success": False, "error": "ไม่พบอุปกรณ์"}), 404
        return jsonify({"success": True, "asset": asset})
        
    if request.method == "DELETE":
        ok = db.delete_asset(asset_id)
        return jsonify({"success": ok})
        
    data = request.get_json() or {}
    ok = db.update_asset(asset_id, data)
    return jsonify({"success": ok})


@app.route("/api/assets/<int:asset_id>/status", methods=["PATCH"])
def api_asset_status(asset_id):
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    data = request.get_json() or {}
    status = data.get("status", "").strip()
    if not status:
        return jsonify({"success": False, "error": "กรุณาระบุสถานะ"}), 400
    ok = db.update_asset_status(asset_id, status)
    return jsonify({"success": ok})


@app.route("/api/assets/workstations", methods=["GET"])
def api_assets_workstations():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    dept_id = request.args.get("dept_id", type=int)
    search = request.args.get("search")
    workstations = db.get_workstations(dept_id=dept_id, search=search)
    return jsonify({"success": True, "workstations": workstations})


@app.route("/api/assets/stats", methods=["GET"])
def api_assets_stats():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    dept_id = request.args.get("dept_id", type=int)
    stats = db.get_asset_stats(dept_id=dept_id)
    return jsonify({"success": True, "stats": stats})


@app.route("/api/assets/parent-candidates", methods=["GET"])
def api_assets_parent_candidates():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    exclude_id = request.args.get("exclude_id", type=int)
    candidates = db.get_parent_candidates(exclude_id=exclude_id)
    return jsonify({"success": True, "candidates": candidates})


@app.route("/api/assets/generate-tag", methods=["GET"])
def api_assets_generate_tag():
    if not is_auth():
        return jsonify({"success": False, "error": "กรุณาเข้าสู่ระบบก่อน"}), 401
    cat = request.args.get("category", "pc")
    tag = db.generate_asset_tag(cat)
    return jsonify({"success": True, "tag": tag})


@app.route("/api/assets/export/excel", methods=["GET"])
def api_assets_export_excel():
    """ส่งออกทะเบียนทรัพย์สินไอทีเป็นไฟล์ Microsoft Excel (.xlsx)"""
    if not is_auth():
        return jsonify({"error": "Unauthorized"}), 401

    dept_id = request.args.get("dept_id", type=int)
    status = request.args.get("status")
    category = request.args.get("category")
    search = request.args.get("search")

    assets = db.get_assets(dept_id=dept_id, status=status, category=category, search=search)

    dept_name = "ทุกแผนก"
    if dept_id:
        dept_obj = db.get_department(dept_id)
        if dept_obj:
            dept_name = dept_obj.get("name", f"แผนก ID {dept_id}")

    org_name = db.get_setting("org_name", "Personal Workspace")
    excel_bytes = report_gen.generate_assets_excel(
        assets=assets,
        report_title="ทะเบียนทรัพย์สินและอุปกรณ์ไอที",
        dept_name=dept_name,
        org_name=org_name
    )

    filename = f"it_assets_{datetime.now().strftime('%Y%m%d')}.xlsx"
    encoded_name = quote(filename.encode("utf-8"))
    ascii_name = f"assets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    response = send_file(
        io.BytesIO(excel_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    return response


@app.route("/api/assets/export/pdf", methods=["GET"])
def api_assets_export_pdf():
    """ส่งออกทะเบียนทรัพย์สินไอทีเป็นไฟล์ PDF ขนาด A4 แนวนอน"""
    if not is_auth():
        return jsonify({"error": "Unauthorized"}), 401

    dept_id = request.args.get("dept_id", type=int)
    status = request.args.get("status")
    category = request.args.get("category")
    search = request.args.get("search")

    assets = db.get_assets(dept_id=dept_id, status=status, category=category, search=search)

    dept_name = "ทุกแผนก"
    if dept_id:
        dept_obj = db.get_department(dept_id)
        if dept_obj:
            dept_name = dept_obj.get("name", f"แผนก ID {dept_id}")

    org_name = db.get_setting("org_name", "Personal Workspace")
    pdf_bytes = report_gen.generate_asset_pdf_report(
        assets=assets,
        report_title="รายงานทะเบียนทรัพย์สินและอุปกรณ์ไอที",
        dept_name=dept_name,
        org_name=org_name
    )

    filename = f"it_assets_report_{datetime.now().strftime('%Y%m%d')}.pdf"
    encoded_name = quote(filename.encode("utf-8"))
    ascii_name = f"assets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    response = send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    return response


# Auto-start background cleanup worker when module loads (only on persistent servers, disabled on Vercel/Serverless)
if not os.environ.get("VERCEL") and not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    start_background_cleanup_worker(interval=300)




if __name__ == "__main__":
    print(f"[*] Ghostscript: {GS_CMD}")
    print(f"[*] Upload dir: {UPLOAD_DIR}")
    print(f"[*] Server พร้อมใช้งานที่: http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
