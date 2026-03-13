# STORM + 3x-UI Integration Guide

> [!IMPORTANT]
> این سند شامل بخش‌های آزمایشی/قدیمی است (مثل bridge مبتنی بر `storm_3xui_bridge.py`).
> برای تست عملی روی سرور، مسیر پایدار فعلی را از `SERVER_TEST_RUNBOOK_FA.md` دنبال کنید:
> `storm_client.py` (inside) + `storm_server.py` (outside) + outbound socks روی `127.0.0.1:1443`.

## سریع شروع (Quick Start)

### نیازمندی‌ها
- Docker
- Docker Compose
- Linux/Mac(یا WSL2 برای Windows)

### مرحله 1: Clone و Setup

```bash
cd Nexora\ storm/

# Make setup script executable
chmod +x setup_3xui.sh

# Run setup
./setup_3xui.sh
```

### مرحله 2: 3x-UI به STORM متصل کن

```bash
# Open 3x-UI dashboard
open http://localhost:54321
# یا
xdg-open http://localhost:54321

# Login
Username: admin
Password: admin123
```

### مرحله 3: Add VLESS Inbound

1. **Inbounds** → **Add Inbound**
2. **Settings:**
   - Protocol: `VLESS`
   - Listen: `0.0.0.0`
   - Port: `443`
   - Network: `TCP`
   - Security: `TLS` or `None`
3. **Add Clients** (optional - خود STORM Bridge سنجش می‌کند)
4. **Save**

### مرحله 4: Add Users

```sql
-- Inside PostgreSQL:
INSERT INTO users (uuid, email, tier, traffic_limit_gb) VALUES
  ('550e8400-e29b-41d4-a716-446655440000', 'user1@test.com', 'pro', 100),
  ('6ba7b810-9dad-11d1-80b4-00c04fd430c8', 'user2@test.com', 'free', 10);
```

### مرحله 5: Test VLESS Client

```bash
# Get VLESS URL for user
vless://550e8400-e29b-41d4-a716-446655440000@localhost:443?
  encryption=none&
  security=tls&
  sni=example.com&
  type=tcp

# Test with curl (Linux/Mac)
curl -x socks5://550e8400-e29b-41d4-a716-446655440000:@localhost:443 http://example.com
```

---

## آرکیتکچر

```
┌─────────────────────────────────────────────────────┐
│ User App (VLESS Client)                             │
└────────────┬────────────────────────────────────────┘
             │ VLESS protocol (:443 TLS)
             ↓
┌─────────────────────────────────────────────────────┐
│ STORM Bridge (Inside Gateway)                       │
│ - UUID validation                                   │
│ - Stream to STORM tunnel                            │
│ - Per-user traffic tracking                         │
└────────────┬────────────────────────────────────────┘
             │ DNS Query (UDP/53)
             │ base32(encrypted stream)
             ↓
┌─────────────────────────────────────────────────────┐
│ Public Resolvers (Google, Cloudflare, Quad9)       │
│ - Dual resolver failover                            │
│ - FEC recovery                                      │
└────────────┬────────────────────────────────────────┘
             │ DNS Response
             ↓
┌─────────────────────────────────────────────────────┐
│ STORM Outbound (Outside Gateway)                    │
│ - Extract STORM packets                             │
│ - Forward to 3x-UI upstream                         │
│ - Return response via DNS                           │
└────────────┬────────────────────────────────────────┘
             │ Upstream SOCKS (port 10000)
             ↓
┌─────────────────────────────────────────────────────┐
│ 3x-UI XRay Proxy                                    │
│ - Routes traffic to target                          │
│ - Applies routing rules                             │
│ - Logs statistics                                   │
└─────────────────────────────────────────────────────┘
```

---

## فایل‌های مهم

### STORM Bridge
- **`storm_3xui_bridge.py`** - VLESS listener و stream forwarding
- **`storm_outbound.py`** - DNS receiver و upstream connector

### Infrastructure
- **`docker-compose.yml`** - تمام سرویسات
- **`Dockerfile.inside`** - داخل gateway image
- **`Dockerfile.outside`** - خارج gateway image

### Configuration
- **`schema.sql`** - User/traffic database
- **`3xui-config.json`** - XRay config
- **`.env`** - Environment variables

### Setup
- **`setup_3xui.sh`** - Automated setup script

---

## مدیریت

### Docker Commands

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# View specific service
docker-compose logs -f storm-inside
docker-compose logs -f 3xui

# Stop all services
docker-compose down

# Restart a service
docker-compose restart storm-inside

# Execute command in container
docker-compose exec 3xui curl http://localhost:54321
```

### Database Access

```bash
# Connect to PostgreSQL
docker-compose exec postgres psql -U storm -d storm_users

# Check users
SELECT uuid, email, tier, traffic_used_gb, traffic_limit_gb FROM users;

# Check active sessions
SELECT u.email, s.peer_address, s.bytes_in, s.bytes_out FROM users u 
JOIN sessions s ON u.id = s.user_id WHERE s.status = 'active';
```

### Monitor Traffic

```bash
# View real-time stats
docker-compose exec postgres psql -U storm -d storm_users << SQL
SELECT u.email, COUNT(s.id) as active, 
       SUM(tl.bytes_in) as total_in, 
       SUM(tl.bytes_out) as total_out 
FROM users u 
LEFT JOIN sessions s ON u.id = s.user_id 
LEFT JOIN traffic_log tl ON u.id = tl.user_id 
GROUP BY u.email;
SQL
```

---

## Troubleshooting

### VLESS Connection Refused
```bash
# Check if bridge is running
docker-compose logs storm-inside | tail -20

