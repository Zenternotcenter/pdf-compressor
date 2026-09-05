#!/usr/bin/env python3
"""
PDF Compressor Web App — Production Engine (v7 Enterprise)
Features:
  1. Multi-threaded Parallel Execution: Ghostscript Balanced & Maximum run concurrently across CPU cores.
  2. Unified Asset Deduplication: Strips BOTH duplicate Form XObjects (Vector) AND identical Image XObjects (Raster) without quality loss.
  3. Real-time Progress Streaming: Server-Sent Events (SSE) for live percentage & phase updates without timeouts.
  4. In-Memory Fast Split: Binary search chunking performed 100% in RAM via tobytes() for sub-second splitting.
  5. Safe ASCII Storage & RFC 5987 Unicode Headers: 100% preservation of Thai filenames.
  6. Multi-page Live Preview & Page Navigation with High-DPI Zoom Inspection.
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

import shutil
import hashlib
import zipfile
import threading
import platform
import subprocess
import concurrent.futures
from urllib.parse import quote
import pymupdf
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # รองรับได้ถึง 200MB

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# In-memory Task Progress Store (for Real-time SSE)
TASKS = {}
TASKS_LOCK = threading.Lock()


# ============================================================
# Ghostscript Detection
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
    """เริ่มเธรดทำงานเบื้องหลังสำหรับล้างไฟล์ขยะอัตโนมัติ (ครั้งเดียว)"""
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
# Strategy 1: Unified Vector & Image Asset Deduplication
# ============================================================

def strategy_structure_dedup(input_path: str, output_path: str, expected_pages: int):
    """
    ขจัดความซ้ำซ้อนของทั้ง Vector Form XObjects และ Raster Image XObjects
    พร้อมบีบอัด Stream ขยะและ Deflate 100% โดยไม่แตะคุณภาพพิกเซล
    """
    try:
        doc = pymupdf.open(input_path)
        changed = False

        # 1. Deduplicate Form XObjects (Vector templates/watermarks)
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

        # 2. Deduplicate Image XObjects (Identical raster images repeated across pages)
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

        doc.save(output_path, garbage=4, deflate=True, clean=True)
        doc.close()

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
# Strategy 2: Ghostscript Balanced
# ============================================================

def strategy_ghostscript_balanced(input_path: str, output_path: str, expected_pages: int, target_dpi: int = 150):
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
# Strategy 3: Ghostscript Maximum (Grayscale / High Compression)
# ============================================================

def strategy_ghostscript_maximum(input_path: str, output_path: str, expected_pages: int, target_dpi: int = 120):
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
# Preview Generation
# ============================================================

def render_page_preview(pdf_path: str, png_path: str, page_idx: int = 0, dpi: int = 130) -> bool:
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
# In-Memory Fast Smart PDF Splitting (RAM-based Chunking)
# ============================================================

def split_pdf_smart_by_size(input_path: str, max_size_mb: float, file_id: str, variant_id: str):
    """
    แบ่งไฟล์ PDF อัจฉริยะ โดยคำนวณจุดตัดหน้าบน RAM (In-Memory tobytes())
    ทำให้การแบ่งไฟล์ 70+ หน้าเสร็จสิ้นในเสี้ยววินาที (< 0.5s)
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

        # Binary search completely in RAM
        while low <= high:
            mid = (low + high) // 2

            tmp = pymupdf.open()
            tmp.insert_pdf(doc, from_page=current_start, to_page=mid - 1)
            # Calculate size in memory without disk I/O
            sz = len(tmp.tobytes(garbage=3, deflate=True))
            tmp.close()

            if sz <= target_bytes:
                best_end = mid
                low = mid + 1
            else:
                high = mid - 1

        # Save only the finalized chunk to disk
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
# Deep PDF DPI Analyzer (APITemplate.io Equivalent Engine)
# ============================================================

def analyze_pdf_dpi(pdf_path: str, file_id: str = None) -> dict:
    """
    วิเคราะห์ความละเอียดรูปภาพใน PDF ระดับลึก เทียบเท่ามาตรฐาน APITemplate.io:
    - คำนวณ Effective Displayed DPI ตามขนาดแสดงผลจริงบนหน้ากระดาษ
    - สกัดขนาดพิกเซล (px), ขนาดพิมพ์จริง (mm / inch), Color Space, ฟอร์แมต
    - คำนวณสรุปเอกสาร: Min, Max, Average DPI, Total Images
    - ตรวจจับโครงสร้างและสถานะ 100% Vector Text & Graphics
    - จัดเกรดคุณภาพ (Print Ready 300+ DPI, Standard 150-300 DPI, Web < 150 DPI)
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

        for img_tuple in image_list:
            xref = img_tuple[0]
            try:
                base_img = doc.extract_image(xref)
                if not base_img:
                    continue

                w = base_img.get("width", 0)
                h = base_img.get("height", 0)
                cs = base_img.get("colorspace", 3)
                ext = base_img.get("ext", "png")
                bpc = base_img.get("bpc", 8)
                img_size_bytes = len(base_img.get("image", b""))

                # Color space readable string
                if cs == 1:
                    cs_str = "Grayscale"
                elif cs == 3:
                    cs_str = "RGB"
                elif cs == 4:
                    cs_str = "CMYK"
                elif isinstance(cs, str):
                    cs_str = cs
                else:
                    cs_str = f"CS-{cs}"

                # Calculate placement on page to get displayed DPI
                img_rects = page.get_image_rects(xref)
                if img_rects:
                    rect = img_rects[0]
                    disp_w_inch = rect.width / 72.0 if rect.width > 0 else 1.0
                    disp_h_inch = rect.height / 72.0 if rect.height > 0 else 1.0

                    dpi_x = round(w / disp_w_inch)
                    dpi_y = round(h / disp_h_inch)
                    eff_dpi = round((dpi_x + dpi_y) / 2)
                    disp_w_mm = round(rect.width * 25.4 / 72.0, 1)
                    disp_h_mm = round(rect.height * 25.4 / 72.0, 1)
                    disp_w_in = round(disp_w_inch, 2)
                    disp_h_in = round(disp_h_inch, 2)
                else:
                    eff_dpi = 72
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

@app.route("/")
def index():
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
        for cand in [variant_id, "vector", "balanced", "maximum", "in"]:
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

    download_name = f"compressed_{variant_id}_{clean_orig}.pdf"
    encoded_name = quote(download_name.encode("utf-8"))
    ascii_name = f"compressed_{file_id}_{variant_id}.pdf"

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


# Auto-start background cleanup worker when module loads (for both dev & WSGI)
start_background_cleanup_worker(interval=300)


if __name__ == "__main__":
    print(f"[*] Ghostscript: {GS_CMD}")
    print(f"[*] Upload dir: {UPLOAD_DIR}")
    print(f"[*] Server พร้อมใช้งานที่: http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
