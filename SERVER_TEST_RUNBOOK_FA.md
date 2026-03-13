# Runbook تست دو‌سروره STORM + 3x-ui (مسیر پایدار)

این ران‌بوک برای مسیر پایدار فعلی است:
- `storm_client.py` روی سرور inside
- `storm_server.py` روی سرور outside
- کاربر موبایل با VLESS به Xray inside وصل می‌شود
- Xray inside خروجی را به SOCKS روی `127.0.0.1:1443` (STORM) می‌دهد
- STORM ترافیک را روی DNS/UDP53 به outside می‌برد
- outside ترافیک را به SOCKS/Xray upstream تحویل می‌دهد

## 1) پیش‌نیاز

1. داخل هر دو سرور Python 3.10+ نصب باشد.
2. روی outside پورت `53/udp` باز باشد.
3. روی inside پورت VLESS (معمولا `443/tcp`) باز باشد.
4. فایل‌های پروژه روی هر دو سرور در مسیر یکسان (مثلا `/opt/storm`) موجود باشد.

## 2) نصب روی هر دو سرور

```bash
cd /opt/storm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3) راه‌اندازی outside

1. outside باید یک upstream SOCKS واقعی داشته باشد (مثلا Xray SOCKS inbound روی `127.0.0.1:10000`).
2. STORM server را اجرا کن:

```bash
cd /opt/storm
source .venv/bin/activate
python storm_server.py \
  --listen 0.0.0.0:53 \
  --target 127.0.0.1:10000 \
  --zone t1.phonexpress.ir
```

## 4) راه‌اندازی inside

```bash
cd /opt/storm
source .venv/bin/activate
python storm_client.py \
  --listen 127.0.0.1:1443 \
  --resolvers <OUTSIDE_PUBLIC_IP> \
  --zone t1.phonexpress.ir \
  --poll-interval 0.2
```

نکته: برای فاز تست اولیه، resolver را مستقیم IP سرور outside بده (پایدارتر از public resolver).

## 5) تنظیم 3x-ui/Xray روی inside

1. inbound کاربر (VLESS/TLS) مثل قبل.
2. یک outbound از نوع `socks` بساز که به `127.0.0.1:1443` وصل شود.
3. route ترافیک کاربران را به همان outbound socks بفرست.

نمونه outbound:

```json
{
  "protocol": "socks",
  "tag": "storm-out",
  "settings": {
    "servers": [
      {
        "address": "127.0.0.1",
        "port": 1443
      }
    ]
  }
}
```

## 6) Health Check قبل از تست کاربر

از روی inside:

```bash
cd /opt/storm
source .venv/bin/activate
python storm_health_check.py \
  --proxy-host 127.0.0.1 \
  --proxy-port 1443 \
  --target-host 1.1.1.1 \
  --target-port 53 \
  --checks 30 \
  --interval 0.5 \
  --success-threshold 0.95 \
  --latency-threshold-ms 1200 \
  --json-out storm_health_report.json
```

## 7) معیار Go / No-Go

1. `success_rate >= 95%`
2. `p95 latency <= 1200ms`
3. حداقل 30 probe متوالی بدون crash
4. لاگ `storm_client` و `storm_server` بدون exception تکرارشونده

## 8) rollout پیشنهادی

1. ابتدا فقط 5 کاربر تست.
2. اگر 2 ساعت پایدار بود، به 20 کاربر برسون.
3. اگر 24 ساعت پایدار بود، rollout اصلی.

## 9) rollback سریع

1. route داخل Xray را موقتاً از `storm-out` برگردان به outbound قبلی.
2. سرویس‌های `storm_client` و `storm_server` را stop کن.
3. بعد از رفع مشکل دوباره health-check اجرا کن.

## 10) انتخاب resolver سالم از `data/resolvers.txt` (inside)

برای اینکه داخل به DNS ثابت (مثل `1.1.1.1`) وابسته نباشد، از ابزار picker استفاده کن:

```bash
cd /opt/nexora-storm
source .venv/bin/activate
python storm_resolver_picker.py \
  --resolvers-file data/resolvers.txt \
  --zone t1.phonexpress.ir \
  --timeout 1.5 \
  --max-probe 40 \
  --concurrency 15 \
  --take 4 \
  --min-healthy 2 \
  --json-out resolver_probe_report.json
```

خروجی دستور، لیست resolver انتخاب‌شده است (space-separated) که می‌تواند مستقیم به `storm_client.py --resolvers ...` داده شود.

## 11) اجرای دائم بدون SSH (systemd)

### خارج (outside)

```bash
cd /opt/nexora-storm
sudo cp systemd/storm-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now storm-server
sudo systemctl status storm-server --no-pager
```

### داخل (inside)

```bash
cd /opt/nexora-storm
chmod +x run_storm_client_auto.sh
sudo cp systemd/storm-client.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now storm-client
sudo systemctl status storm-client --no-pager
```

لاگ‌ها:

```bash
sudo journalctl -u storm-server -f
sudo journalctl -u storm-client -f
```
