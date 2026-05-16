#!/usr/bin/env python3
"""
ZernProxy Manager v22 - Soft-Tariff with SQLite
"""

import json
import base64
import logging
import asyncio
import io
import os
import qrcode
import uuid
import hashlib
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import SquareModuleDrawer
from typing import List, Dict, Optional, Tuple
from contextlib import contextmanager
from fastapi import FastAPI, Response, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
import uvicorn
import httpx
import re
from py3xui import Api
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("zernproxy")

BASE_DIR = "/opt/aggregator"
SERVERS_CONF = os.path.join(BASE_DIR, "servers.conf")
SETTINGS_CONF = os.path.join(BASE_DIR, "settings.conf")
USERS_DB = os.path.join(BASE_DIR, "users.db")
LOGO_PATH = os.path.join(BASE_DIR, "logo.png")
MOTD_PATH = os.path.join(BASE_DIR, "motd.txt")

servers = []
settings = {}
last_donation_id = 0

# In-memory cache
_links_cache: Dict[str, Tuple[List[str], float]] = {}
_traffic_cache: Dict[str, Tuple[Dict, float]] = {}
CACHE_TTL_TRAFFIC = 120
CACHE_TTL_LINKS = 86400 * 7

def _get_cached(key: str, cache: dict, ttl: float):
    entry = cache.get(key)
    if entry and time.time() - entry[1] < ttl:
        return entry[0]
    return None

def _set_cache(key: str, value, cache: dict):
    cache[key] = (value, time.time())

def get_cached_links(sub_id: str):
    return _get_cached(sub_id, _links_cache, CACHE_TTL_LINKS)

def set_cached_links(sub_id: str, links: List[str]):
    _set_cache(sub_id, links, _links_cache)

def get_cached_traffic(sub_id: str):
    return _get_cached(sub_id, _traffic_cache, CACHE_TTL_TRAFFIC)

def set_cached_traffic(sub_id: str, data: dict):
    _set_cache(sub_id, data, _traffic_cache)

def clear_cache(sub_id: str = None):
    if sub_id:
        _links_cache.pop(sub_id, None)
        _traffic_cache.pop(sub_id, None)
    else:
        _links_cache.clear()
        _traffic_cache.clear()

def load_json(path: str, default: dict) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {path}: {e}")
    return default

def save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")

