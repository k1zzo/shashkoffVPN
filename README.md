# SHASHKOFFVPN

Subscription-only VPN service. Users connect through the Happ client — no manual config import needed.

## How it works

1. Each user gets a personal link: `/{token}` (the cabinet page)
2. The main action is "Добавить в подписку" (Add to subscription)
3. This opens a Happ deep-link: `happ://add/<encoded /sub/{token}>`
4. Happ fetches `/sub/{token}`, imports the VLESS URL + subscription headers
5. Happ manages device limits client-side via HWID headers

## Endpoints

| Route | Description |
|---|---|
| `GET /health` | Health check — returns `{"status":"ok"}` |
| `GET /{token}` | Personal user cabinet (main shareable link) |
| `GET /u/{token}` | Alias for `/{token}` |
| `GET /sub/{token}` | Happ subscription endpoint — VLESS URL + metadata headers |
| `POST /api/device/register` | Register or update a device (enforces per-user limit) |
| `POST /api/device/remove` | Deactivate a device |
| `GET /api/profile/{token}?device_id=` | Return VPN profile JSON, auto-register device |
| `GET /open/{token}` | Internal — return raw VLESS URL (`?mode=raw\|json\|download`) |

## Access policy

A user is **active** when both are true:
- `is_active = True`
- `expires_at` is NULL or in the future

Expired / inactive users are blocked from all endpoints except device removal (cleanup).

## User management (CLI)

```bash
# Create user
python -m backend.cli create-user --username alice --device-limit 5
python -m backend.cli create-user --username bob --token bob123 --expires-days 90

# List / inspect
python -m backend.cli list-users
python -m backend.cli get-user --token alice-token

# Activate / deactivate
python -m backend.cli activate-user --token alice-token
python -m backend.cli deactivate-user --token alice-token

# Extend or set expiry
python -m backend.cli extend-user --token alice-token --days 30
python -m backend.cli set-expiry --token alice-token --expires-at 2026-12-31T23:59:59
python -m backend.cli set-expiry --token alice-token --expires-at never

# Replace token
python -m backend.cli reset-token --token old-token --new-token new-token
```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app:app --reload
```

Open:
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/demo-token`
- `http://127.0.0.1:8000/sub/demo-token`

## Tests

```bash
pip install -r requirements-test.txt
python3 -m pytest tests/ -v
```

## 🚀 Quick deploy (VPS)

### Option A — Docker (recommended)

```bash
# 1. Clone
git clone <YOUR_REPO_URL> /opt/shashkoffvpn
cd /opt/shashkoffvpn

# 2. Configure
cp .env.example .env
nano .env          # fill in VPN_SERVER, VPN_REALITY_PUBLIC_KEY, etc.

# 3. Start
cd docker
docker compose up --build -d
docker compose logs -f app
```

The app binds to `127.0.0.1:8000`. Put a reverse proxy in front (see HTTPS section below).

### Option B — Python directly

```bash
# 1. Clone and enter
git clone <YOUR_REPO_URL> /opt/shashkoffvpn
cd /opt/shashkoffvpn

# 2. Create venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
nano .env

# 4. Run
uvicorn backend.app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 2 \
  --proxy-headers \
  --forwarded-allow-ips="*"
```

Verify:
```bash
curl http://127.0.0.1:8000/health
```

## 🌐 HTTPS (required for Happ)

Happ requires HTTPS to import subscriptions. Use a reverse proxy on the same VPS.

### Caddy (simplest — auto HTTPS)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

`/etc/caddy/Caddyfile`:
```caddy
vpn.your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl reload caddy
```

Caddy handles Let's Encrypt automatically.

### nginx + certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/shashkoffvpn`:
```nginx
server {
    listen 80;
    server_name vpn.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/shashkoffvpn /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Issue certificate (fills in the HTTPS block automatically)
sudo certbot --nginx -d vpn.your-domain.com
```

## Docker run

```bash
cp .env.example .env
cd docker
docker compose up --build -d
docker compose logs -f app
docker compose down
```

SQLite is bind-mounted: container `/app/data/app.db` ↔ host `./data/app.db`.

## Environment variables

| Variable | Default | Required |
|---|---|---|
| `APP_BASE_URL` | _(empty)_ | **Yes in production** — used in all generated links |
| `APP_TRUSTED_HOSTS` | `*` | Recommended in production |
| `VPN_SERVER` | _(placeholder)_ | **Yes** |
| `VPN_PORT` | `443` | Yes |
| `VPN_SNI` | _(placeholder)_ | **Yes** |
| `VPN_REALITY_PUBLIC_KEY` | _(placeholder)_ | **Yes** |
| `VPN_REALITY_SHORT_ID` | _(placeholder)_ | **Yes** |
| `VPN_TRANSPORT` | `tcp` | Optional |
| `APP_NAME` | `SHASHKOFFVPN` | Optional |
| `APP_ENV` | `development` | Optional |
| `DATABASE_PATH` | `data/app.db` | Optional |
| `VPN_PROFILE_NAME_PREFIX` | `SHASHKOFFVPN` | Optional |

`APP_BASE_URL` must be set in production — all subscription URLs, deep links, and fallback URLs are built from it. Without it, URLs are derived from the incoming request, which breaks when sitting behind a proxy.

## Example .env

```bash
APP_BASE_URL=https://vpn.your-domain.com
APP_TRUSTED_HOSTS=vpn.your-domain.com,localhost,127.0.0.1

VPN_SERVER=vpn.your-domain.com
VPN_PORT=443
VPN_SNI=www.cloudflare.com
VPN_REALITY_PUBLIC_KEY=PASTE_REAL_PUBLIC_KEY_HERE
VPN_REALITY_SHORT_ID=abcdef1234567890
VPN_TRANSPORT=tcp
```

## VPN server values (from your Xray Reality config)

- `VPN_SERVER` — public IP or domain of your Xray host
- `VPN_PORT` — VLESS inbound listening port
- `VPN_SNI` — `serverName` for Reality clients
- `VPN_REALITY_PUBLIC_KEY` — output of server keypair generation
- `VPN_REALITY_SHORT_ID` — one entry from `shortIds` in the inbound config

## Post-deploy verification

```bash
curl -i https://vpn.your-domain.com/health
curl -i https://vpn.your-domain.com/demo-token
curl -i https://vpn.your-domain.com/sub/demo-token
curl -s "https://vpn.your-domain.com/api/profile/demo-token?device_id=test-device" | python3 -m json.tool
```

## Manual endpoint testing (local)

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/demo-token
curl -i http://127.0.0.1:8000/sub/demo-token

curl -s -X POST http://127.0.0.1:8000/api/device/register \
  -H "Content-Type: application/json" \
  -d '{"token":"demo-token","device_id":"my-device-1","device_name":"MacBook","platform":"macOS"}'

curl -s "http://127.0.0.1:8000/api/profile/demo-token?device_id=my-device-1" | python3 -m json.tool
```
