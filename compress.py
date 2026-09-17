#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
========================================================================================
โปรแกรมบีบอัดไฟล์ PDF (PDF Compressor Engine) ด้วย Ghostscript
========================================================================================
วัตถุประสงค์:
  - ลดความละเอียดรูปภาพ (DPI) ให้อยู่ในระดับมาตรฐาน (ค่าเริ่มต้น 160 DPI หรือต่ำกว่า)
  - ปรับลดขนาดไฟล์ให้ไม่เกินขีดจำกัดที่กำหนด (เช่น เป้าหมาย 1.0 MB)
  - ใช้กลยุทธ์ Multi-Pass (ประมวลผลหลายระดับจากเบาไปหนัก) เพื่อรักษาคุณภาพสูงสุด

ฟีเจอร์หลัก:
  1. Automatic Ghostscript Detection: ค้นหา executable ของ Ghostscript ใน Windows/Linux อัตโนมัติ
  2. Multi-Pass Adaptive Compression: ปรับลดคุณภาพทีละระดับ หากได้ขนาดตามเป้าจะหยุดทันที
  3. Font Subsetting & Compression: ฝังและบีบอัดฟอนต์เฉพาะตัวอักษรที่ใช้จริง
  4. Non-Destructive Temporary File Management: จัดการไฟล์ชั่วคราวอย่างปลอดภัย ป้องกันไฟล์ต้นฉบับสูญหาย