# Check if port 443 is in use
lsof -i :443

# Verify VLESS config
# Make sure encryption is "none" and security is "tls"
```

### DNS Queries Not Working
```bash
# Test DNS query
dig @localhost example.com

# Check DNS logs
docker-compose logs storm-outside | grep "query\|error"
```

### Database Connection Error
```bash
# Check PostgreSQL
docker-compose exec postgres pg_isready -U storm

# View logs
docker-compose logs postgres
```

### High Latency
```bash
# Check resolver health
python test_real_resolvers.py --mode=full

# Check FEC recovery rate
docker-compose logs storm-inside | grep "FEC\|parity"
```

---

## Performance Tuning

### Adjust Resolver Timeout
Edit `storm_3xui_bridge.py`:
```python
timeout=3.0,  # Change this (default 3s)
```

### Increase Carrier Limit
Edit `docker-compose.yml`:
```yaml
environment:
  MAX_CARRIERS: "10"  # Default 5
```

### Buffer Size
Edit `storm_connection.py`:
```python
block_size=32,  # Increase from 16 for more redundancy
```

---

## مثال: چگونه کاربران را اضافه کنیم

### از طریق CLI
```bash
# Generate new UUID
python -c "import uuid; print(uuid.uuid4())"

# Add to database
docker-compose exec postgres psql -U storm -d storm_users << SQL
INSERT INTO users (uuid, email, tier, traffic_limit_gb) VALUES
  ('new-uuid-here', 'newuser@example.com', 'pro', 50);
SQL

# User is immediately available in STORM
```

### از طریق 3x-UI Panel
1. Inbounds → Select VLESS
2. Add client
3. Generate UUID
4. Save

---

## مثال: VLESS URL برای کاربران

```
# Basic
vless://550e8400-e29b-41d4-a716-446655440000@gateway.example.com:443?encryption=none&security=tls

# With all options
vless://550e8400-e29b-41d4-a716-446655440000@gateway.example.com:443?
  encryption=none&
  security=tls&
  sni=example.com&
  type=tcp&
  fp=chrome&
  allowinsecure=0

# QR Code
python -c "
import qrcode
url = 'vless://550e8400-e29b-41d4-a716-446655440000@gateway.example.com:443?encryption=none&security=tls'
qr = qrcode.QRCode()
qr.add_data(url)
qr.make()
qr.print_ascii()
"
```

---

## نظارت و Metrics

### Prometheus
- URL: `http://localhost:9090`
- Metrics:
  - `storm_bytes_in_total`
  - `storm_bytes_out_total`
  - `storm_active_connections`
  - `storm_packet_loss_rate`

### Grafana
- URL: `http://localhost:3000`
- Default: `admin/admin123`
- Dashboards:
  - Gateway Health
  - User Statistics
  - Traffic Analysis

### Logs
- Inside: `/var/log/storm/bridge.log`
- Outside: `/var/log/storm/outbound.log`
- 3x-UI: `/var/log/xray/`

---

## بهتر‌سازی برای تولید

### 1. SSL Certificates
```bash
# Generate self-signed
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365

# Or use Let's Encrypt
certbot certonly -d gateway.example.com
```

### 2. اپدیت Docker Compose
```yaml
volumes:
  - /etc/letsencrypt:/etc/letsencrypt:ro
  - keys.pem:/app/keys.pem:ro
```

### 3. Reverse Proxy (Nginx)
```nginx
server {
    listen 443 ssl http2;
    server_name gateway.example.com;
    
    ssl_certificate /etc/letsencrypt/live/gateway.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gateway.example.com/privkey.pem;
    
    location / {
        proxy_pass http://localhost:443;
    }
}
```

### 4. DDoS Protection (Cloudflare)
- 3x-UI کو Cloudflare کے پیچھے رکھیں
- DNS queries Cloudflare کے ذریعے بھیجیں
- Rate limiting کریں

### 5. Backup & Recovery
```bash
# Backup database
docker-compose exec postgres pg_dump -U storm storm_users > backup.sql

# Restore
docker-compose exec postgres psql -U storm storm_users < backup.sql
```

---

## سوالات متداول

**Q: کیا STORM stable ہے؟**
A: Local testing ✅ ہے، production میں ابھی untested ہے۔ 20-50 صارفین سے شروع کریں۔

**Q: کتنے صارفین support کر سکتا ہے؟**
A: تھوری‌ہ 10k+ concurrent ہو سکتے ہیں (DNS limits سے)۔

**Q: کیا میں 3x-UI کے بغیر استعمال کر سکتا ہوں؟**
A: ہاں، لیکن خود `storm_3xui_bridge.py` میں UUIDs hardcode کریں۔

**Q: Bandwidth limit کیسے سیٹ کریں؟**
A: Database میں `traffic_limit_gb` سیٹ کریں۔

---

## اگلے قدم

1. ✅ Setup مکمل کریں
2. ✅ 3x-UI میں inbound add کریں
3. ✅ Demo users بنائیں
4. ✅ VLESS config شیئر کریں
5. ✅ کنکشن ٹیسٹ کریں
6. ✅ Monitoring سیٹ اپ کریں
7. 📈 Gradually صارفین بڑھائیں
8. 🚀 تولید میں deploy کریں

---

**مسائل ہو؟ Logs دیکھیں:**
```bash
docker-compose logs -f --tail=100
```

**Config تبدیل کریں:**
```bash
nano .env
docker-compose restart
```

**مکمل reset:**
```bash
docker-compose down -v
./setup_3xui.sh
```