def init_db():
    conn = sqlite3.connect(USERS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            subscription_id TEXT UNIQUE NOT NULL,
            tier TEXT DEFAULT 'free',
            tariff_days_bought INTEGER DEFAULT 0,
            tariff_days_remaining INTEGER DEFAULT 0,
            total_paid_rubles INTEGER DEFAULT 0,
            traffic_limit_gb INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(USERS_DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def load_configs():
    global servers, settings
    servers = load_json(SERVERS_CONF, {"servers": []}).get("servers", [])
    settings = load_json(SETTINGS_CONF, {
        "general": {"title": "ZernProxy", "host": "conn.zernmc.ru", "support_url": ""},
        "announcement": "",
        "shortid_rotation_hours": 11,
        "tiers": {
            "free": {"name": "Free", "servers": [], "traffic_limit_gb": 0},
            "paid": {"name": "Premium", "servers": [], "traffic_limit_gb": 0}
        },
        "payments": {"donationalerts": {"enabled": False}}
    })
    logger.info(f"Loaded {len(servers)} servers")

init_db()
load_configs()

def get_flag_emoji(country_code: str) -> str:
    if not country_code or len(country_code) < 2:
        return ""
    try:
        return chr(ord(country_code[0].upper()) + 127397) + chr(ord(country_code[1].upper()) + 127397)
    except:
        return ""

def generate_qr_base64(data: str, size: int = 300) -> str:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white", image_factory=StyledPilImage, module_drawer=SquareModuleDrawer())
    img = img.resize((size, size), resample=0)
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

def get_logo_base64() -> Optional[str]:
    if os.path.exists(LOGO_PATH):
        try:
            with open(LOGO_PATH, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except:
            pass
    return None

def get_motd() -> str:
    if os.path.exists(MOTD_PATH):
        try:
            with open(MOTD_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except:
            pass
    return ""

def format_bytes(bytes_val: int) -> str:
    if bytes_val == 0:
        return "0 B"
    sizes = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    val = float(bytes_val)
    while val >= 1024 and i < len(sizes) - 1:
        val /= 1024
        i += 1
    return f"{val:.1f} {sizes[i]}" if val < 100 else f"{int(val)} {sizes[i]}"

def get_traffic_stats(sub_id: str) -> dict:
    total_up = 0
    total_down = 0
    by_server = {}
    
    for srv in servers:
        srv_up = 0
        srv_down = 0
        for inbound in srv.get("inbounds", []):
            api_host = inbound.get("api_host")
            api_user = inbound.get("api_user")
            api_pass = inbound.get("api_pass")
            inbound_id = inbound.get("id")
            
            if not all([api_host, api_user, api_pass, inbound_id]):
                continue
            
            try:
                api = Api(host=api_host, username=api_user, password=api_pass, use_tls_verify=False)
                api.login()
                
                inbounds = api.inbound.get_list()
                for ib in inbounds:
                    if ib.id == inbound_id and ib.client_stats:
                        for client in ib.client_stats:
                            if getattr(client, 'sub_id', '') == sub_id:
                                srv_up += client.up or 0
                                srv_down += client.down or 0
            except:
                pass
        
        if srv_up or srv_down:
            by_server[srv["name"]] = {"up": srv_up, "down": srv_down}
        total_up += srv_up
        total_down += srv_down
    
    return {"total_up": total_up, "total_down": total_down, "by_server": by_server}

def generate_sub_id(length: int = 16) -> str:
    return ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(length))

async def fetch_vless_links(url: str) -> List[str]:
    async with httpx.AsyncClient(verify=False, timeout=10.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 404 or resp.status_code == 400:
                logger.warning(f"Sub not found on server: {url}")
                return []
            if resp.status_code == 200:
                content = resp.text.strip()
                try:
                    decoded = base64.b64decode(content).decode('utf-8')
                    links = re.findall(r'(vless://[^\s\n]+)', decoded)
                    if links:
                        return links
                except:
                    pass
                return re.findall(r'(vless://[^\s\n]+)', content)
        except Exception as e:
            logger.error(f"Fetch error: {e}")
    return []

def find_user_on_servers(sub_id: str) -> Optional[str]:
    """Ищет юзера на 3x-UI серверах по subId, возвращает username"""
    logger.info(f"Looking for sub_id: {sub_id}")
    for srv in servers:
        for inbound in srv.get("inbounds", []):
            api_host = inbound.get("api_host")
            api_user = inbound.get("api_user")
            api_pass = inbound.get("api_pass")
            inbound_id = inbound.get("id")
            
            if not all([api_host, api_user, api_pass, inbound_id]):
                continue
            
            try:
                api = Api(host=api_host, username=api_user, password=api_pass, use_tls_verify=False)
                api.login()
                
                inbounds = api.inbound.get_list()
                for ib in inbounds:
                    if ib.id == inbound_id and ib.client_stats:
                        for client in ib.client_stats:
                            client_subid = getattr(client, 'sub_id', '') or ''
                            logger.info(f"Found client: {client.email} sub_id: {client_subid}")
                            if client_subid == sub_id:
                                email = client.email or ""
                                if '@' in email:
                                    username = email.split('@')[0]
                                    if '_' in username:
                                        username = username.split('_')[0]
                                    logger.info(f"Migrated user: {username} (found on {srv['name']}/{inbound['name']})")
                                    return username
            except Exception as e:
                logger.error(f"Error checking {srv['name']}/{inbound['name']}: {e}")
    
    return None

def get_user_tier(sub_id: str) -> Tuple[Optional[dict], str]:
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE subscription_id = ?", (sub_id,)).fetchone()
        
        if not user:
            username = find_user_on_servers(sub_id)
            if username:
                try:
                    conn.execute("""
                        INSERT INTO users (username, subscription_id, tier, tariff_days_bought, tariff_days_remaining, total_paid_rubles, traffic_limit_gb, is_active)
VALUES (?, ?, 'free', 0, 0, 0, 0, 1)
                    """, (username, sub_id))
                    conn.commit()
                    user = conn.execute("SELECT * FROM users WHERE subscription_id = ?", (sub_id,)).fetchone()
                    logger.info(f"User migrated: {username}")
                except sqlite3.IntegrityError:
                    pass
            
            if not user:
                return None, "free"
        
        user = dict(user)
        
        if user['tier'] == 'paid' and user['tariff_days_remaining'] <= 0:
            conn.execute("UPDATE users SET tier = 'free' WHERE id = ?", (user['id'],))
            conn.commit()
            user['tier'] = 'free'
        
        return user, user['tier']
    finally:
        conn.close()

def get_servers_for_tier(tier: str) -> List[dict]:
    result = []
    for srv in servers:
        if not srv.get("is_active"):
            continue
        
        if tier == "free":
            if srv.get("is_free", True):
                result.append(srv)
        elif tier == "paid":
            result.append(srv)
    
    return result

def deduplicate_inbounds(servers_list: List[dict], tier: str) -> List[Tuple[dict, dict]]:
    seen = set()
    result = []
    for srv in servers_list:
        for inbound in srv.get("inbounds", []):
            if tier == "free" and not inbound.get("is_free", True):
                continue
            
            key = (srv["name"], inbound.get("name", ""))
            if key not in seen:
                seen.add(key)
                result.append((srv, inbound))
    return result

app = FastAPI(title="ZernProxy Manager", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ADMIN_USER = settings.get("admin", {}).get("username", "admin")
ADMIN_PASS = settings.get("admin", {}).get("password", "")

def check_admin(request: Request) -> bool:
    if not ADMIN_PASS:
        return True
    token = request.cookies.get("admin_token", "")
    if token == ADMIN_PASS:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            u, p = decoded.split(":", 1)
            if u == ADMIN_USER and p == ADMIN_PASS:
                return True
        except:
            pass
    return False

ADMIN_LOGIN_PAGE = '''
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Вход</title><style>
body{font-family:Roboto,sans-serif;background:#121212;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1e1e1e;padding:32px;border-radius:16px;max-width:360px;width:90%;text-align:center}
h2{margin:0 0 20px;font-weight:500}.form-group{margin-bottom:16px;text-align:left}
label{display:block;margin-bottom:8px;color:#aaa;font-size:14px}
input{width:100%;padding:12px;border:1px solid #333;border-radius:8px;background:#2a2a2a;color:#fff;font-size:14px;box-sizing:border-box}
.btn{width:100%;padding:12px;background:#4CAF50;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer}
.btn:hover{background:#45a049}</style></head><body>
<div class="card"><h2>🔐 Вход</h2>
<form method="post" action="/admin/login">
<div class="form-group"><label>Пользователь</label><input type="text" name="username" required></div>
<div class="form-group"><label>Пароль</label><input type="password" name="password" required></div>
<button class="btn" type="submit">Войти</button>
</form></div></body></html>'''

@app.post("/admin/login")
async def admin_login(request: Request):
    data = await request.form()
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
        resp = RedirectResponse(url="/admin/users")
        resp.set_cookie(key="admin_token", value=ADMIN_PASS, max_age=86400 * 7, httponly=True)
        return resp
    return HTMLResponse(content=ADMIN_LOGIN_PAGE.replace("Вход", "Вход · неверный пароль"), status_code=401)

@app.get("/sub/{subscription_id}")
async def get_subscription(request: Request, subscription_id: str, format: str = Query("base64", pattern="^(json|base64|raw)$")):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and format == "base64":
        return await get_web_page(subscription_id)
    
    user, tier = get_user_tier(subscription_id)
    if not user:
        raise HTTPException(404, "User not found")
    
    if not user.get("is_active"):
        raise HTTPException(403, "User is disabled")
    
    tier_config = settings.get("tiers", {}).get(tier, {})
    servers_for_tier = get_servers_for_tier(tier)
    inbounds = deduplicate_inbounds(servers_for_tier, tier)
    
    if not inbounds:
        raise HTTPException(404, "No servers available")
    
    all_links = get_cached_links(subscription_id)
    if all_links is None:
        all_links = []
        seen_links = set()
        servers_processed = set()
        
        for srv, inbound in inbounds:
            srv_name = srv["name"]
            if srv_name in servers_processed:
                continue
            
            sub_path = srv["sub_path"].format(sub_id=subscription_id)
            url = f"{srv['subscription_url'].rstrip('/')}{sub_path}"
            links = await fetch_vless_links(url)
            
            servers_processed.add(srv_name)
            
            for link in links:
                clean_link = link.split('#')[0]
                if clean_link in seen_links:
                    continue
                seen_links.add(clean_link)
                
                srv_inbounds = [ib for s, ib in inbounds if s["name"] == srv_name]
                if len(srv_inbounds) > 1:
                    inbound_names = ", ".join([ib["name"] for ib in srv_inbounds])
                    remark = f"{get_flag_emoji(srv.get('country', ''))} {srv_name.upper()} ({inbound_names})"
                else:
                    remark = f"{get_flag_emoji(srv.get('country', ''))} {srv_name.upper()} ({inbound['name']})"
                
                all_links.append(f"{clean_link}#{remark}")
        
        set_cached_links(subscription_id, all_links)
    
    if not all_links:
        raise HTTPException(404, "No links found")
    
    if format == "json":
        return {"links": all_links, "count": len(all_links), "tier": tier}
    
    lines = []
    motd_text = get_motd()
    announce_header = ""
    
    if motd_text:
        announce_header = f"base64:{base64.b64encode(motd_text.encode()).decode()}"
    elif settings.get("announcement"):
        announce_header = f"base64:{base64.b64encode(settings['announcement'].encode()).decode()}"
    
    host = settings.get("general", {}).get("host", "conn.zernmc.ru")
    title = settings.get("general", {}).get("title", "ZernProxy")
    support_url = settings.get("general", {}).get("support_url", "")
    update_interval = 12
    
    web_url = f"https://{host}/sub/{subscription_id}"
    lines.append(f"#profile-web-page-url: {web_url}")
    lines.append(f"#profile-title: {title}")
    lines.append(f"#profile-update-interval: {update_interval}")
    lines.append("#hide-settings: 1")
    if support_url:
        lines.append(f"#support-url: {support_url}")
    
    expire_ts = 0
    if user.get("tariff_days_remaining", 0) > 0:
        expire_ts = int((datetime.now() + timedelta(days=user["tariff_days_remaining"])).timestamp())
        lines.append("#sub-expire: 1")
        if support_url:
            lines.append(f"#sub-expire-button-link: {support_url}")
    
    traffic_limit = user.get("traffic_limit_gb") or tier_config.get("traffic_limit_gb", 0)
    traffic_limit_bytes = traffic_limit * 1073741824 if traffic_limit > 0 else 0
    
    traffic = get_cached_traffic(subscription_id)
    if not traffic:
        traffic = get_traffic_stats(subscription_id)
        set_cached_traffic(subscription_id, traffic)
    upload = traffic.get("total_up", 0)
    download = traffic.get("total_down", 0)
    
    lines.append(f"#subscription-userinfo: upload={upload}; download={download}; total={traffic_limit_bytes}; expire={expire_ts}")
    
    if announce_header:
        lines.append(f"#announce: {announce_header}")
    
    lines.extend(all_links)
    content = "\n".join(lines)
    
    headers = {
        "Profile-Title": title,
        "Profile-Update-Interval": str(update_interval),
        "Profile-Web-Page-Url": web_url,
        "Subscription-Userinfo": f"upload={upload}; download={download}; total={traffic_limit_bytes}; expire={expire_ts}",
        "Content-Disposition": f"attachment; filename={title}_{user['username']}",
    }
    if announce_header:
        headers["Announce"] = announce_header
    if support_url:
        headers["Support-Url"] = support_url
    headers["Hide-Settings"] = "1"
    if expire_ts:
        headers["Sub-Expire"] = "1"
        if support_url:
            headers["Sub-Expire-Button-Link"] = support_url
    
    if format == "base64":
        return Response(content=base64.b64encode(content.encode()).decode(), media_type="text/plain; charset=utf-8", headers=headers)
    return Response(content=content, media_type="text/plain; charset=utf-8", headers=headers)

async def get_web_page(subscription_id: str):
    user, tier = get_user_tier(subscription_id)
    if not user:
        raise HTTPException(404, "User not found")
    
    tier_config = settings.get("tiers", {}).get(tier, {})
    servers_for_tier = get_servers_for_tier(tier)
    inbounds = deduplicate_inbounds(servers_for_tier, tier)
    
    host = settings.get("general", {}).get("host", "conn.zernmc.ru")
    title = settings.get("general", {}).get("title", "ZernProxy")
    announcement = get_motd() or settings.get("announcement", "")
    da_config = settings.get("payments", {}).get("donationalerts", {})
    
    sub_url = f"https://{host}/sub/{subscription_id}"
    qr_base64 = generate_qr_base64(sub_url, size=300)
    logo_base64 = get_logo_base64()
    
    logo_html = f'<img src="data:image/png;base64,{logo_base64}" alt="Logo" class="logo-img">' if logo_base64 else '<div class="logo-emoji">⚡</div>'
    announcement_html = f'<div class="announcement">{announcement}</div>' if announcement else ''
    
    tier_color = "#4CAF50" if tier == "paid" else "#757575"
    tier_name = tier_config.get("name", "Free")
    tier_badge = f'<span class="tier-badge" style="background: {tier_color}">{tier_name}</span>'
    
    days_remaining = user.get("tariff_days_remaining", 0)
    days_info = f"<p>⏳ Осталось дней: {days_remaining}</p>" if tier == "paid" and days_remaining > 0 else ""
    
    traffic = get_cached_traffic(subscription_id)
    if not traffic:
        traffic = get_traffic_stats(subscription_id)
        set_cached_traffic(subscription_id, traffic)
    traffic_limit_val = user.get('traffic_limit_gb') or tier_config.get('traffic_limit_gb', 0)
    traffic_limit_str = "∞" if traffic_limit_val == 0 else f"{traffic_limit_val} GB"
    traffic_info = f"<p>📊 Лимит: {traffic_limit_str}</p><p>⬆️ {format_bytes(traffic['total_up'])} | ⬇️ {format_bytes(traffic['total_down'])}</p>"
    
    traffic_details = ""
    if traffic.get("by_server"):
        for srv_name, data in traffic["by_server"].items():
            if data["up"] or data["down"]:
                traffic_details += f'<div class="traffic-server"><span class="server-name">{srv_name}</span>: <span class="traffic-values">⬆ {format_bytes(data["up"])} ⬇ {format_bytes(data["down"])}</span></div>'
    
    info_html = f'<div class="info-block">{days_info}{traffic_info}{traffic_details}</div>' if days_info or traffic_info or traffic_details else ''
    
    servers_html = "".join(f'<span class="server-tag">{get_flag_emoji(srv.get("country", ""))} {srv["name"].upper()}</span>' for srv in servers_for_tier)
    
    support_btn = ""
    if da_config.get("enabled"):
        da_url = da_config.get("url", "#")
        support_btn = f'''
        <a href="{da_url}" class="btn btn-support" target="_blank">
            <span class="material-icons">favorite</span> Поддержать проект
        </a>
        '''
    
    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Roboto', sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); min-height: 100vh; color: #fff; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .card {{ background: rgba(255,255,255,0.05); backdrop-filter: blur(10px); border-radius: 16px; padding: 24px; margin-bottom: 20px; border: 1px solid rgba(255,255,255,0.1); }}
        .header {{ text-align: center; padding: 20px 0; }}
        .logo-img {{ width: 80px; height: 80px; border-radius: 12px; }}
        .logo-emoji {{ font-size: 60px; }}
        h1 {{ font-size: 24px; font-weight: 500; margin: 16px 0; }}
        .tier-badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500; text-transform: uppercase; }}
        .announcement {{ background: rgba(76,175,80,0.2); border-left: 4px solid #4CAF50; padding: 12px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }}
        .info-block {{ background: rgba(255,255,255,0.05); border-radius: 12px; padding: 16px; margin-bottom: 20px; }}
        .info-block p {{ display: flex; align-items: center; gap: 8px; margin: 8px 0; font-size: 14px; }}
        .traffic-server {{ font-size: 12px; color: #aaa; margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.1); }}
        .traffic-server .server-name {{ color: #888; }}
        .traffic-server .traffic-values {{ color: #4CAF50; }}
        .qr-container {{ text-align: center; padding: 20px; }}
        .qr-container img {{ border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }}
        .sub-url {{ background: rgba(255,255,255,0.1); padding: 12px; border-radius: 8px; word-break: break-all; font-size: 12px; color: #aaa; margin-top: 12px; }}
        .btn {{ display: inline-flex; align-items: center; gap: 8px; padding: 12px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; text-decoration: none; transition: all 0.3s; }}
        .btn-primary {{ background: #4CAF50; color: #fff; }}
        .btn-primary:hover {{ background: #45a049; transform: translateY(-2px); }}
        .btn-support {{ background: #E91E63; color: #fff; margin-top: 16px; }}
        .btn-support:hover {{ background: #C2185B; }}
        .servers {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .server-tag {{ background: rgba(255,255,255,0.1); padding: 6px 12px; border-radius: 20px; font-size: 12px; }}
        .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
        @media (max-width: 480px) {{ .container {{ padding: 12px; }} .card {{ padding: 16px; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="card header">
            {logo_html}
            <h1>{title}</h1>
            {tier_badge}
        </div>
        {announcement_html}
        <div class="card">
            {info_html}
            <div class="servers">{servers_html}</div>
        </div>
        <div class="card">
            <div class="qr-container">
                <img src="data:image/png;base64,{qr_base64}" alt="QR Code">
                <div class="sub-url">{sub_url}</div>
                <a href="{sub_url}" class="btn btn-primary" style="margin-top: 16px;">
                    <span class="material-icons">download</span> Скачать подписку
                </a>
                {support_btn}
            </div>
        </div>
        <div class="footer">
            <p>© 2026 {title}</p>
        </div>
    </div>
</body>
</html>'''
    return HTMLResponse(content=html.replace("{sub_url}", sub_url).replace("{host}", host))

@app.post("/payment/webhook/donationalerts")
async def webhook_donationalerts(request: Request):
    try:
        data = await request.json()
    except:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    
    amount = data.get("amount", 0)
    username = data.get("username", "")
    message = data.get("message", "")
    
    if amount not in [150, 990]:
        return JSONResponse({"status": "ignored", "reason": "not_vpn_payment"})
    
    user = None
    message_parts = message.split() if message else []
    
    for part in message_parts:
        conn = get_db()
        try:
            if part.isdigit():
                user = conn.execute("SELECT * FROM users WHERE id = ?", (int(part),)).fetchone()
            if not user:
                user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (part,)).fetchone()
            if user:
                break
        finally:
            conn.close()
    
    if not user and username:
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM username = ? COLLATE NOCASE", (username,)).fetchone()
        finally:
            conn.close()
    
    if not user:
        return JSONResponse({"status": "ignored", "reason": "user_not_found"})
    
    days = 30 if amount == 150 else 365
    
    conn = get_db()
    try:
        conn.execute("""
            UPDATE users SET 
                tier = 'paid',
                tariff_days_bought = tariff_days_bought + ?,
                tariff_days_remaining = tariff_days_remaining + ?,
                total_paid_rubles = total_paid_rubles + ?
            WHERE id = ?
        """, (days, days, amount, user["id"]))
        conn.commit()
    finally:
        conn.close()
    
    logger.info(f"VPN payment: {username} paid {amount} RUB, +{days} days")
    return JSONResponse({"status": "ok", "user": user["username"], "days": days})

@app.get("/admin/users")
async def admin_users(request: Request):
    if not check_admin(request):
        return HTMLResponse(content=ADMIN_LOGIN_PAGE)
    conn = get_db()
    try:
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    
    da_config = settings.get("payments", {}).get("donationalerts", {})
    
    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Управление - {settings.get("general", {}).get("title", "ZernProxy")}</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Roboto', sans-serif; background: #121212; min-height: 100vh; color: #fff; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }}
        h1 {{ font-size: 24px; font-weight: 500; }}
        .btn {{ display: inline-flex; align-items: center; gap: 8px; padding: 10px 20px; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; }}
        .btn-small {{ padding: 6px 10px; font-size: 12px; background: #333; color: #fff; border: none; border-radius: 4px; cursor: pointer; }}
        .btn-small:hover {{ background: #444; }}
        .btn-danger {{ background: #f44336; }}
        .btn-danger:hover {{ background: #d32f2f; }}
        .btn-primary {{ background: #4CAF50; color: #fff; }}
        .card {{ background: #1e1e1e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
        .table {{ width: 100%; border-collapse: collapse; }}
        .table th, .table td {{ padding: 12px; text-align: left; border-bottom: 1px solid #333; }}
        .table th {{ color: #aaa; font-weight: 500; font-size: 12px; text-transform: uppercase; }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
        .badge-free {{ background: #757575; }}
        .badge-paid {{ background: #4CAF50; }}
        .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; }}
        .modal.active {{ display: flex; align-items: center; justify-content: center; }}
        .modal-content {{ background: #1e1e1e; border-radius: 12px; padding: 24px; max-width: 500px; width: 90%; }}
        .form-group {{ margin-bottom: 16px; }}
        .form-group label {{ display: block; margin-bottom: 8px; color: #aaa; font-size: 14px; }}
        .form-group input {{ width: 100%; padding: 12px; border: 1px solid #333; border-radius: 8px; background: #2a2a2a; color: #fff; font-size: 14px; }}
        @media (max-width: 768px) {{ .table {{ display: block; overflow-x: auto; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>👥 Пользователи ({len(users)})</h1>
            <button class="btn btn-primary" onclick="openModal()">
                <span class="material-icons">add</span> Добавить
            </button>
        </div>
        <div class="card">
            <table class="table">
                <thead>
                    <tr><th>ID</th><th>Username</th><th>Sub ID</th><th>Тариф</th><th>Дней</th><th>Оплачено</th><th>Трафик</th><th>Статус</th><th>Создан</th></tr>
                </thead>
                <tbody>
                    {"".join(f'''<tr>
                        <td>{u['id']}</td>
                        <td>{u['username']}</td>
                        <td><code style="font-size:10px">{u['subscription_id']}</code></td>
                        <td><span class="badge badge-{u['tier']}">{u['tier'].upper()}</span></td>
                        <td>{u['tariff_days_remaining']}</td>
                        <td>{u['total_paid_rubles']}₽</td>
                        <td>{u['traffic_limit_gb'] if u['traffic_limit_gb'] > 0 else '∞'} GB</td>
                        <td>{"✓" if u['is_active'] else "✗"}</td>
                        <td>{u['created_at'][:10]}</td>
                        <td>
                            <button class="btn-small" onclick="editUser({u['id']}, '{u['tier']}', {u['tariff_days_remaining']}, {u['traffic_limit_gb']}, {u['is_active']})">✏️</button>
                            <button class="btn-small btn-danger" onclick="deleteUser({u['id']}, '{u['username']}')">🗑️</button>
                        </td>
                    </tr>''' for u in users)}
                </tbody>
            </table>
        </div>
    </div>
    <div class="modal" id="userModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Добавить пользователя</h2>
                <button class="close-btn" onclick="closeModal()">×</button>
            </div>
            <form id="userForm">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" required>
                </div>
                <div class="form-group">
                    <label>Лимит трафика (GB, 0 = ∞)</label>
                    <input type="number" name="traffic_limit_gb" value="0" min="0">
                </div>
                <button type="submit" class="btn btn-primary">Создать</button>
            </form>
        </div>
    </div>
    <script>
        function openModal() {{ document.getElementById('userModal').classList.add('active'); }}
        function closeModal() {{ document.getElementById('userModal').classList.remove('active'); }}
        document.getElementById('userForm').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const formData = new FormData(e.target);
            const username = formData.get('username');
            const btn = e.target.querySelector('button');
            btn.disabled = true;
            btn.textContent = 'Создание...';
            
            const response = await fetch('/admin/api/users', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    username,
                    traffic_limit_gb: parseInt(formData.get('traffic_limit_gb')) || 0
                }})
            }});
            
            const result = await response.json();
            
            if (response.ok) {{
                let msg = `Пользователь \\`${{username}}\\` создан!\\n`;
                msg += `Sub ID: \\`${{result.subscription_id}}\\`\\n`;
                msg += `Подписка: ${{result.subscription_url}}\\n\\n`;
                msg += result.results.map(r => {{
                    const ibs = r.inbounds.map(ib => `${{ib.name}}: ${{ib.success ? '✅' : '❌ ' + (ib.error || '')}}`).join('\\n');
                    return `${{r.server}}:\\n${{ibs}}`;
                }}).join('\\n\\n');
                msg += `\\n\\n${{result.summary}}`;
                alert(msg);
                location.reload();
            }} else {{
                alert('Ошибка: ' + (result.error || 'unknown'));
                btn.disabled = false;
                btn.textContent = 'Создать';
            }}
        }});
        
        function editUser(id, tier, days, traffic, active) {{
            document.getElementById('editId').value = id;
            document.getElementById('editTier').value = tier;
            document.getElementById('editDays').value = days;
            document.getElementById('editTraffic').value = traffic;
            document.getElementById('editActive').checked = active == 1;
            document.getElementById('editModal').classList.add('active');
        }}
        
        function closeEditModal() {{
            document.getElementById('editModal').classList.remove('active');
        }}
        
        async function submitEditForm() {{
            const form = document.getElementById('editForm');
            const formData = new FormData(form);
            const data = {{
                id: formData.get('id'),
                tier: formData.get('tier'),
                tariff_days_remaining: parseInt(formData.get('tariff_days_remaining')) || 0,
                traffic_limit_gb: formData.get('traffic_limit_gb') === '' ? 0 : parseInt(formData.get('traffic_limit_gb')),
                is_active: formData.get('is_active') === 'on'
            }};
            
            const response = await fetch('/admin/api/users/update', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(data)
            }});
            
            if (response.ok) {{
                alert('Сохранено!');
                location.reload();
            }} else {{
                const result = await response.json();
                alert('Ошибка: ' + (result.error || 'unknown'));
            }}
        }}
        
        function deleteUser(id, username) {{
            if (confirm('Удалить пользователя ' + username + '? Это удалит его со всех серверов!')) {{
                fetch('/admin/api/users/delete', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{id, username}})
                }}).then(r => {{
                    if (r.ok) {{
                        alert('Удалён!');
                        location.reload();
                    }} else {{
                        alert('Ошибка удаления');
                    }}
                }});
            }}
        }}
    </script>
    <div class="modal" id="editModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Редактировать пользователя</h2>
                <button class="close-btn" onclick="closeEditModal()">×</button>
            </div>
            <form id="editForm">
                <input type="hidden" name="id" id="editId">
                <div class="form-group">
                    <label>Тариф</label>
                    <select name="tier" id="editTier">
                        <option value="free">Free</option>
                        <option value="paid">Paid</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Дней осталось</label>
                    <input type="number" name="tariff_days_remaining" id="editDays">
                </div>
                <div class="form-group">
                    <label>Лимит трафика (GB, 0 = ∞)</label>
                    <input type="number" name="traffic_limit_gb" id="editTraffic">
                </div>
                <div class="form-group">
                    <label><input type="checkbox" name="is_active" id="editActive"> Активен</label>
                </div>
                <button type="button" class="btn btn-primary" onclick="submitEditForm()">Сохранить</button>
            </form>
        </div>
    </div>
</body>
</html>'''
    return HTMLResponse(content=html)

@app.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    if not check_admin(request):
        return HTMLResponse(content=ADMIN_LOGIN_PAGE)
    
    conn = get_db()
    try:
        total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        free_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE tier='free'").fetchone()["c"]
        paid_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE tier='paid'").fetchone()["c"]
        test_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE tier='test'").fetchone()["c"]
        total_revenue = conn.execute("SELECT SUM(total_paid_rubles) as s FROM users").fetchone()["s"] or 0
        total_donations = conn.execute("SELECT SUM(total_paid_rubles) as s FROM users WHERE total_paid_rubles > 0").fetchone()["s"] or 0
    finally:
        conn.close()
    
    online_count = 0
    for srv in servers:
        for inbound in srv.get("inbounds", []):
            try:
                api = Api(host=inbound["api_host"], username=inbound["api_user"], password=inbound["api_pass"], use_tls_verify=False)
                api.login()
                online = api.client.online()
                online_count += len(online)
            except:
                pass
    
    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Дашборд - ZernProxy</title>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Roboto,sans-serif;background:#121212;color:#fff;min-height:100vh}}
.container{{max-width:1200px;margin:0 auto;padding:20px}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}}
h1{{font-size:24px}}
.nav{{display:flex;gap:12px}}
.nav a{{color:#4CAF50;text-decoration:none;padding:8px 16px;border-radius:8px}}
.nav a:hover{{background:#2a2a2a}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:24px}}
.card{{background:#1e1e1e;border-radius:12px;padding:20px}}
.card h3{{color:#aaa;font-size:14px;font-weight:400;margin-bottom:8px}}
.card .val{{font-size:28px;font-weight:500}}
.card .sub{{font-size:12px;color:#666;margin-top:4px}}
.green{{color:#4CAF50}}.blue{{color:#2196F3}}.orange{{color:#FF9800}}.red{{color:#f44336}}
@media(max-width:600px){{.header{{flex-direction:column;align-items:start}}}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>📊 Дашборд</h1>
<div class="nav">
<a href="/admin/users">👥 Пользователи</a>
<a href="/admin/dashboard">📊 Дашборд</a>
</div>
</div>
<div class="grid">
<div class="card"><h3>Всего пользователей</h3><div class="val green">{total_users}</div></div>
<div class="card"><h3>Free</h3><div class="val blue">{free_users}</div></div>
<div class="card"><h3>Test</h3><div class="val orange">{test_users}</div></div>
<div class="card"><h3>Paid</h3><div class="val green">{paid_users}</div></div>
<div class="card"><h3>Онлайн</h3><div class="val blue">{online_count}</div><div class="sub">на всех серверах</div></div>
<div class="card"><h3>Выручка</h3><div class="val green">{total_revenue}₽</div><div class="sub">всего оплат</div></div>
<div class="card"><h3>Серверов</h3><div class="val blue">{len(servers)}</div></div>
<div class="card"><h3>Инбаундов</h3><div class="val orange">{sum(len(s.get("inbounds",[])) for s in servers)}</div></div>
</div>
<div class="card">
<h3>Серверы</h3>
{"".join(f'<div style="margin-top:12px;padding:12px;background:#2a2a2a;border-radius:8px;display:flex;justify-content:space-between"><span>{get_flag_emoji(s.get("country",""))} {s["name"].upper()}</span><span style="color:#aaa">{len(s.get("inbounds",[]))} inbound</span></div>' for s in servers)}
</div>
</div>
</body>
</html>'''
    return HTMLResponse(content=html)

@app.get("/")
async def home_page():
    title = settings.get("general", {}).get("title", "ZernProxy")
    return HTMLResponse(content=f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Roboto,sans-serif;background:linear-gradient(135deg,#1a1a2e,#16213e);min-height:100vh;color:#fff}}
.container{{max-width:700px;margin:0 auto;padding:40px 20px;text-align:center}}
.logo{{font-size:64px;margin-bottom:20px}}
h1{{font-size:32px;font-weight:500;margin-bottom:16px}}
p{{color:#aaa;font-size:16px;line-height:1.6;margin-bottom:32px}}
.btn{{display:inline-block;padding:14px 32px;background:#4CAF50;color:#fff;text-decoration:none;border-radius:12px;font-size:16px;font-weight:500;margin:8px;transition:.3s}}
.btn:hover{{background:#45a049;transform:translateY(-2px)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:40px}}
.card{{background:rgba(255,255,255,0.05);border-radius:16px;padding:24px;border:1px solid rgba(255,255,255,0.1)}}
.card .icon{{font-size:32px;margin-bottom:12px}}
.card h3{{font-size:16px;margin-bottom:8px}}
.card p{{font-size:13px;color:#888}}
.footer{{margin-top:60px;color:#555;font-size:13px}}
</style>
</head>
<body>
<div class="container">
<div class="logo">⚡</div>
<h1>{title}</h1>
<p>Быстрый и надёжный VPN. Подписка на основе подписки. Безлимитный трафик на всех тарифах.</p>
<div class="cards">
<div class="card"><div class="icon">🆓</div><h3>Free</h3><p>Базовый доступ к серверам. Безлимитный трафик.</p></div>
<div class="card"><div class="icon">🧪</div><h3>Test 7 дней</h3><p>Пробный период за 50₽. 5GB трафика.</p></div>
<div class="card"><div class="icon">⭐</div><h3>Premium</h3><p>Полный доступ, приоритетные серверы. От 150₽/мес.</p></div>
</div>
<div style="margin-top:32px">
<a href="/admin/users" class="btn">👤 Панель управления</a>
<a href="{settings.get("payments",{}).get("donationalerts",{}).get("url","#")}" class="btn" style="background:#E91E63">❤️ Поддержать проект</a>
</div>
<div class="footer">© 2026 {title} · <a href="/admin/users" style="color:#555">admin</a></div>
</div>
</body>
</html>''')

@app.get("/webhook/tg")
async def tg_webhook():
    bot_cfg = settings.get("bot", {})
    if bot_cfg.get("enabled"):
        return JSONResponse({"status": "ok", "bot": True, "message": "Telegram bot is ready"})
    return JSONResponse({"status": "ok", "bot": False, "message": "Telegram bot not configured"})

def create_3xui_client(username: str, sub_id: str, inbound: dict, traffic_gb: int = 0) -> dict:
    """Создаёт клиента на 3x-UI сервере"""
    api_host = inbound.get("api_host")
    api_user = inbound.get("api_user")
    api_pass = inbound.get("api_pass")
    inbound_id = inbound.get("id")
    
    if not all([api_host, api_user, api_pass, inbound_id]):
        return {"success": False, "error": "missing_credentials"}
    
    try:
        api = Api(host=api_host, username=api_user, password=api_pass, use_tls_verify=False)
        api.login()
        
        inbound_name = inbound.get('name', 'default')
        email = f"{username}_{inbound_name}@vless.local"
        total_bytes = traffic_gb * 1073741824 if traffic_gb > 0 else 0
        
        logger.info(f"Creating client: email={email}, total_bytes={total_bytes}, inbound={inbound_name}")
        
        from py3xui.client import Client
        client = Client(
            id=str(uuid.uuid4()),
            email=email,
            enable=True,
            total_gb=total_bytes,
            expiry_time=0,
            limit_ip=0,
            subId=sub_id
        )
        
        try:
            existing = api.client.get_by_email(email)
            if existing:
                logger.info(f"Deleting existing client: {existing.id}")
                api.client.delete(existing.id, inbound_id)
        except:
            pass
        
        logger.info(f"About to add client: {client}")
        api.client.add(inbound_id=inbound_id, clients=[client])
        logger.info(f"Client created successfully on {inbound_name}")
        return {"success": True, "email": email}
        
    except Exception as e:
        logger.error(f"Error creating client: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"success": False, "error": str(e)[:150]}

def delete_3xui_client(username: str, sub_id: str, inbound: dict) -> dict:
    """Удаляет клиента с 3x-UI сервера"""
    api_host = inbound.get("api_host")
    api_user = inbound.get("api_user")
    api_pass = inbound.get("api_pass")
    inbound_id = inbound.get("id")
    
    if not all([api_host, api_user, api_pass, inbound_id]):
        return {"success": False, "error": "missing_credentials"}
    
    try:
        api = Api(host=api_host, username=api_user, password=api_pass, use_tls_verify=False)
        api.login()
        
        inbounds = api.inbound.get_list()
        for ib in inbounds:
            if ib.id == inbound_id and ib.client_stats:
                for client in ib.client_stats:
                    if getattr(client, 'sub_id', '') == sub_id:
                        api.client.delete(inbound_id, client.uuid)
                        logger.info(f"Deleted client from {inbound.get('name')}")
                        return {"success": True}
        
        return {"success": True, "error": "not_found"}
        
    except Exception as e:
        return {"success": False, "error": str(e)[:100]}

@app.post("/admin/api/users")
async def create_user(request: Request, data: dict):
    if not check_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    username = data.get("username", "").strip()
    if not username:
        return JSONResponse({"error": "username required"}, status_code=400)
    
    sub_id = generate_sub_id()
    conn = get_db()
    
    traffic_gb = int(data.get("traffic_limit_gb", 0) or 0)
    logger.info(f"Creating user: username={username}, traffic_gb={traffic_gb}")
    
    results = []
    for srv in servers:
        srv_result = {"server": srv["name"], "inbounds": []}
        for inbound in srv.get("inbounds", []):
            result = create_3xui_client(username, sub_id, inbound, traffic_gb)
            srv_result["inbounds"].append({
                "name": inbound.get("name"),
                "success": result["success"],
                "error": result.get("error", "")
            })
        results.append(srv_result)
    
    try:
        conn.execute("""
            INSERT INTO users (username, subscription_id, tier, tariff_days_bought, tariff_days_remaining, total_paid_rubles, traffic_limit_gb, is_active)
            VALUES (?, ?, 'free', 0, 0, 0, 0, 1)
        """, (username, sub_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse({"error": "username exists"}, status_code=400)
    finally:
        conn.close()
    
    success_count = sum(1 for r in results for ib in r["inbounds"] if ib["success"])
    total_count = sum(len(r["inbounds"]) for r in results)
    
    return JSONResponse({
        "status": "ok",
        "username": username,
        "subscription_id": sub_id,
        "subscription_url": f"https://{settings.get('general', {}).get('host', 'conn.zernmc.ru')}/sub/{sub_id}",
        "results": results,
        "summary": f"{success_count}/{total_count} инбаундов создано"
    })

@app.post("/admin/api/users/update")
async def update_user(request: Request, data: dict):
    if not check_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    logger.info(f"RAW data received: {data}")
    
    user_id = int(data.get("id", 0))
    tier = str(data.get("tier", "free"))
    tariff_days_remaining = int(data.get("tariff_days_remaining", 0) or 0)
    traffic_limit_gb = int(data.get("traffic_limit_gb", 0) or 0)
    is_active = 1 if data.get("is_active") in [True, "true", "on", "1", 1] else 0
    
    logger.info(f"Update user: id={user_id}, tier='{tier}', days={tariff_days_remaining}, traffic={traffic_limit_gb}, active={is_active}")
    
    conn = get_db()
    try:
        conn.execute("""
            UPDATE users SET tier = ?, tariff_days_remaining = ?, traffic_limit_gb = ?, is_active = ?
            WHERE id = ?
        """, (tier, tariff_days_remaining, traffic_limit_gb, is_active, user_id))
        conn.commit()
        
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        logger.info(f"After update DB: {dict(updated)}")
    finally:
        conn.close()
    
    return JSONResponse({"status": "ok"})

@app.post("/admin/api/users/delete")
async def delete_user(request: Request, data: dict):
    if not check_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id = data.get("id")
    username = data.get("username", "")
    
    conn = get_db()
    try:
        user = conn.execute("SELECT subscription_id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return JSONResponse({"error": "user not found"}, status_code=404)
        
        sub_id = user["subscription_id"]
        
        for srv in servers:
            for inbound in srv.get("inbounds", []):
                result = delete_3xui_client(username, sub_id, inbound)
        
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    
    return JSONResponse({"status": "ok", "message": "User deleted from all servers"})

async def rotate_shortids():
    while True:
        try:
            rotation_hours = settings.get("shortid_rotation_hours", 11)
            await asyncio.sleep(rotation_hours * 3600)
            
            for srv in servers:
                for inbound in srv.get("inbounds", []):
                    api_host = inbound.get("api_host")
                    api_user = inbound.get("api_user")
                    api_pass = inbound.get("api_pass")
                    
                    if not all([api_host, api_user, api_pass]):
                        continue
                    
                    try:
                        api = Api(host=api_host, username=api_user, password=api_pass, use_tls_verify=False)
                        api.login()
                        
                        new_shortid = secrets.token_hex(4)
                        
                        inbounds = api.inbound.get_inbounds()
                        for ib in inbounds:
                            if ib.id == inbound.get("id"):
                                ib.shortid = new_shortid
                                api.inbound.update_inbound(ib)
                                logger.info(f"ShortID rotated for {srv['name']}/{inbound['name']}: {new_shortid}")
                                break
                    except Exception as e:
                        logger.error(f"ShortID rotation error {srv['name']}/{inbound['name']}: {e}")
        except Exception as e:
            logger.error(f"ShortID rotation error: {e}")
            await asyncio.sleep(3600)

def update_3xui_expiry(sub_id: str, days: int):
    """Обновляет expiry_time на всех 3x-UI серверах"""
    import time
    expiry_timestamp = int(time.time() + (days * 86400))
    
    for srv in servers:
        for inbound in srv.get("inbounds", []):
            api_host = inbound.get("api_host")
            api_user = inbound.get("api_user")
            api_pass = inbound.get("api_pass")
            inbound_id = inbound.get("id")
            
            if not all([api_host, api_user, api_pass, inbound_id]):
                continue
            
            try:
                api = Api(host=api_host, username=api_user, password=api_pass, use_tls_verify=False)
                api.login()
                
                inbounds = api.inbound.get_list()
                for ib in inbounds:
                    if ib.id == inbound_id and ib.client_stats:
                        for client in ib.client_stats:
                            if getattr(client, 'sub_id', '') == sub_id:
                                client.expiry_time = expiry_timestamp
                                api.client.update(client.uuid, client)
                                logger.info(f"Updated expiry for {srv['name']}/{inbound['name']}: {days} days")
                                break
            except Exception as e:
                logger.error(f"Error updating expiry {srv['name']}/{inbound['name']}: {e}")

async def poll_donationalerts():
    global last_donation_id
    while True:
        try:
            da_config = settings.get("payments", {}).get("donationalerts", {})
            if not da_config.get("enabled"):
                await asyncio.sleep(300)
                continue
            
            api_token = da_config.get("api_token", "")
            if not api_token:
                logger.warning("DonationAlerts API token not configured")
                await asyncio.sleep(300)
                continue
            
            interval = da_config.get("check_interval_minutes", 5)
            await asyncio.sleep(interval * 60)
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://www.donationalerts.com/api/v1/alerts/donations",
                    headers={"Authorization": f"Bearer {api_token}"}
                )
                
                if resp.status_code != 200:
                    logger.error(f"DA API error: {resp.status_code}")
                    continue
                
                data = resp.json()
                donations = data.get("data", [])
                
                for donation in reversed(donations):
                    donation_id = donation.get("id")
                    if donation_id <= last_donation_id:
                        continue
                    
                    amount = donation.get("amount", 0)
                    username = donation.get("username", "")
                    message = donation.get("message", "")
                    
                    if amount not in [150, 990]:
                        last_donation_id = donation_id
                        continue
                    
                    user = None
                    message_parts = message.split() if message else []
                    
                    conn = get_db()
                    try:
                        for part in message_parts:
                            if part.isdigit():
                                user = conn.execute("SELECT * FROM users WHERE id = ?", (int(part),)).fetchone()
                            if not user:
                                user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (part,)).fetchone()
                            if user:
                                break
                        
                        if not user and username:
                            user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
                        
                        if user:
                            tier = "paid"
                            if amount == 50:
                                tier = "test"
                                days = 7
                            elif amount == 150:
                                days = 30
                            elif amount == 990:
                                days = 365
                            else:
                                continue
                            
                            current_expiry = user.get("tariff_days_remaining", 0)
                            if current_expiry > 0:
                                new_expiry = current_expiry + days
                            else:
                                new_expiry = days
                            
                            conn.execute("""
                                UPDATE users SET 
                                    tier = ?,
                                    tariff_days_bought = tariff_days_bought + ?,
                                    tariff_days_remaining = ?,
                                    total_paid_rubles = total_paid_rubles + ?
                                WHERE id = ?
                            """, (tier, days, new_expiry, amount, user["id"]))
                            conn.commit()
                            
                            sub_id = user["subscription_id"]
                            update_3xui_expiry(sub_id, days)
                            
                            logger.info(f"VPN payment: {username} paid {amount} RUB, tier={tier}, +{days} days")
                    finally:
                        conn.close()
                    
                    last_donation_id = donation_id
                    
        except Exception as e:
            logger.error(f"DonationAlerts polling error: {e}")
            await asyncio.sleep(300)

@app.get("/health")
async def health():
    return {"status": "ok", "servers": len(servers), "settings_loaded": bool(settings)}

@app.post("/admin/api/reload")
async def reload_configs(request: Request):
    if not check_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    load_configs()
    clear_cache()
    return {"status": "ok"}

@app.get("/admin/api/rotate-shortids")
async def manual_rotate(request: Request):
    if not check_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    await rotate_shortids()
    return {"status": "ok"}

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(rotate_shortids())
    loop.create_task(poll_donationalerts())
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")