========================================================================================
"""

import subprocess
import sys
import os
import shutil
import platform


def find_ghostscript() -> str:
    """
    ค้นหาตำแหน่งของโปรแกรม Ghostscript ในระบบปฏิบัติการโดยอัตโนมัติ
    
    ลำดับการทำงาน:
      1. หากเป็น Windows: ตรวจสอบในโฟลเดอร์มาตรฐาน Program Files และ Program Files (x86)
         ค้นหาไฟล์ 'gswin64c.exe' (Command-line version สำหรับ 64-bit)
      2. หากพบไฟล์ จะส่งคืน Absolute Path ทันที
      3. หากไม่พบในโฟลเดอร์มาตรฐาน จะ fallback คืนค่า 'gswin64c' เพื่อให้ระบบหาใน System PATH
      4. หากเป็น Linux หรือ macOS: ส่งคืน 'gs' เพื่อเรียกผ่าน PATH มาตรฐาน
      
    Returns:
      str: คำสั่งหรือ Path เต็มสำหรับเรียกใช้ Ghostscript
    """
    if platform.system() == "Windows":
        # ตรวจสอบใน Program Files ของ Windows ทั้ง 64-bit และ 32-bit
        candidates = []
        for base in [r"C:\Program Files\gs", r"C:\Program Files (x86)\gs"]:
            if os.path.isdir(base):
                for root, dirs, files in os.walk(base):
                    for f in files:
                        if f.lower() == "gswin64c.exe":
                            candidates.append(os.path.join(root, f))
        if candidates:
            return candidates[0]  # เลือก path แรกที่ตรวจพบ
        return "gswin64c"  # Fallback: หวังว่าผู้ใช้ตั้งค่าไว้ใน System PATH
    else:
        return "gs"  # สำหรับ Linux / Unix / macOS


# กำหนดตัวแปรส่วนกลางสำหรับคำสั่ง Ghostscript ที่ตรวจพบ
GS_CMD = find_ghostscript()


def run_gs(args: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """
    รันคำสั่ง Ghostscript ผ่าน Subprocess พร้อมจัดการ Timeout และการดักจับผลลัพธ์
    
    Args:
      args (list): รายการพารามิเตอร์หรือ Argument ที่จะส่งให้ Ghostscript
      timeout (int): เวลาสูงสุดที่อนุญาตให้ทำงาน (วินาที) ค่าเริ่มต้น 120 วินาที
      
    Returns:
      subprocess.CompletedProcess: ผลลัพธ์การรันคำสั่ง (returncode, stdout, stderr)
    """
    return subprocess.run([GS_CMD] + args, capture_output=True, text=True, timeout=timeout)


def compress_pdf(input_path: str, output_path: str, target_dpi: int = 160, max_size_mb: float = 1.0) -> dict:
    """
    บีบอัดไฟล์ PDF ด้วย Ghostscript แบบ Multi-Pass (Adaptive Multi-level)
    
    หลักการทำงาน:
      - นำไฟล์ PDF มาผ่านการบีบอัดทีละระดับ (Pass 1 ถึง Pass 4) จากความละเอียดสูงไปต่ำ
      - Pass 1: /ebook (DPI=160, คุณภาพรูปภาพ=75%) - เหมาะสำหรับอ่านบนจอและพิมพ์เอกสารทั่วไป
      - Pass 2: /screen (DPI=120, คุณภาพรูปภาพ=60%) - เหมาะสำหรับส่งอีเมลหรืออัปโหลดระบบราชการ
      - Pass 3: /screen (DPI=96, คุณภาพรูปภาพ=45%) - บีบอัดเข้มข้นสำหรับไฟล์ที่มีภาพถ่ายขนาดใหญ่
      - Pass 4: /screen (DPI=72, คุณภาพรูปภาพ=30%) - ระดับบีบอัดสูงสุดสำหรับไฟล์ที่ต้องการขนาดเล็กที่สุด
      - หาก Pass ใดสามารถทำให้ขนาดไฟล์ ≤ max_size_mb ได้ จะหยุดการทำงานทันที (Early Exit)
      
    Args:
      input_path (str): ตำแหน่งไฟล์ PDF ต้นฉบับ
      output_path (str): ตำแหน่งที่ต้องการบันทึกไฟล์ PDF ปลายทาง
      target_dpi (int): ความละเอียดเป้าหมายเริ่มต้น (DPI) ค่าเริ่มต้น 160
      max_size_mb (float): ขนาดไฟล์เป้าหมายสูงสุดที่ต้องการ (หน่วย MB) ค่าเริ่มต้น 1.0 MB
      
    Returns:
      dict: ผลลัพธ์การบีบอัดประกอบด้วย:
        - success (bool): สำเร็จหรือไม่
        - input_size_mb (float): ขนาดไฟล์ก่อนบีบอัด (MB)
        - output_size_mb (float): ขนาดไฟล์หลังบีบอัด (MB)
        - reduction_percent (float): เปอร์เซ็นต์ขนาดที่ลดลงได้
        - size_ok (bool): ขนาดไฟล์ผ่านเกณฑ์ที่กำหนด (≤ max_size_mb) หรือไม่
        - warning (str หรือ None): ข้อความแจ้งเตือนกรณีขนาดไฟล์ยังเกินเกณฑ์
        - error (str): ข้อความผิดพลาดกรณีบีบอัดไม่สำเร็จ
    """
    # ตรวจสอบว่าไฟล์ต้นฉบับมีอยู่จริงหรือไม่
    if not os.path.exists(input_path):
        return {"success": False, "error": f"ไม่พบไฟล์: {input_path}"}

    input_size = os.path.getsize(input_path)
    max_size_bytes = max_size_mb * 1024 * 1024

    # กำหนดค่าพารามิเตอร์ของแต่ละ Pass (เริ่มจากคุณภาพดีสุด -> ลดระดับลงมา)
    passes = [
        {
            "label": "Pass 1 (/ebook, DPI=160, Q=75)",
            "settings": "/ebook",
            "dpi": target_dpi,
            "quality": 75,
        },
        {
            "label": "Pass 2 (/screen, DPI=120, Q=60)",
            "settings": "/screen",
            "dpi": 120,
            "quality": 60,
        },
        {
            "label": "Pass 3 (/screen, DPI=96, Q=45)",
            "settings": "/screen",
            "dpi": 96,
            "quality": 45,
        },
        {
            "label": "Pass 4 (/screen, DPI=72, Q=30)",
            "settings": "/screen",
            "dpi": 72,
            "quality": 30,
        },
    ]

    try:
        best_path = output_path
        best_size = input_size
        current_input = input_path

        # วนลูปทดลองบีบอัดทีละ Pass
        for i, p in enumerate(passes):
            temp_out = output_path + f".pass{i}.pdf"
            print(f"  🔄 {p['label']}...")

            # เรียกคำสั่ง Ghostscript พร้อม Parameter ที่ปรับแต่งเพื่อความคมชัดและขนาดเล็ก
            result = run_gs([
                "-sDEVICE=pdfwrite",                   # ไดรเวอร์สำหรับแปลง/สร้างไฟล์ PDF
                "-dCompatibilityLevel=1.4",            # มาตรฐาน PDF 1.4 รองรับโปรแกรมเปิด PDF ได้ 100%
                f"-dPDFSETTINGS={p['settings']}",      # โปรไฟล์การบีบอัด (/ebook หรือ /screen)
                f"-dColorImageResolution={p['dpi']}",  # กำหนด DPI สำหรับรูปภาพสี
                f"-dGrayImageResolution={p['dpi']}",   # กำหนด DPI สำหรับรูปภาพขาวดำ/เกรย์สเกล
                f"-dMonoImageResolution={p['dpi']}",   # กำหนด DPI สำหรับภาพ Monochrome/ลายเส้น
                "-dColorImageDownsampleType=/Bicubic", # ใช้อัลกอริทึม Bicubic ช่วยให้ภาพย่อแล้วไม่แตก
                "-dGrayImageDownsampleType=/Bicubic",
                "-dDownsampleColorImages=true",        # เปิดใช้งานการย่อภาพสี
                "-dDownsampleGrayImages=true",         # เปิดใช้งานการย่อภาพขาวดำ
                "-dDownsampleMonoImages=true",
                f"-dColorImageQuality={p['quality']}", # คุณภาพการบีบอัด JPEG (1-100)
                f"-dGrayImageQuality={p['quality']}",
                "-dEmbedAllFonts=true",                # ฝังฟอนต์ลงในเอกสาร ป้องกันฟอนต์เพี้ยน
                "-dSubsetFonts=true",                  # ฝังเฉพาะตัวอักษรที่ใช้งานจริง (ลดขนาดได้มาก)
                "-dCompressFonts=true",                # บีบอัดข้อมูลฟอนต์ Stream
                "-dNOPAUSE",                           # ทำงานอัตโนมัติ ไม่ต้องหยุดรอการกดปุ่ม
                "-dQUIET",                             # ไม่แสดงข้อความดีบักที่ไม่จำเป็น
                "-dBATCH",                             # จบการทำงานทันทีเมื่อแปลงไฟล์เสร็จ
                f"-sOutputFile={temp_out}",            # ไฟล์ผลลัพธ์ของ Pass นี้
                current_input,                         # ไฟล์ต้นฉบับนำเข้า
            ])

            # ตรวจสอบว่า Ghostscript ทำงานสำเร็จหรือไม่
            if result.returncode != 0 or not os.path.exists(temp_out):
                print(f"     ⚠️  ข้ามรอบนี้ (Ghostscript error)")
                if os.path.exists(temp_out):
                    os.remove(temp_out)
                continue

            temp_size = os.path.getsize(temp_out)
            print(f"     → {temp_size/1024/1024:.2f} MB")

            # ตรวจสอบว่าได้ขนาดที่เล็กลงกว่าเดิมหรือไม่
            if temp_size < best_size:
                # ลบไฟล์ชั่วคราวรอบก่อนหน้าที่ไม่ใช่ไฟล์ต้นฉบับ
                if best_path != input_path and os.path.exists(best_path) and best_path != output_path:
                    os.remove(best_path)
                best_size = temp_size
                best_path = temp_out
            else:
                os.remove(temp_out)

            # หากได้ขนาดไฟล์ตามเป้าหมาย (≤ max_size_mb) ให้หยุดทันทีเพื่อรักษาคุณภาพสูงสุด
            if best_size <= max_size_bytes:
                print(f"  ✅ ได้ขนาดตามเป้าแล้ว หยุดที่ {p['label']}")
                break

        # ย้ายไฟล์ที่ดีที่สุดไปยังตำแหน่ง output_path ที่กำหนด
        if best_path != output_path:
            shutil.copy2(best_path, output_path)
            if best_path != input_path:
                os.remove(best_path)

        # ทำความสะอาดไฟล์ชั่วคราวที่อาจค้างอยู่
        for i in range(len(passes)):
            tmp = output_path + f".pass{i}.pdf"
            if os.path.exists(tmp):
                os.remove(tmp)

        # ตรวจสอบว่ามีไฟล์ผลลัพธ์เกิดขึ้นจริง
        if not os.path.exists(output_path):
            return {"success": False, "error": "ไม่สามารถสร้างไฟล์ output ได้"}

        # คำนวณสถิติขนาดไฟล์และเปอร์เซ็นต์ที่ลดลง
        output_size = os.path.getsize(output_path)
        reduction = (1 - output_size / input_size) * 100
        size_ok = output_size <= max_size_bytes

        return {
            "success": True,
            "input_size_mb": input_size / 1024 / 1024,
            "output_size_mb": output_size / 1024 / 1024,
            "reduction_percent": reduction,
            "size_ok": size_ok,
            "warning": None if size_ok else f"ขนาดยังเกิน {max_size_mb}MB ({output_size/1024/1024:.2f}MB) - PDF อาจมีเนื้อหา vector/font ขนาดใหญ่"
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timeout - การประมวลผลใช้เวลานานเกินกำหนด (ไฟล์อาจซับซ้อนเกินไป)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    """
    ฟังก์ชันหลักสำหรับการรันผ่าน Command Line Interface (CLI)
    ตัวอย่างการเรียกใช้:
      python compress.py my_document.pdf
      python compress.py input.pdf output_compressed.pdf
    """
    if len(sys.argv) < 2:
        print("=" * 60)
        print("โปรแกรมบีบอัดไฟล์ PDF ด้วย Ghostscript")
        print("=" * 60)
        print("การใช้งาน: python compress.py <input.pdf> [output.pdf]")
        print("ตัวอย่าง:  python compress.py document.pdf compressed.pdf")
        sys.exit(1)

    input_path = sys.argv[1]

    # หากไม่ได้ระบุ output_path จะสร้างชื่อไฟล์ใหม่โดยเติม _compressed
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_compressed.pdf"

    print(f"📄 กำลัง compress: {input_path}")
    print(f"🎯 เป้าหมาย: DPI={160}, ขนาด ≤ 1MB")
    print(f"💾 Output: {output_path}")
    print(f"🔧 Ghostscript: {GS_CMD}")
    print("⏳ กำลังประมวลผล...")

    # เรียกใช้ฟังก์ชันบีบอัด
    result = compress_pdf(input_path, output_path)

    # แสดงผลลัพธ์
    if not result["success"]:
        print(f"\n❌ เกิดข้อผิดพลาด: {result['error']}")
        sys.exit(1)

    print(f"\n✅ บีบอัดสำเร็จ!")
    print(f"   ขนาดก่อนบีบอัด: {result['input_size_mb']:.2f} MB")
    print(f"   ขนาดหลังบีบอัด: {result['output_size_mb']:.2f} MB")
    print(f"   ลดขนาดลงได้:   {result['reduction_percent']:.1f}%")

    if result["size_ok"]:
        print(f"   ✅ ขนาดไฟล์อยู่ในเกณฑ์ที่กำหนด (≤ 1MB)")
    else:
        print(f"\n⚠️  {result['warning']}")

    print(f"\n📁 บันทึกไฟล์เรียบร้อยที่: {output_path}")


if __name__ == "__main__":
    main()