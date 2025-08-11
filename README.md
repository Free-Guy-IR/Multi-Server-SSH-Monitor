
# 📊 Multi-Server SSH Monitor (Flask + Paramiko)

داشبورد تحت‌وب برای مانیتورینگ و مدیریت چند سرور لینوکسی از طریق SSH با رابط فارسی و حالت تاریک.

## ✨ ویژگی‌ها

- مانیتورینگ زنده **CPU / RAM / Disk / Network**
- نمایش نمودارهای نیم‌دایره برای منابع سیستم
- پشتیبانی از **ترمینال زنده** و اجرای دستورات روی یک یا همه سرورها
- **ترمینال کلی**: اجرای همزمان یک دستور روی تمام سرورها با خروجی زنده
- قابلیت افزودن و حذف سرورها از طریق رابط وب (نیاز به ویرایش دستی JSON نیست)
- پشتیبانی از ریستارت/خاموش‌کردن سرورها
- سازگار با موبایل و دسکتاپ
- رابط کاملاً فارسی

---

## 📦 پیش‌نیازها

روی سیستم خود **Python 3.8+** و pip نصب داشته باشید.

---

## 🛠 نصب و راه‌اندازی روی اوبونتو

```bash
# نصب وابستگی‌ها
sudo apt update && sudo apt install python3 python3-pip -y

# کلون کردن مخزن
git clone https://github.com/Free-Guy-IR/Multi-Server-SSH-Monitor.git
cd Multi-Server-SSH-Monitor

# نصب کتابخانه‌های پایتون
pip install -r requirements.txt

# اجرای برنامه
python3 Multi-server-ssh-monitor_live.py
```

---

## 🚀 اجرای خودکار بعد از بوت (systemd)

برای اینکه برنامه بعد از ریبوت سرور به صورت خودکار اجرا شود:

1. فایل `mssm.service` را در مسیر `/etc/systemd/system/` کپی کنید:
```bash
sudo cp mssm.service /etc/systemd/system/
```

2. سرویس را فعال و اجرا کنید:
```bash
sudo systemctl daemon-reload
sudo systemctl enable mssm
sudo systemctl start mssm
```

3. وضعیت سرویس را بررسی کنید:
```bash
sudo systemctl status mssm
```

---

## 🗂 فایل `servers.json`

در اولین اجرا، این فایل می‌تواند **خالی** باشد:
```json
[]
```
شما می‌توانید از طریق بخش **مدیریت سرورها** در رابط وب، سرورها را اضافه کنید.

---

## 📸 اسکرین‌شات

![screenshot](screenshot.png)

---

## ⚖ لایسنس

این پروژه تحت لایسنس MIT منتشر شده است.
