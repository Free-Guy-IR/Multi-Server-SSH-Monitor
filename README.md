# 📊 Multi‑Server SSH Monitor (Flask + Paramiko)

داشبورد تحت‌وب برای **مانیتورینگ و مدیریت چند سرور لینوکسی از طریق SSH** با رابط فارسی و حالت تاریک.  
ویژگی‌ها: مانیتورینگ زنده CPU/RAM/Disk/Network، نمودارها، ترمینال زنده هر سرور، **ترمینال کلی** (اجرای همزمان روی همه)، افزودن/حذف سرور از داخل وب، ذخیره تاریخچه، ساعت تهران و بیشتر.

---

## ✨ قابلیت‌ها
- مانیتورینگ زنده (Real‑Time) برای:
  - CPU% ،RAM% ،Disk% (نمودار نیم‌دایره با برچسب‌های CPU/RAM/Disk)
  - نرخ و حجم شبکه (RX/TX)
  - Load Average و Uptime
- نمودارهای خطی و نیم‌دایره با Chart.js
- **ترمینال لایو** برای هر سرور (SSE)
- **ترمینال کلی**: اجرای یک دستور روی همهٔ سرورها به‌صورت همزمان + امکان ارسال ورودی بعدی (تعامل با اسکریپت‌ها)
- مدیریت سرور: **Reboot / Shutdown**، **Restart Service**
- **افزودن/حذف سرور از داخل وب** (بدون ویرایش دستی فایل)
- ذخیره تاریخچهٔ کوتاه‌مدت روی دیسک (persist) و بازیابی بعد از ری‌استارت برنامه
- نمایش ساعت با **منطقهٔ زمانی تهران** (مستقل از تایم‌زون سیستم)

---

## 📦 پیش‌نیازها
- Python 3.8+
- اوبونتو 20.04/22.04/24.04 توصیه می‌شود
- دسترسی SSH به سرورها (ترجیحاً با **SSH Key**)

---

## 🚀 نصب سریع روی اوبونتو
```bash
# ابزارهای پایه
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

# مسیر نصب
sudo mkdir -p /opt/mssm && sudo chown $USER:$USER /opt/mssm
cd /opt/mssm

# دریافت کد (ریپو خودتان را جایگزین کنید)
git clone https://github.com/<USERNAME>/<REPO>.git .

# محیط مجازی و وابستگی‌ها
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt  # یا: pip install Flask paramiko
```

### 🧩 ساخت تنظیمات اولیه (فایل خالی)
> در این پروژه، **فایل `servers.json` به‌صورت خالی** شروع می‌شود و شما **از داخل وب** سرورها را اضافه می‌کنید.

`servers.json` (پیش‌فرض خالی):
```json
{
  "poll_interval": 2,
  "admin_token": "CHANGE_ME_OR_REMOVE",
  "servers": []
}
```
- `admin_token`: اگر مقدار بدهید، عملیات مدیریتی (ریبوت/شات‌داون/ترمینال) تنها با این توکن انجام می‌شود. (در UI یک‌بار از شما پرسیده می‌شود)
- `servers`: خالی بماند؛ سرورها را از بخش «مدیریت سرورها» در داشبورد اضافه کنید.  
- حتماً مطمئن شوید کاربری که سرویس را اجرا می‌کند **اجازهٔ نوشتن روی `servers.json`** را دارد.

### ▶️ اجرای تستی
```bash
source /opt/mssm/.venv/bin/activate
python3 Multi-server-ssh-monitor.py
# سپس در مرورگر: http://127.0.0.1:8000
```
> به صورت پیش‌فرض روی 127.0.0.1 گوش می‌دهد. در صورت نیاز می‌توانید host را به 0.0.0.0 تغییر دهید یا پشت Nginx قرار دهید.

---

## ♻️ اجرای پایدار با systemd (بعد از ریبوت هم بالا بیاید)
۱) سرویس بسازید:
```bash
sudo nano /etc/systemd/system/mssm.service
```
محتوا:
```ini
[Unit]
Description=Multi-Server SSH Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
Group=YOUR_USER
WorkingDirectory=/opt/mssm
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/mssm/.venv/bin/python3 /opt/mssm/Multi-server-ssh-monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
> `YOUR_USER` را با کاربر واقعی جایگزین کنید.  
> اطمینان حاصل کنید `/opt/mssm/servers.json` برای این کاربر writable باشد (برای افزودن/حذف سرور از UI).

۲) فعال‌سازی:
```bash
sudo systemctl daemon-reload
sudo systemctl enable mssm
sudo systemctl start mssm
```

۳) بررسی وضعیت و لاگ:
```bash
systemctl status mssm
journalctl -u mssm -f
```

---

## 🖥 نحوه استفاده (داشبورد وب)
- ورود به `http://127.0.0.1:8000`
- **مدیریت سرورها** → افزودن سرور جدید (Name, Host, Port, Username, Password/KeyPath)
  - اتصال برقرار شد → سرور به `servers.json` اضافه می‌شود و مانیتورینگ شروع می‌شود
  - حذف سرور → از UI انجام می‌شود و فایل تنظیمات به‌روزرسانی می‌گردد
- **ترمینال هر سرور**: دستور وارد کنید (Live برای خروجی لحظه‌ای)
- **ترمینال کلی**: دستور را روی همهٔ سرورها همزمان اجرا کنید؛ باکس ورودی برای ارسال‌های بعدی وجود دارد (برای اسکریپت‌های تعاملی)
- **مدیریت**: Reboot / Shutdown و Restart Service
- **برچسب گیج‌ها**: زیر سه گیج نیم‌دایره برچسب‌های CPU / RAM / Disk نمایش داده می‌شود
- **ساعت**: هدر همیشه با **منطقه زمانی تهران** نمایش داده می‌شود

---

## 🔐 نکات امنیتی
- این برنامه برای اجرای **محلی یا شبکهٔ امن** طراحی شده؛ در صورت انتشار روی اینترنت، حتماً پشت **Reverse Proxy + HTTPS** قرار دهید.
- از **SSH Key** به‌جای پسورد استفاده کنید؛ مثال:
  ```bash
  ssh-keygen -t ed25519 -C "mssm"
  ssh-copy-id -i ~/.ssh/id_ed25519.pub root@SERVER_IP
  ```
  سپس در UI هنگام افزودن سرور، مسیر کلید خصوصی (مثلاً `/home/USER/.ssh/id_ed25519`) را بدهید.
- اگر `admin_token` تنظیم کرده‌اید، آن را امن نگه دارید.

---

## ❓ رفع اشکال رایج
- **اتصال برقرار نمی‌شود** → پورت 22، فایروال سرور مقصد و دسترسی کاربر را بررسی کنید.
- **Host key verification failed** → یک‌بار دستی `ssh user@host` بزنید تا کلید در `known_hosts` ثبت شود.
- **پس از ریبوت داده‌های نمودار صفر شد** → persistence فعال است؛ مسیر `./persist/` بررسی شود و دسترسی نوشتن وجود داشته باشد.

---

## 📄 لایسنس
MIT

— ساخته‌شده برای نیازهای واقعی مدیریت چندین سرور، با رابط فارسی و تجربهٔ کاربری روان.
