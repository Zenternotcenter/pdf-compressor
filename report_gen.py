#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
========================================================================================
โมดูลสร้างรายงานเอกสาร PDF (PDF Report Generator Engine)
========================================================================================
วัตถุประสงค์:
  - สร้างรายงานสรุปการปฏิบัติงานประจำวัน (Daily Worklog Report) รูปแบบไฟล์ PDF ขนาด A4
  - รองรับการแสดงผลภาษาไทยสมบูรณ์แบบ (แก้ปัญหาวรรณยุกต์จม และตัวอักษรสี่เหลี่ยม Tofu)
  - คำนวณสถิติภาพรวมอัตโนมัติ (งานทั้งหมด, เสร็จสิ้น, กำลังทำ, คงค้าง, % ความก้าวหน้า)
  - แบ่งหน้าอัตโนมัติ (Auto Pagination) พร้อมหัวตารางซ้ำในทุกหน้า และเลขหน้ากำกับ
========================================================================================
"""

import os
import io
import platform
from datetime import datetime

try:
    import pymupdf
except Exception:
    pymupdf = None


def get_thai_font_paths():
    """
    ค้นหาตำแหน่งของฟอนต์ TrueType/OpenType ภาษาไทย (ปกติ และ ตัวหนา)
    โดยให้ความสำคัญกับฟอนต์มาตรฐาน Sarabun ที่ Bundle ไว้ในโปรเจกต์ก่อน (fonts/)
    เพื่อรองรับการรันบน Vercel Serverless (Linux Container ที่ไม่มีฟอนต์ไทยในระบบ)
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_reg = os.path.join(base_dir, "fonts", "Sarabun-Regular.ttf")
    bundled_bold = os.path.join(base_dir, "fonts", "Sarabun-Bold.ttf")
    if os.path.exists(bundled_reg):
        bold = bundled_bold if os.path.exists(bundled_bold) else bundled_reg
        return bundled_reg, bold

    candidates = [
        (r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\tahomabd.ttf"),
        (r"C:\Windows\Fonts\cordia.ttc", r"C:\Windows\Fonts\cordia.ttc"),
        ("/usr/share/fonts/truetype/tlwg/Loma.ttf", "/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf"),
        ("/usr/share/fonts/truetype/tlwg/Garuda.ttf", "/usr/share/fonts/truetype/tlwg/Garuda-Bold.ttf"),
        ("/usr/share/fonts/truetype/tlwg/Waree.ttf", "/usr/share/fonts/truetype/tlwg/Waree-Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    for reg, bld in candidates:
        if os.path.exists(reg):
            bold = bld if os.path.exists(bld) else reg
            return reg, bold
    return None, None


def get_thai_font_path():
    """ส่งคืน Path ฟอนต์ภาษาไทยปกติ"""
    reg, _ = get_thai_font_paths()
    return reg


def format_thai_date(date_str: str) -> str:
    """
    แปลงวันที่รูปแบบมาตรฐานสากล (YYYY-MM-DD) เป็นวันที่ภาษาไทยแบบเต็มพร้อมปี พ.ศ.
    
    ตัวอย่าง:
      "2026-09-10" -> "วันพฤหัสบดีที่ 10 กันยายน พ.ศ. 2569"
      
    Args:
      date_str (str): ข้อความวันที่ในรูปแบบ YYYY-MM-DD
      
    Returns:
      str: ข้อความวันที่ภาษาไทยแบบทางการ
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        thai_months = [
            "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
            "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
        ]
        thai_days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
        day_name = thai_days[dt.weekday()]
        thai_year = dt.year + 543  # แปลงปี ค.ศ. เป็น พ.ศ.
        return f"วัน{day_name}ที่ {dt.day} {thai_months[dt.month]} พ.ศ. {thai_year}"
    except Exception:
        # หากเกิดข้อผิดพลาดในการแปลง ให้ส่งคืนค่าเดิม
        return date_str


def generate_pdf_report(tasks: list, report_title="รายงานสรุปการปฏิบัติงาน", date_label="", user_name="ผู้ปฏิบัติงาน", org_name="บันทึกการปฏิบัติงานประจำวัน") -> bytes:
    """
    สร้างเอกสาร PDF สรุปรายงานการปฏิบัติงานด้วย PyMuPDF (MuPDF Engine)
    
    องค์ประกอบของรายงาน:
      1. Header Banner: แถบหัวรายงานสีน้ำเงิน แสดงชื่อรายงาน, วันที่, ผู้ปฏิบัติงาน และหน่วยงาน
      2. Summary Stats Card: การ์ดสรุปตัวเลขสถิติภาพรวม (รวม, เสร็จ, กำลังทำ, รอ, % ก้าวหน้า)
      3. Data Table: ตารางแสดงรายการงาน พร้อมแถบสีสถานะ (Badge) และสลับสีแถว (Zebra striping)
      4. Auto Pagination: หากรายการงานยาวเกิน 1 หน้า จะขึ้นหน้าใหม่อัตโนมัติและวาดหัวตารางซ้ำ
      5. Footer: แถบส่วนท้ายแสดงลิขสิทธิ์ระบบ วันเวลาที่พิมพ์ และหมายเลขหน้า (หน้า X / ทั้งหมด Y)
      
    Args:
      tasks (list): รายการข้อมูลงานแต่ละชิ้น (dict)
      report_title (str): ชื่อหัวเรื่องรายงาน
      date_label (str): ข้อความระบุวันที่
      user_name (str): ชื่อผู้ปฏิบัติงาน
      org_name (str): ชื่อหน่วยงานหรือโครงการ
      
    Returns:
      bytes: ไบนารีของไฟล์ PDF ที่สร้างเสร็จสมบูรณ์ พร้อมส่งออกให้ดาวน์โหลด
    """
    doc = pymupdf.open()
    font_path, bold_path = get_thai_font_paths()
    font_name = "thai_font" if font_path else "Helvetica"
    font_bold = "thai_bold" if font_path else "Helvetica-Bold"

    # กำหนดขนาดมาตรฐานของหน้ากระดาษ A4 ในหน่วย Point (72 points = 1 นิ้ว)
    PAGE_WIDTH = 595.32   # กว้าง 210 มม.
    PAGE_HEIGHT = 841.92  # สูง 297 มม.
    MARGIN = 36           # ระยะขอบกระดาษ 0.5 นิ้ว

    # คำนวณสถิติภาพรวมของงาน
    total = len(tasks)
    done_count = sum(1 for t in tasks if t.get("status") == "done")
    in_prog = sum(1 for t in tasks if t.get("status") == "in_progress")
    todo_count = total - done_count - in_prog
    pct = round(done_count / total * 100) if total > 0 else 0

    def setup_page():
        """ฟังก์ชันย่อยสำหรับสร้างหน้าใหม่และลงทะเบียนฟอนต์ภาษาไทย"""
        p = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        if font_path:
            p.insert_font(fontname=font_name, fontfile=font_path)
            if bold_path and os.path.exists(bold_path):
                p.insert_font(fontname=font_bold, fontfile=bold_path)
            else:
                p.insert_font(fontname=font_bold, fontfile=font_path)
        return p

    # เริ่มต้นสร้างหน้าแรก
    page = setup_page()
    y = MARGIN + 10

    # ------------------------------------------------------------
    # ส่วนที่ 1: แถบหัวกระดาษ (Header Banner)
    # ------------------------------------------------------------
    header_rect = pymupdf.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 68)
    # วาดกรอบสี่เหลี่ยมพื้นหลังสีฟ้าอ่อน พร้อมเส้นขอบสีน้ำเงินเข้ม
    page.draw_rect(header_rect, color=(0.15, 0.35, 0.75), fill=(0.95, 0.97, 1.0), width=1.2)
    
    page.insert_text((MARGIN + 16, y + 26), report_title, fontname=font_bold, fontsize=16, color=(0.1, 0.2, 0.5))
    page.insert_text((MARGIN + 16, y + 44), f"วันที่: {date_label}", fontname=font_name, fontsize=11, color=(0.25, 0.3, 0.4))
    page.insert_text((MARGIN + 16, y + 59), f"ผู้ปฏิบัติงาน: {user_name}  |  หน่วยงาน/โครงการ: {org_name}", fontname=font_name, fontsize=10, color=(0.35, 0.4, 0.45))
    
    y += 82

    # ------------------------------------------------------------
    # ส่วนที่ 2: การ์ดสรุปสถิติงาน (Summary Stats Card)
    # ------------------------------------------------------------
    stats_rect = pymupdf.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 42)
    page.draw_rect(stats_rect, color=(0.85, 0.88, 0.92), fill=(0.98, 0.98, 0.99), width=0.8)
    
    stat_items = [
        (f"งานทั้งหมด: {total} รายการ", (0.2, 0.2, 0.2)),
        (f"เสร็จสิ้น: {done_count} รายการ", (0.1, 0.6, 0.2)),
        (f"กำลังดำเนินการ: {in_prog} รายการ", (0.85, 0.55, 0.0)),
        (f"รอจัดการ: {todo_count} รายการ", (0.5, 0.5, 0.5)),
        (f"ความคืบหน้า: {pct}%", (0.1, 0.35, 0.75)),
    ]
    
    sx = MARGIN + 12
    for text, col in stat_items:
        page.insert_text((sx, y + 25), text, fontname=font_bold if "ความคืบหน้า" in text else font_name, fontsize=10, color=col)
        sx += 102

    y += 54

    # ------------------------------------------------------------
    # ส่วนที่ 3: หัวตารางข้อมูล (Table Headers)
    # ------------------------------------------------------------
    cols = [
        {"name": "#", "w": 28, "align": "center"},
        {"name": "รายการงาน / กิจกรรม", "w": 265, "align": "left"},
        {"name": "หมวดหมู่", "w": 80, "align": "center"},
        {"name": "เวลา", "w": 65, "align": "center"},
        {"name": "สถานะ", "w": 85, "align": "center"},
    ]
    
    table_w = sum(c["w"] for c in cols)
    th_rect = pymupdf.Rect(MARGIN, y, MARGIN + table_w, y + 24)
    page.draw_rect(th_rect, color=(0.2, 0.4, 0.8), fill=(0.2, 0.4, 0.8))
    
    cx = MARGIN
    for c in cols:
        page.insert_text((cx + 6, y + 16), c["name"], fontname=font_bold, fontsize=10, color=(1, 1, 1))
        cx += c["w"]
        
    y += 24

    # ------------------------------------------------------------
    # ส่วนที่ 4: แถวข้อมูลในตาราง (Table Rows & Pagination)
    # ------------------------------------------------------------
    if not tasks:
        # กรณีไม่มีข้อมูลในวันดังกล่าว
        empty_rect = pymupdf.Rect(MARGIN, y, MARGIN + table_w, y + 40)
        page.draw_rect(empty_rect, color=(0.88, 0.9, 0.93), fill=(1, 1, 1), width=0.5)
        page.insert_text((MARGIN + 180, y + 25), "— ไม่มีรายการงานในวันที่ระบุ —", fontname=font_name, fontsize=11, color=(0.6, 0.6, 0.6))
        y += 40
    else:
        for idx, task in enumerate(tasks, 1):
            title = task.get("title", "")
            notes = task.get("notes", "")
            category = task.get("category", "ทั่วไป")
            time_spent = task.get("time_spent", "-") or "-"
            status = task.get("status", "todo")
            priority = task.get("priority", "normal")

            # กำหนดสีและข้อความตามสถานะงาน
            status_map = {
                "done": ("เสร็จสิ้น", (0.1, 0.6, 0.2), (0.9, 0.98, 0.92)),
                "in_progress": ("กำลังทำ", (0.85, 0.5, 0.0), (1.0, 0.96, 0.88)),
                "todo": ("ยังไม่เสร็จ", (0.5, 0.5, 0.5), (0.95, 0.95, 0.95)),
            }
            status_text, status_col, status_bg = status_map.get(status, ("ทั่วไป", (0.3, 0.3, 0.3), (1, 1, 1)))

            # คำนวณความสูงของแต่ละแถว (หากมีหมายเหตุจะขยายความสูงแถว)
            row_h = 24
            if notes:
                row_h = 36

            # ตรวจสอบว่าพื้นที่กระดาษพอหรือไม่ หากใกล้ตกขอบล่าง ให้ขึ้นหน้าใหม่
            if y + row_h > PAGE_HEIGHT - MARGIN - 30:
                page = setup_page()
                y = MARGIN + 20
                # วาดหัวตารางซ้ำในหน้าใหม่เพื่อความต่อเนื่อง
                th_rect = pymupdf.Rect(MARGIN, y, MARGIN + table_w, y + 22)
                page.draw_rect(th_rect, color=(0.2, 0.4, 0.8), fill=(0.2, 0.4, 0.8))
                cx = MARGIN
                for c in cols:
                    page.insert_text((cx + 6, y + 15), c["name"], fontname=font_bold, fontsize=10, color=(1, 1, 1))
                    cx += c["w"]
                y += 22

            # วาดพื้นหลังสลับสีเพื่อให้อ่านง่าย (Zebra Striping)
            bg_col = (0.97, 0.98, 1.0) if idx % 2 == 0 else (1.0, 1.0, 1.0)
            row_rect = pymupdf.Rect(MARGIN, y, MARGIN + table_w, y + row_h)
            page.draw_rect(row_rect, color=(0.88, 0.9, 0.93), fill=bg_col, width=0.5)

            # คอลัมน์ที่ 1: ลำดับที่
            page.insert_text((MARGIN + 8, y + 16), str(idx), fontname=font_name, fontsize=9.5, color=(0.4, 0.4, 0.4))
            
            # คอลัมน์ที่ 2: ชื่องาน + บันทึกหมายเหตุ
            tx = MARGIN + cols[0]["w"] + 6
            page.insert_text((tx, y + 16), title[:52] + ("..." if len(title) > 52 else ""), fontname=font_bold if priority in ["high", "urgent"] else font_name, fontsize=10, color=(0.8, 0.1, 0.1) if priority == "urgent" else (0.15, 0.15, 0.15))
            if notes:
                page.insert_text((tx, y + 30), f"หมายเหตุ: {notes[:60]}", fontname=font_name, fontsize=8.5, color=(0.45, 0.5, 0.55))

            # คอลัมน์ที่ 3: หมวดหมู่
            cx = MARGIN + cols[0]["w"] + cols[1]["w"]
            page.insert_text((cx + 8, y + 16), category, fontname=font_name, fontsize=9.5, color=(0.3, 0.35, 0.45))

            # คอลัมน์ที่ 4: เวลาที่ใช้
            cx += cols[2]["w"]
            page.insert_text((cx + 10, y + 16), time_spent, fontname=font_name, fontsize=9.5, color=(0.4, 0.4, 0.4))

            # คอลัมน์ที่ 5: ป้ายสถานะ (Status Badge)
            cx += cols[3]["w"]
            badge_rect = pymupdf.Rect(cx + 6, y + 4, cx + cols[4]["w"] - 6, y + 19)
            page.draw_rect(badge_rect, color=status_col, fill=status_bg, width=0.6)
            page.insert_text((cx + 16, y + 15), status_text, fontname=font_bold, fontsize=9, color=status_col)

            y += row_h

    # ------------------------------------------------------------
    # ส่วนที่ 5: ท้ายกระดาษ (Page Footers) สำหรับทุกหน้า
    # ------------------------------------------------------------
    total_pages = len(doc)
    for pno, p in enumerate(doc, 1):
        footer_y = PAGE_HEIGHT - MARGIN
        # เส้นคั่นส่วนท้าย
        p.draw_line(pymupdf.Point(MARGIN, footer_y - 12), pymupdf.Point(PAGE_WIDTH - MARGIN, footer_y - 12), color=(0.8, 0.85, 0.9), width=0.8)
        # ข้อความระบุระบบและวันเวลาที่พิมพ์
        p.insert_text((MARGIN, footer_y), f"สร้างโดย Personal Workspace  |  พิมพ์เมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')}", fontname=font_name, fontsize=8.5, color=(0.5, 0.55, 0.6))
        # หมายเลขหน้าปัจจุบัน / จำนวนหน้าทั้งหมด
        p.insert_text((PAGE_WIDTH - MARGIN - 70, footer_y), f"หน้า {pno} / {total_pages}", fontname=font_name, fontsize=8.5, color=(0.5, 0.55, 0.6))

    # แปลงเอกสารเป็นไบต์ในหน่วยความจำ RAM แล้วปิดเอกสาร
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ============================================================
# พจนานุกรมแปลคำศัพท์สำหรับระบบทรัพย์สินและงาน
# ============================================================
CATEGORY_MAP = {
    "pc": "คอมพิวเตอร์ (PC)",
    "laptop": "โน้ตบุ๊ก (Laptop)",
    "monitor": "จอภาพ (Monitor)",
    "ups": "เครื่องสำรองไฟ (UPS)",
    "printer": "เครื่องพิมพ์ (Printer)",
    "network": "อุปกรณ์เครือข่าย (Network)",
    "other": "อุปกรณ์อื่นๆ"
}

STATUS_MAP = {
    "in_use": "ใช้งานปกติ",
    "spare": "เครื่องสำรอง",
    "repair": "ส่งซ่อม",
    "broken": "ชำรุด",
    "disposed": "ปลดระวาง"
}

TASK_STATUS_MAP = {
    "done": "เสร็จสิ้น",
    "in_progress": "กำลังดำเนินการ",
    "todo": "รอดำเนินการ"
}

TASK_PRIORITY_MAP = {
    "urgent": "ด่วนที่สุด",
    "high": "สำคัญสูง",
    "normal": "ปกติ",
    "low": "ต่ำ"
}


# ============================================================
# 1. ฟังก์ชันสร้างไฟล์ Excel สำหรับงานประจำวัน (Worklog Excel)
# ============================================================
def generate_tasks_excel(tasks: list, report_title="รายงานสรุปผลการปฏิบัติงาน", date_label="", user_name="ผู้ปฏิบัติงาน", org_name="บันทึกการปฏิบัติงานประจำวัน") -> bytes:
    """
    สร้างไฟล์ Microsoft Excel (.xlsx) สรุปการปฏิบัติงานประจำวัน
    ทำงานบนหน่วยความจำ RAM 100% ผ่าน io.BytesIO เพื่อรองรับ Serverless / Vercel
    """
    import xlsxwriter

    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output, {'in_memory': True})
    ws = wb.add_worksheet("Worklog Report")
    ws.set_tab_color("#2563eb")

    # กำหนดรูปแบบ Format ต่างๆ
    title_fmt = wb.add_format({
        'bold': True, 'font_size': 16, 'font_name': 'Segoe UI',
        'font_color': '#1e3a8a', 'valign': 'vcenter'
    })
    meta_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI', 'font_color': '#475569', 'valign': 'vcenter'
    })
    meta_bold_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI', 'font_color': '#1e293b', 'valign': 'vcenter'
    })

    stat_box_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#f1f5f9', 'border': 1, 'border_color': '#cbd5e1',
        'align': 'center', 'valign': 'vcenter'
    })

    header_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#1e40af', 'font_color': '#ffffff',
        'border': 1, 'border_color': '#1d4ed8',
        'align': 'center', 'valign': 'vcenter'
    })

    row_even_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#f8fafc', 'border': 1, 'border_color': '#e2e8f0',
        'valign': 'vcenter'
    })
    row_odd_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#ffffff', 'border': 1, 'border_color': '#e2e8f0',
        'valign': 'vcenter'
    })

    center_even_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#f8fafc', 'border': 1, 'border_color': '#e2e8f0',
        'align': 'center', 'valign': 'vcenter'
    })
    center_odd_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#ffffff', 'border': 1, 'border_color': '#e2e8f0',
        'align': 'center', 'valign': 'vcenter'
    })

    # สไตล์สถานะ
    status_done_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#dcfce7', 'font_color': '#15803d',
        'border': 1, 'border_color': '#bbf7d0', 'align': 'center', 'valign': 'vcenter'
    })
    status_prog_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#dbeafe', 'font_color': '#1d4ed8',
        'border': 1, 'border_color': '#bfdbfe', 'align': 'center', 'valign': 'vcenter'
    })
    status_todo_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#fef3c7', 'font_color': '#b45309',
        'border': 1, 'border_color': '#fde68a', 'align': 'center', 'valign': 'vcenter'
    })

    # สไตล์ความสำคัญ
    urgent_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI',
        'font_color': '#dc2626', 'align': 'center', 'valign': 'vcenter',
        'border': 1, 'border_color': '#e2e8f0'
    })

    # เขียนหัวรายงาน
    ws.merge_range('A1:I1', report_title, title_fmt)
    ws.set_row(0, 28)

    ws.write('A2', "วันที่ / ช่วงเวลา:", meta_bold_fmt)
    ws.write('B2', date_label or datetime.now().strftime('%d/%m/%Y'), meta_fmt)
    ws.write('D2', "ผู้ปฏิบัติงาน:", meta_bold_fmt)
    ws.write('E2', user_name, meta_fmt)

    ws.write('A3', "หน่วยงาน / โครงการ:", meta_bold_fmt)
    ws.write('B3', org_name, meta_fmt)
    ws.write('D3', "สร้างรายงานเมื่อ:", meta_bold_fmt)
    ws.write('E3', datetime.now().strftime("%d/%m/%Y %H:%M"), meta_fmt)

    # สรุปตัวเลขสถิติ
    total = len(tasks)
    done_count = sum(1 for t in tasks if t.get("status") == "done")
    prog_count = sum(1 for t in tasks if t.get("status") == "in_progress")
    todo_count = total - done_count - prog_count
    pct = round((done_count / total * 100)) if total > 0 else 0

    ws.write('A5', f"งานทั้งหมด: {total} รายการ", stat_box_fmt)
    ws.write('B5', f"เสร็จสิ้น: {done_count} รายการ", stat_box_fmt)
    ws.write('C5', f"กำลังทำ: {prog_count} รายการ", stat_box_fmt)
    ws.write('D5', f"รอดำเนินการ: {todo_count} รายการ", stat_box_fmt)
    ws.write('E5', f"ความก้าวหน้า: {pct}%", stat_box_fmt)
    ws.set_row(4, 22)

    # หัวตารางข้อมูล
    headers = [
        ("ลำดับ", 8),
        ("วันที่", 14),
        ("ชื่องาน / รายละเอียดงาน", 42),
        ("หมวดหมู่", 18),
        ("ความสำคัญ", 14),
        ("สถานะ", 16),
        ("เวลาที่ใช้", 12),
        ("ผู้บันทึก", 18),
        ("หมายเหตุ", 30)
    ]

    start_row = 6
    ws.set_row(start_row, 24)
    for col_idx, (h_title, _) in enumerate(headers):
        ws.write(start_row, col_idx, h_title, header_fmt)

    # วนลูปเขียนข้อมูลงาน
    for i, t in enumerate(tasks, 1):
        curr_row = start_row + i
        ws.set_row(curr_row, 22)
        is_even = (i % 2 == 0)
        c_fmt = center_even_fmt if is_even else center_odd_fmt
        r_fmt = row_even_fmt if is_even else row_odd_fmt

        ws.write(curr_row, 0, i, c_fmt)
        ws.write(curr_row, 1, t.get("date", ""), c_fmt)
        ws.write(curr_row, 2, t.get("title", ""), r_fmt)
        ws.write(curr_row, 3, t.get("category", "ทั่วไป"), c_fmt)

        prio_raw = t.get("priority", "normal")
        prio_th = TASK_PRIORITY_MAP.get(prio_raw, prio_raw)
        if prio_raw == "urgent":
            ws.write(curr_row, 4, prio_th, urgent_fmt)
        else:
            ws.write(curr_row, 4, prio_th, c_fmt)

        st_raw = t.get("status", "todo")
        st_th = TASK_STATUS_MAP.get(st_raw, st_raw)
        if st_raw == "done":
            ws.write(curr_row, 5, st_th, status_done_fmt)
        elif st_raw == "in_progress":
            ws.write(curr_row, 5, st_th, status_prog_fmt)
        else:
            ws.write(curr_row, 5, st_th, status_todo_fmt)

        ws.write(curr_row, 6, t.get("time_spent", "") or "-", c_fmt)
        ws.write(curr_row, 7, t.get("creator_name", "") or user_name, r_fmt)
        ws.write(curr_row, 8, t.get("notes", "") or "", r_fmt)

    # ตั้งค่าความกว้างคอลัมน์
    for col_idx, (_, width) in enumerate(headers):
        ws.set_column(col_idx, col_idx, width)

    # ตรึงหัวตารางไว้แถวที่ 7
    ws.freeze_panes(start_row + 1, 0)

    wb.close()
    return output.getvalue()


# ============================================================
# 2. ฟังก์ชันสร้างไฟล์ Excel สำหรับทรัพย์สินไอที (IT Assets Excel)
# ============================================================
def generate_assets_excel(assets: list, report_title="ทะเบียนทรัพย์สินและอุปกรณ์ไอที", dept_name="ทุกแผนก", org_name="Personal Workspace") -> bytes:
    """
    สร้างไฟล์ Microsoft Excel (.xlsx) ทะเบียนทรัพย์สินและอุปกรณ์ไอที
    ทำงานบนหน่วยความจำ RAM 100% ผ่าน io.BytesIO
    """
    import xlsxwriter

    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output, {'in_memory': True})
    ws = wb.add_worksheet("IT Assets")
    ws.set_tab_color("#059669")

    # Format ต่างๆ
    title_fmt = wb.add_format({
        'bold': True, 'font_size': 16, 'font_name': 'Segoe UI',
        'font_color': '#065f46', 'valign': 'vcenter'
    })
    meta_bold_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI', 'font_color': '#1e293b', 'valign': 'vcenter'
    })
    meta_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI', 'font_color': '#475569', 'valign': 'vcenter'
    })

    stat_box_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#ecfdf5', 'border': 1, 'border_color': '#a7f3d0',
        'align': 'center', 'valign': 'vcenter'
    })

    header_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#047857', 'font_color': '#ffffff',
        'border': 1, 'border_color': '#065f46',
        'align': 'center', 'valign': 'vcenter'
    })

    tag_even_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Consolas',
        'bg_color': '#f0fdf4', 'font_color': '#15803d',
        'border': 1, 'border_color': '#e2e8f0', 'align': 'center', 'valign': 'vcenter'
    })
    tag_odd_fmt = wb.add_format({
        'bold': True, 'font_size': 10, 'font_name': 'Consolas',
        'bg_color': '#ffffff', 'font_color': '#15803d',
        'border': 1, 'border_color': '#e2e8f0', 'align': 'center', 'valign': 'vcenter'
    })

    row_even_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#f8fafc', 'border': 1, 'border_color': '#e2e8f0', 'valign': 'vcenter'
    })
    row_odd_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#ffffff', 'border': 1, 'border_color': '#e2e8f0', 'valign': 'vcenter'
    })

    center_even_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#f8fafc', 'border': 1, 'border_color': '#e2e8f0',
        'align': 'center', 'valign': 'vcenter'
    })
    center_odd_fmt = wb.add_format({
        'font_size': 10, 'font_name': 'Segoe UI',
        'bg_color': '#ffffff', 'border': 1, 'border_color': '#e2e8f0',
        'align': 'center', 'valign': 'vcenter'
    })

    status_in_use_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#dcfce7', 'font_color': '#15803d',
        'border': 1, 'border_color': '#bbf7d0', 'align': 'center', 'valign': 'vcenter'
    })
    status_spare_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#e0f2fe', 'font_color': '#0369a1',
        'border': 1, 'border_color': '#bae6fd', 'align': 'center', 'valign': 'vcenter'
    })
    status_repair_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#fef3c7', 'font_color': '#b45309',
        'border': 1, 'border_color': '#fde68a', 'align': 'center', 'valign': 'vcenter'
    })
    status_other_fmt = wb.add_format({
        'bold': True, 'font_size': 9.5, 'font_name': 'Segoe UI',
        'bg_color': '#fee2e2', 'font_color': '#b91c1c',
        'border': 1, 'border_color': '#fecaca', 'align': 'center', 'valign': 'vcenter'
    })

    # ส่วนหัว
    ws.merge_range('A1:L1', report_title, title_fmt)
    ws.set_row(0, 28)

    ws.write('A2', "แผนก / ฝ่าย:", meta_bold_fmt)
    ws.write('B2', dept_name, meta_fmt)
    ws.write('E2', "หน่วยงาน / บริษัท:", meta_bold_fmt)
    ws.write('F2', org_name, meta_fmt)

    ws.write('A3', "ส่งออกรายงานเมื่อ:", meta_bold_fmt)
    ws.write('B3', datetime.now().strftime("%d/%m/%Y %H:%M"), meta_fmt)
    ws.write('E3', "จำนวนอุปกรณ์ที่แสดง:", meta_bold_fmt)
    ws.write('F3', f"{len(assets)} รายการ", meta_fmt)

    # สรุปสถิติ
    total = len(assets)
    in_use_count = sum(1 for a in assets if a.get("status") == "in_use")
    spare_count = sum(1 for a in assets if a.get("status") == "spare")
    repair_count = sum(1 for a in assets if a.get("status") == "repair")
    other_count = total - in_use_count - spare_count - repair_count

    ws.write('A5', f"อุปกรณ์ทั้งหมด: {total}", stat_box_fmt)
    ws.write('B5', f"ใช้งานปกติ: {in_use_count}", stat_box_fmt)
    ws.write('C5', f"เครื่องสำรอง: {spare_count}", stat_box_fmt)
    ws.write('D5', f"ส่งซ่อม: {repair_count}", stat_box_fmt)
    ws.write('E5', f"ชำรุด/ปลดระวาง: {other_count}", stat_box_fmt)
    ws.set_row(4, 22)

    # ตารางข้อมูล
    headers = [
        ("ลำดับ", 7),
        ("รหัสทรัพย์สิน (Asset Tag)", 16),
        ("ประเภท", 18),
        ("ยี่ห้อและรุ่น (Brand & Model)", 30),
        ("Serial Number (S/N)", 20),
        ("แผนก / สังกัด", 20),
        ("ผู้ถือครอง", 20),
        ("โต๊ะ / จุดติดตั้ง", 16),
        ("สถานะ", 15),
        ("ข้อมูลสเปก (Specs)", 35),
        ("IP / MAC Address", 22),
        ("วันหมดประกัน", 15),
        ("หมายเหตุ", 25)
    ]

    start_row = 6
    ws.set_row(start_row, 24)
    for col_idx, (h_title, _) in enumerate(headers):
        ws.write(start_row, col_idx, h_title, header_fmt)

    for i, a in enumerate(assets, 1):
        curr_row = start_row + i
        ws.set_row(curr_row, 22)
        is_even = (i % 2 == 0)
        c_fmt = center_even_fmt if is_even else center_odd_fmt
        r_fmt = row_even_fmt if is_even else row_odd_fmt
        tag_fmt = tag_even_fmt if is_even else tag_odd_fmt

        cat_raw = a.get("category", "")
        cat_th = CATEGORY_MAP.get(cat_raw, cat_raw)

        st_raw = a.get("status", "in_use")
        st_th = STATUS_MAP.get(st_raw, st_raw)

        if st_raw == "in_use":
            st_cell_fmt = status_in_use_fmt
        elif st_raw == "spare":
            st_cell_fmt = status_spare_fmt
        elif st_raw == "repair":
            st_cell_fmt = status_repair_fmt
        else:
            st_cell_fmt = status_other_fmt

        ip_mac = []
        if a.get("ip_address"): ip_mac.append(a["ip_address"])
        if a.get("mac_address"): ip_mac.append(a["mac_address"])
        ip_mac_str = " / ".join(ip_mac) if ip_mac else "-"

        ws.write(curr_row, 0, i, c_fmt)
        ws.write(curr_row, 1, a.get("asset_tag", "") or "-", tag_fmt)
        ws.write(curr_row, 2, cat_th, c_fmt)
        ws.write(curr_row, 3, a.get("brand_model", "") or "-", r_fmt)
        ws.write(curr_row, 4, a.get("serial_number", "") or "-", c_fmt)
        ws.write(curr_row, 5, a.get("department_name", "") or "-", r_fmt)
        ws.write(curr_row, 6, a.get("assigned_user", "") or "-", r_fmt)
        ws.write(curr_row, 7, a.get("workstation_label", "") or "-", c_fmt)
        ws.write(curr_row, 8, st_th, st_cell_fmt)
        ws.write(curr_row, 9, a.get("specs", "") or "-", r_fmt)
        ws.write(curr_row, 10, ip_mac_str, c_fmt)
        ws.write(curr_row, 11, a.get("warranty_expire", "") or "-", c_fmt)
        ws.write(curr_row, 12, a.get("notes", "") or "", r_fmt)

    for col_idx, (_, width) in enumerate(headers):
        ws.set_column(col_idx, col_idx, width)

    ws.freeze_panes(start_row + 1, 0)

    wb.close()
    return output.getvalue()


# ============================================================
# 3. ฟังก์ชันสร้างไฟล์ PDF สำหรับทรัพย์สินไอที (IT Assets PDF)
# ============================================================
def generate_asset_pdf_report(assets: list, report_title="รายงานทะเบียนทรัพย์สินและอุปกรณ์ไอที", dept_name="ทุกแผนก", org_name="Personal Workspace") -> bytes:
    """
    สร้างรายงานสรุปทะเบียนทรัพย์สินไอทีรูปแบบ PDF ขนาด A4 แนวนอน (Landscape)
    ด้วย PyMuPDF และระบบฟอนต์ไทยอัตโนมัติ
    """
    doc = pymupdf.open()
    font_path, bold_path = get_thai_font_paths()
    font_name = "thai_font" if font_path else "Helvetica"
    font_bold = "thai_bold" if font_path else "Helvetica-Bold"

    # A4 แนวนอน (Landscape: กว้าง 841.92, สูง 595.32 pt)
    PAGE_WIDTH = 841.92
    PAGE_HEIGHT = 595.32
    MARGIN = 32

    total = len(assets)
    in_use = sum(1 for a in assets if a.get("status") == "in_use")
    spare = sum(1 for a in assets if a.get("status") == "spare")
    repair = sum(1 for a in assets if a.get("status") == "repair")
    other = total - in_use - spare - repair

    def setup_page():
        p = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        if font_path:
            p.insert_font(fontname=font_name, fontfile=font_path)
            if bold_path and os.path.exists(bold_path):
                p.insert_font(fontname=font_bold, fontfile=bold_path)
            else:
                p.insert_font(fontname=font_bold, fontfile=font_path)
        return p

    page = setup_page()
    y = MARGIN + 6

    # แถบ Header
    header_rect = pymupdf.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 54)
    page.draw_rect(header_rect, color=(0.05, 0.45, 0.35), fill=(0.94, 0.98, 0.96), width=1.2)
    page.insert_text((MARGIN + 14, y + 22), report_title, fontname=font_bold, fontsize=15, color=(0.04, 0.35, 0.28))
    page.insert_text((MARGIN + 14, y + 42), f"แผนก/ฝ่าย: {dept_name}   |   หน่วยงาน: {org_name}   |   พิมพ์เมื่อ: {datetime.now().strftime('%d/%m/%Y %H:%M')}", fontname=font_name, fontsize=9.5, color=(0.2, 0.3, 0.3))

    y += 64

    # แถบสถิติ
    stats_rect = pymupdf.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 28)
    page.draw_rect(stats_rect, color=(0.8, 0.88, 0.84), fill=(0.98, 0.99, 0.98), width=0.8)

    stat_texts = [
        f"ทั้งหมด: {total} รายการ",
        f"ใช้งานปกติ: {in_use} รายการ",
        f"เครื่องสำรอง: {spare} รายการ",
        f"ส่งซ่อม: {repair} รายการ",
        f"ชำรุด/ปลดระวาง: {other} รายการ"
    ]
    sx = MARGIN + 14
    for st_t in stat_texts:
        page.insert_text((sx, y + 18), st_t, fontname=font_bold if "ทั้งหมด" in st_t else font_name, fontsize=9.5, color=(0.1, 0.2, 0.2))
        sx += 150

    y += 38

    # โครงสร้างตาราง
    cols = [
        {"name": "ลำดับ", "w": 36},
        {"name": "รหัสทรัพย์สิน", "w": 85},
        {"name": "ประเภท", "w": 90},
        {"name": "ยี่ห้อและรุ่น / S/N", "w": 200},
        {"name": "แผนก / โต๊ะทำงาน", "w": 135},
        {"name": "ผู้ถือครอง", "w": 130},
        {"name": "สถานะ", "w": 95}
    ]
    table_w = sum(c["w"] for c in cols)

    def draw_table_header(p, py):
        th_rect = pymupdf.Rect(MARGIN, py, MARGIN + table_w, py + 22)
        p.draw_rect(th_rect, color=(0.04, 0.4, 0.3), fill=(0.04, 0.4, 0.3))
        cx = MARGIN
        for c in cols:
            p.insert_text((cx + 6, py + 15), c["name"], fontname=font_bold, fontsize=9.5, color=(1, 1, 1))
            cx += c["w"]
        return py + 22

    y = draw_table_header(page, y)

    row_h = 32
    bottom_limit = PAGE_HEIGHT - MARGIN - 30

    if not assets:
        empty_rect = pymupdf.Rect(MARGIN, y, MARGIN + table_w, y + 40)
        page.draw_rect(empty_rect, color=(0.85, 0.88, 0.85), fill=(1, 1, 1), width=0.5)
        page.insert_text((MARGIN + 20, y + 25), "ไม่พบข้อมูลอุปกรณ์ตามเงื่อนไขที่เลือก", fontname=font_name, fontsize=11, color=(0.5, 0.5, 0.5))
    else:
        for idx, a in enumerate(assets, 1):
            if y + row_h > bottom_limit:
                page = setup_page()
                y = MARGIN + 10
                y = draw_table_header(page, y)

            bg_col = (0.96, 0.98, 0.97) if idx % 2 == 0 else (1.0, 1.0, 1.0)
            row_rect = pymupdf.Rect(MARGIN, y, MARGIN + table_w, y + row_h)
            page.draw_rect(row_rect, color=(0.86, 0.9, 0.88), fill=bg_col, width=0.5)

            # 1. ลำดับ
            page.insert_text((MARGIN + 8, y + 18), str(idx), fontname=font_name, fontsize=9, color=(0.4, 0.4, 0.4))

            # 2. Asset Tag
            cx = MARGIN + cols[0]["w"]
            tag_text = a.get("asset_tag", "") or "-"
            page.insert_text((cx + 6, y + 18), tag_text, fontname=font_bold, fontsize=9.5, color=(0.05, 0.45, 0.3))

            # 3. ประเภท
            cx += cols[1]["w"]
            cat_th = CATEGORY_MAP.get(a.get("category", ""), a.get("category", ""))
            page.insert_text((cx + 6, y + 18), cat_th[:20], fontname=font_name, fontsize=9, color=(0.2, 0.25, 0.3))

            # 4. ยี่ห้อและรุ่น + S/N
            cx += cols[2]["w"]
            bm = a.get("brand_model", "") or "-"
            sn = a.get("serial_number", "")
            page.insert_text((cx + 6, y + 14), bm[:36] + ("..." if len(bm) > 36 else ""), fontname=font_bold, fontsize=9.5, color=(0.1, 0.1, 0.1))
            if sn:
                page.insert_text((cx + 6, y + 26), f"S/N: {sn[:25]}", fontname=font_name, fontsize=8.5, color=(0.45, 0.5, 0.55))

            # 5. แผนก + โต๊ะทำงาน
            cx += cols[3]["w"]
            dept = a.get("department_name", "") or "-"
            desk = a.get("workstation_label", "")
            page.insert_text((cx + 6, y + 14), dept[:22], fontname=font_name, fontsize=9, color=(0.2, 0.25, 0.3))
            if desk:
                page.insert_text((cx + 6, y + 26), f"โต๊ะ: {desk[:18]}", fontname=font_name, fontsize=8.5, color=(0.4, 0.45, 0.5))

            # 6. ผู้ถือครอง
            cx += cols[4]["w"]
            user = a.get("assigned_user", "") or "-"
            page.insert_text((cx + 6, y + 18), user[:22], fontname=font_name, fontsize=9, color=(0.2, 0.2, 0.2))

            # 7. สถานะ Badge
            cx += cols[5]["w"]
            st_raw = a.get("status", "in_use")
            st_th = STATUS_MAP.get(st_raw, st_raw)
            if st_raw == "in_use":
                badge_c, badge_f = (0.1, 0.6, 0.2), (0.9, 0.98, 0.92)
            elif st_raw == "spare":
                badge_c, badge_f = (0.1, 0.4, 0.8), (0.92, 0.96, 1.0)
            elif st_raw == "repair":
                badge_c, badge_f = (0.8, 0.5, 0.0), (1.0, 0.96, 0.88)
            else:
                badge_c, badge_f = (0.8, 0.2, 0.2), (1.0, 0.92, 0.92)

            b_rect = pymupdf.Rect(cx + 4, y + 5, cx + cols[6]["w"] - 6, y + 22)
            page.draw_rect(b_rect, color=badge_c, fill=badge_f, width=0.6)
            page.insert_text((cx + 12, y + 17), st_th, fontname=font_bold, fontsize=8.5, color=badge_c)

            y += row_h

    # Footers
    total_pages = len(doc)
    for pno, p in enumerate(doc, 1):
        footer_y = PAGE_HEIGHT - MARGIN
        p.draw_line(pymupdf.Point(MARGIN, footer_y - 10), pymupdf.Point(PAGE_WIDTH - MARGIN, footer_y - 10), color=(0.8, 0.86, 0.82), width=0.8)
        p.insert_text((MARGIN, footer_y), f"ระบบ Personal Workspace IT Asset Suite  |  พิมพ์เมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')}", fontname=font_name, fontsize=8, color=(0.4, 0.45, 0.45))
        p.insert_text((PAGE_WIDTH - MARGIN - 70, footer_y), f"หน้า {pno} / {total_pages}", fontname=font_name, fontsize=8, color=(0.4, 0.45, 0.45))

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes

