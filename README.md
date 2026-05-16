# Py-3XUI-multiserver

Объединение нескольких серверов 3x-UI в единую VPN-подписку. Soft-tariff система с веб-панелью, донатной оплатой и Happ-совместимостью.

## Architecture

```
servers.conf ──→ 3x-UI сервера (API + sub URL)
settings.conf ──→ тарифы, платежи, админка
users.db ───────→ пользователи (SQLite)
     ↓
aggregator.py ──→ FastAPI сервер
     ↓
  /sub/{id} ────→ подписка для клиента (VLESS links)
  /admin/* ─────→ веб-панель управления
```

**Soft-tariff**: пользователь создаётся на всех inbounds один раз, tier (free/test/paid) меняется локально в БД, без дёргания 3x-UI API.

## Features

- Multi-server: объединение любого количества 3x-UI серверов
- Multi-inbound: поддержка нескольких inbound на сервере
- Soft-tariff: free / test / paid tier без пересоздания клиентов
- DonationAlerts: приём платежей (50₽ → test 7д, 150₽ → paid 30д, 990₽ → paid 365д)
- Веб-панель: дашборд, CRUD пользователей, просмотр трафика, QR-коды
- Happ.su совместимость: hide-settings, sub-expire, announce через HTTP-хедеры
- In-memory кеш: ссылки кешируются на 7 дней, трафик — на 2 минуты
- ShortID ротация: автоматическая смена shortId на серверах каждые N часов
- MOTD: объявления из motd.txt или settings

## Quick Start

```bash
pip install fastapi uvicorn httpx py3xui qrcode[pil] Pillow
git clone https://github.com/SashegDev/Py-3XUI-multiserver.git
cd Py-3XUI-multiserver
# настроить servers.conf, settings.conf (см. ниже)
python3 aggregator.py
```

## Configuration

### servers.conf

```json
{
  "servers": [
    {
      "name": "ru-1",
      "subscription_url": "https://panel.domain.com:2096",
      "api_base_url": "https://panel.domain.com:65431/PATH",
      "country": "RU",
      "sub_path": "/sub/{sub_id}",
      "inbounds": [
        {
          "id": 1,
          "name": "Reality",
          "api_host": "https://panel.domain.com:65431/PATH",
          "api_user": "admin",
          "api_pass": "password"
        }
      ]
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique server name |
| `subscription_url` | string | Base URL for subscription links |
| `api_base_url` | string | API URL for traffic stats |
| `country` | string | 2-letter country code (RU, DE, NL...) |
| `sub_path` | string | Sub endpoint path on the server (`/sub/{sub_id}`) |
| `inbounds[].id` | int | Inbound ID in 3x-UI |
| `inbounds[].name` | string | Display name |
| `inbounds[].api_host` | string | 3x-UI API URL for this inbound |
| `inbounds[].api_user` | string | API login |
| `inbounds[].api_pass` | string | API password |

### settings.conf

```json
{
  "general": {
    "title": "MyVPN",
    "host": "conn.example.com",
    "support_url": "https://t.me/support"
  },
  "announcement": "base64 enc or plain text",
  "shortid_rotation_hours": 11,
  "tiers": {
    "free": {
      "name": "Free",
      "servers": ["ru-1"],
      "traffic_limit_gb": 0
    },
    "test": {
      "name": "Test 7 days",
      "servers": ["ru-1", "nl-1"],
      "traffic_limit_gb": 5,
      "price": 50,
      "duration_days": 7
    },
    "paid": {
      "name": "Premium",
      "servers": ["ru-1", "nl-1", "de-1"],
      "traffic_limit_gb": 0
    }
  },
  "payments": {
    "donationalerts": {
      "enabled": true,
      "token": "your_donationalerts_token",
      "url": "https://www.donationalerts.com/r/you"
    }
  },
  "admin": {
    "username": "admin",
    "password": "secure_password"
  }
}
```

### Tiers

| Tier | Servers | Traffic | Price | Duration |
|------|---------|---------|-------|----------|
| free | subset marked `is_free: true` in config | unlimited (0 = ∞) | free | forever |
| test | all servers | 5 GB | 50₽ | 7 days |
| paid | all servers | unlimited | 150₽/30d, 990₽/365d | configurable |

Free servers are defined in `servers.conf` per-server via `is_free: true`.

### MOTD

Two ways:
1. `motd.txt` — plain text file in project root
2. `settings.conf → announcement` — string (plain or base64)

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/sub/{id}` | GET | Subscription (format: base64/json/raw) |
| `/sub/{id}` | GET (HTML accept) | Web page with QR and info |
| `/admin/login` | POST | Admin auth |
| `/admin/users` | GET | User management panel |
| `/admin/dashboard` | GET | Stats dashboard |
| `/admin/api/users` | POST | Create user |
| `/admin/api/users/update` | POST | Update user |
| `/admin/api/users/delete` | POST | Delete user |
| `/admin/api/reload` | POST | Reload configs + clear cache |
| `/admin/api/rotate-shortids` | GET | Rotate short IDs on all servers |
| `/` | GET | Landing page |

## DonationAlerts

Polling-based (every ~2 seconds). Amount mapping:
- 50₽ → test tier (7 days)
- 150₽ → paid tier (30 days)
- 990₽ → paid tier (365 days)

Other amounts are ignored. Donor identified by `id` (priority) or `username` from message parts.

## Happ.su Support

Subscription response includes Happ-compatible headers:
- `Profile-Title`, `Profile-Update-Interval`, `Profile-Web-Page-Url`
- `Subscription-Userinfo` (upload/download/total/expire)
- `Announce`, `Support-Url`, `Hide-Settings`
- `Sub-Expire`, `Sub-Expire-Button-Link`

## Tech

- **FastAPI** + uvicorn
- **SQLite** (users only)
- **py3xui** — 3x-UI API client
- **httpx** — async HTTP for sub links
- **qrcode** — QR generation
