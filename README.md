# 🚀 Worklog Management & PDF Tools — Vercel + Supabase Edition

เว็บแอปพลิเคชันระบบจัดการงานประจำวัน (Worklog), บันทึกและสรุปรายงาน, ระบบแยกสิทธิ์ผู้ใช้งาน (Admin / Staff), จัดการทรัพย์สินไอที (IT Asset Management) และเครื่องมือจัดการไฟล์ PDF แบบ Production Ready 

ขับเคลื่อนด้วยสถาปัตยกรรมระดับโลก:
* **Frontend & Backend:** [Vercel](https://vercel.com) (Serverless Python Fast Execution)
* **Cloud Database:** [Supabase](https://supabase.com) (PostgreSQL 17 Cloud Database 24/7 ถาวร ฟรี 100%)

---

## ☁️ วิธีขึ้นระบบบน Vercel (ง่ายและเร็วที่สุดใน 3 นาที)

### ขั้นตอนที่ 1: อัปโหลดโค้ดขึ้น GitHub
1. สร้าง New Repository บน GitHub ของคุณ (เช่น ตั้งชื่อว่า `worklog-suite`)
2. นำไฟล์ทั้งหมดในโปรเจกต์นี้ขึ้น GitHub:
   ```bash
   git init
   git add .
   git commit -m "Deploy Worklog Suite to Vercel + Supabase"
   git branch -M main
   git remote add origin https://github.com/<your-username>/worklog-suite.git
   git push -u origin main
   ```
   *(หรือใช้โปรแกรม GitHub Desktop / อัปโหลดผ่านหน้าเว็บ GitHub ก็ได้)*

---

### ขั้นตอนที่ 2: Import ขึ้น Vercel
1. เข้าไปที่ [vercel.com](https://vercel.com) แล้วล็อกอินด้วย GitHub
2. คลิกปุ่ม **"Add New..."** ➔ เลือก **"Project"**
3. เลือก Repository `worklog-suite` ที่เพิ่งอัปโหลด ➔ กด **"Import"**

---

### ขั้นตอนที่ 3: ใส่ Environment Variables แล้วกด Deploy
1. ในหน้า Configure Project เลื่อนลงมาที่หัวข้อ **"Environment Variables"**
2. เพิ่มตัวแปรดังนี้:
   * **Key:** `DATABASE_URL`
   * **Value:** `postgresql://postgres.wsmenlqasdomtjkbptmy:[YOUR-SUPABASE-PASSWORD]@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require`
   *(และสามารถเพิ่ม `SECRET_KEY` เป็นข้อความสุ่มได้ตามต้องการ)*
3. กดปุ่ม **"Deploy"** สีดำด้านล่าง

---

### 🎉 ผลลัพธ์
* Vercel จะทำการ Build และปล่อยเว็บแอปพลิเคชันให้อัตโนมัติภายในไม่ถึง 1 นาที
* คุณจะได้รับ URL ถาวร (HTTPS ฟรี) เช่น:  
  👉 **`https://worklog-suite.vercel.app`**
* เข้าใช้งานได้ตลอด 24 ชั่วโมงจากมือถือ ไอแพด หรือคอมพิวเตอร์ทุกเครื่องในโลก
* ข้อมูลทั้งหมด (งาน 17 รายการ, บัญชีผู้ใช้ 6 คน, ข้อมูลแผนก และอุปกรณ์ไอที) บันทึกและซิงค์แบบ Real-time บน Supabase PostgreSQL อย่างปลอดภัยถาวร!

---

## 🔑 บัญชีเข้าสู่ระบบเริ่มต้น
* **Super Admin (ผู้ดูแลระบบ):**
  * **Username:** `It`
  * **Password:** `*Admin55*`
* **Staff (พนักงาน):**
  * **Username:** `somchai_staff` หรือตามที่ได้ลงทะเบียนไว้
