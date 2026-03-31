# SHASHKOFFVPN MVP

## What is included

- `GET /health` returns `{"status":"ok"}`
- `GET /u/{token}` renders `templates/user_page.html`
- `POST /api/device/register` registers/updates client devices with limit enforcement
- `GET /api/profile/{token}?device_id=...` auto-registers device when possible and returns VPN profile JSON
- `GET /open/{token}?device_id=...` supports mode-aware responses:
- `mode=json` -> JSON profile
- `mode=raw` -> raw JSON profile
- `mode=download` -> downloadable JSON attachment
- no mode -> helper HTML page (`templates/open_profile.html`)
- profile `meta.warnings` reports incomplete/placeholder production VPN settings
- SQLite bootstrap on startup at `data/app.db`
- SQLAlchemy models: `users`, `devices`
- Environment-based config in `backend/config.py`

## File structure

```text
backend/
  app.py
  config.py
  config_generator.py
  db.py
  models.py
  routes/
    devices.py
    health.py
    profile.py
    user_page.py
templates/
  error.html
  open_profile.html
  user_page.html
static/
  app.js
data/
  app.db   # created automatically on startup
```

## Local run

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the app:

```bash
uvicorn backend.app:app --reload
```

4. Open:

- Health check: `http://127.0.0.1:8000/health`
- User page example: `http://127.0.0.1:8000/u/demo-token`
- Register device: `POST http://127.0.0.1:8000/api/device/register`
- VPN profile JSON: `http://127.0.0.1:8000/api/profile/demo-token?device_id=my-device-1`
- Open helper page: `http://127.0.0.1:8000/open/demo-token?device_id=my-device-1`
- Open download mode: `http://127.0.0.1:8000/open/demo-token?device_id=my-device-1&mode=download`

## Docker run

1. Create runtime env file:

```bash
cp .env.example .env
```

2. Start via Docker Compose:

```bash
cd docker
docker compose up --build
```

3. Open in browser:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/u/demo-token`
- `http://127.0.0.1:8000/api/profile/demo-token?device_id=test-device`

4. Stop services:

```bash
cd docker
docker compose down
```

SQLite persistence:
- Container path: `/app/data/app.db`
- Host path (bind mounted): `./data/app.db`

Production uvicorn command recommendation:

```bash
uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips="*"
```

## Environment variables

- `APP_NAME` (default: `SHASHKOFFVPN`)
- `APP_ENV` (default: `development`)
- `APP_BASE_URL` (default: empty, recommended in production: `https://vpn.your-domain.com`)
- `APP_TRUSTED_HOSTS` (default: `*`, recommended in production: `vpn.your-domain.com,localhost,127.0.0.1`)
- `DATABASE_PATH` (default: `data/app.db`)
- `DATABASE_URL` (overrides `DATABASE_PATH`, expected format: `sqlite:////absolute/path/to/app.db`)
- `TEMPLATES_DIR` (default: `templates`)
- `STATIC_DIR` (default: `static`)
- `VPN_SERVER` (default: `your-vpn-host.example.com`)
- `VPN_PORT` (default: `443`)
- `VPN_SNI` (default: `your-sni.example.com`)
- `VPN_REALITY_PUBLIC_KEY` (default: `CHANGE_ME_REALITY_PUBLIC_KEY`)
- `VPN_REALITY_SHORT_ID` (default: `CHANGE_ME_SHORT_ID`)
- `VPN_TRANSPORT` (default: `tcp`)
- `VPN_PROFILE_NAME_PREFIX` (default: `SHASHKOFFVPN`)
- For Docker deployment, values are loaded from `.env` via `docker/docker-compose.yml`.

## Real deployment values (from Xray Reality server)

- `VPN_SERVER`: public IP or domain of your Xray host.
- `VPN_PORT`: inbound listening port for your VLESS Reality inbound.
- `VPN_SNI`: `serverName` value used by Reality clients.
- `VPN_REALITY_PUBLIC_KEY`: output from your server keypair generation.
- `VPN_REALITY_SHORT_ID`: one of inbound `shortIds` entries.

## Example .env

```bash
APP_ENV=production
DATABASE_PATH=data/app.db
APP_BASE_URL=https://vpn.your-domain.com
APP_TRUSTED_HOSTS=vpn.your-domain.com,localhost,127.0.0.1

VPN_SERVER=vpn.your-domain.com
VPN_PORT=443
VPN_SNI=www.cloudflare.com
VPN_REALITY_PUBLIC_KEY=PASTE_REAL_PUBLIC_KEY_HERE
VPN_REALITY_SHORT_ID=abcdef1234567890
VPN_TRANSPORT=tcp
VPN_PROFILE_NAME_PREFIX=KovalenkoVPN
```

## VPS deployment (fresh server)

1. Install Docker and Compose plugin:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

2. Upload project and enter directory:

```bash
cd /opt
git clone <YOUR_REPO_URL> shashkoffvpn
cd shashkoffvpn
```

3. Create production env:

```bash
cp .env.example .env
nano .env
```

4. Run app container:

```bash
cd docker
docker compose up --build -d
docker compose logs -f app
```

5. Keep backend private (recommended):
- `docker/docker-compose.yml` binds to `127.0.0.1:8000`, so only local reverse proxy can reach it.
- Do not open port `8000` in the external firewall/security group.

## Reverse proxy example (nginx)

```nginx
server {
    listen 80;
    server_name vpn.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Reverse proxy example (Caddy)

```caddy
vpn.your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Example profile request

```bash
curl -s "http://127.0.0.1:8000/api/profile/demo-token?device_id=my-device-1" | python3 -m json.tool
```

## Example open-link helper page request

```bash
curl -i "http://127.0.0.1:8000/open/demo-token?device_id=my-device-1"
```

## Example open-link download request

```bash
curl -i "http://127.0.0.1:8000/open/demo-token?device_id=my-device-1&mode=download"
```

## Example open-link raw JSON request

```bash
curl -s "http://127.0.0.1:8000/open/demo-token?device_id=my-device-1&mode=raw" | python3 -m json.tool
```

## Check profile warnings

```bash
curl -s "http://127.0.0.1:8000/api/profile/demo-token?device_id=my-device-1" | python3 -m json.tool | rg "\"warnings\"|\"profile_name\"|\"server\""
```

## Example protected placeholder request

```bash
curl -s "http://127.0.0.1:8000/open/demo-token?protect=1" | python3 -m json.tool
```

## Example device registration request

```bash
curl -s -X POST "http://127.0.0.1:8000/api/device/register" \
  -H "Content-Type: application/json" \
  -d '{
    "token":"demo-token",
    "device_id":"my-device-1",
    "device_name":"MacBook Pro",
    "platform":"macOS"
  }'
```

## Post-deploy verification

```bash
curl -i https://vpn.your-domain.com/health
curl -i https://vpn.your-domain.com/u/demo-token
curl -s "https://vpn.your-domain.com/api/profile/demo-token?device_id=test-device" | python3 -m json.tool
```
