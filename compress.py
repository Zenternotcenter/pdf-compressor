#!/usr/bin/env python3
"""
PDF Compressor - ลด DPI เป็น 160 และบีบอัดให้ไม่เกิน 1MB
ใช้ Ghostscript สำหรับคุณภาพสูงสุด
"""

import subprocess
import sys
import os
import shutil
import platform

def find_ghostscript() -> str:
    """หา Ghostscript executable อัตโนมัติ"""
    if platform.system() == "Windows":
        # ลองหาใน Program Files ก่อน
        candidates = []
        for base in [r"C:\Program Files\gs", r"C:\Program Files (x86)\gs"]:
            if os.path.isdir(base):
                for root, dirs, files in os.walk(base):
                    for f in files:
                        if f.lower() == "gswin64c.exe":
                            candidates.append(os.path.join(root, f))
        if candidates:
            return candidates[0]  # เอาตัวแรกที่เจอ
        return "gswin64c"  # fallback ให้ลองใน PATH
    else:
        return "gs"

GS_CMD = find_ghostscript()

def run_gs(args: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """รัน Ghostscript command"""
    return subprocess.run([GS_CMD] + args, capture_output=True, text=True, timeout=timeout)


def compress_pdf(input_path: str, output_path: str, target_dpi: int = 160, max_size_mb: float = 1.0) -> dict:
    """
    Compress PDF ด้วย Ghostscript แบบ multi-pass
    ลองตั้งแต่เบาไปหนัก จนกว่าจะได้ขนาดที่ต้องการ

    Returns: dict with success, input_size_mb, output_size_mb, reduction_percent
    """
    if not os.path.exists(input_path):
        return {"success": False, "error": f"ไม่พบไฟล์: {input_path}"}

    input_size = os.path.getsize(input_path)
    max_size_bytes = max_size_mb * 1024 * 1024

    # กำหนด pass แต่ละรอบ (เบา → หนัก)
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

        for i, p in enumerate(passes):
            temp_out = output_path + f".pass{i}.pdf"
            print(f"  🔄 {p['label']}...")

            result = run_gs([
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.4",
                f"-dPDFSETTINGS={p['settings']}",
                f"-dColorImageResolution={p['dpi']}",
                f"-dGrayImageResolution={p['dpi']}",
                f"-dMonoImageResolution={p['dpi']}",
                "-dColorImageDownsampleType=/Bicubic",
                "-dGrayImageDownsampleType=/Bicubic",
                "-dDownsampleColorImages=true",
                "-dDownsampleGrayImages=true",
                "-dDownsampleMonoImages=true",
                f"-dColorImageQuality={p['quality']}",
                f"-dGrayImageQuality={p['quality']}",
                "-dEmbedAllFonts=true",
                "-dSubsetFonts=true",
                "-dCompressFonts=true",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                f"-sOutputFile={temp_out}",
                current_input,
            ])

            if result.returncode != 0 or not os.path.exists(temp_out):
                print(f"     ⚠️  ข้ามรอบนี้ (Ghostscript error)")
                if os.path.exists(temp_out):
                    os.remove(temp_out)
                continue

            temp_size = os.path.getsize(temp_out)
            print(f"     → {temp_size/1024/1024:.2f} MB")

            if temp_size < best_size:
                # ลบไฟล์ best เก่า (ถ้าไม่ใช่ input ต้นฉบับ)
                if best_path != input_path and os.path.exists(best_path) and best_path != output_path:
                    os.remove(best_path)
                best_size = temp_size
                best_path = temp_out
            else:
                os.remove(temp_out)

            # ถ้าได้ขนาดที่ต้องการแล้ว หยุด
            if best_size <= max_size_bytes:
                print(f"  ✅ ได้ขนาดตามเป้าแล้ว หยุดที่ {p['label']}")
                break

        # copy best result ไปยัง output_path
        if best_path != output_path:
            shutil.copy2(best_path, output_path)
            if best_path != input_path:
                os.remove(best_path)

        # ลบ temp files ที่เหลือ
        for i in range(len(passes)):
            tmp = output_path + f".pass{i}.pdf"
            if os.path.exists(tmp):
                os.remove(tmp)

        if not os.path.exists(output_path):
            return {"success": False, "error": "ไม่สามารถสร้างไฟล์ output ได้"}

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
        return {"success": False, "error": "Timeout - ไฟล์ใหญ่เกินไป"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    if len(sys.argv) < 2:
        print("การใช้งาน: python compress.py <input.pdf> [output.pdf]")
        print("ตัวอย่าง:  python compress.py document.pdf compressed.pdf")
        sys.exit(1)

    input_path = sys.argv[1]

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

    result = compress_pdf(input_path, output_path)

    if not result["success"]:
        print(f"\n❌ เกิดข้อผิดพลาด: {result['error']}")
        sys.exit(1)

    print(f"\n✅ สำเร็จ!")
    print(f"   ขนาดก่อน:  {result['input_size_mb']:.2f} MB")
    print(f"   ขนาดหลัง:  {result['output_size_mb']:.2f} MB")
    print(f"   ลดลง:      {result['reduction_percent']:.1f}%")

    if result["size_ok"]:
        print(f"   ✅ ขนาดอยู่ในเกณฑ์ ≤ 1MB")
    else:
        print(f"\n⚠️  {result['warning']}")

    print(f"\n📁 ไฟล์ที่บีบอัดแล้ว: {output_path}")


if __name__ == "__main__":
    main()