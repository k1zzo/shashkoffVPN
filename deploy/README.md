# Deployment

Конфиги и инструкции для развёртывания инфраструктуры shashkoffVPN.

## Архитектура трафика

```
Интернет
  ↓
:443  HAProxy (TCP + SNI/ALPN inspect)
       ├─ ALPN = acme-tls/1               → Caddy :9443 (ACME challenge)
       ├─ SNI = shkoff-cloud.com          → Caddy :9443 → app :8000
       └─ default (SNI=apple от клиентов) → Xray Reality :8444
```

**Порт 80 не используется и не открывается.** Сертификат Let's Encrypt
выпускается и продлевается через TLS-ALPN-01 challenge — ACME-сервер
подключается на :443 с ALPN=`acme-tls/1`, HAProxy роутит коннект в Caddy,
Caddy отвечает challenge-сертификатом. Всё внутри одного :443.

Открытые наружу порты: только `:443` (HAProxy).

## Состав deploy/

| Файл                                       | Куда копировать                              |
|--------------------------------------------|----------------------------------------------|
| `haproxy.cfg`                              | `/etc/haproxy/haproxy.cfg`                   |
| `Caddyfile`                                | `/etc/caddy/Caddyfile`                       |
| `config.base.json.example`                 | `/usr/local/etc/xray/config.base.json` (с подстановкой ключей) |
| `shashkoffvpn-reload-xray.sh`              | `/usr/local/bin/shashkoffvpn-reload-xray`    |
| `shashkoffvpn-merge-xray-clients.py`       | `/usr/local/lib/`                            |
| `shashkoffvpn-xray-watcher.{service,path}` | `/etc/systemd/system/`                       |

Не коммитить: `.env`, `data/`, реальный `config.base.json` с приватным
ключом Reality.

## Установка с нуля

Подразумевается чистый Ubuntu 22.04+, права sudo, git, Docker.

### 1. Системные пакеты

```bash
sudo apt update
sudo apt install -y haproxy caddy ufw curl ca-certificates git \
                    docker.io docker-compose-plugin

sudo bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```

### 2. Файрвол

```bash
sudo ufw allow OpenSSH
sudo ufw allow 443
sudo ufw --force enable
```

Порт 80 закрыт. Порты 8000, 8444, 9443, 10085 — только localhost,
наружу не открываются.

Если включаешь Xray traffic stats (`XRAY_API_ADDR` в `.env`), нужно ещё:

```bash
# Адрес Docker bridge — посмотри `ip a | grep docker0`
sudo ufw allow from 172.18.0.0/16 to any port 10085 proto tcp
```

### 3. Клонировать репо

```bash
sudo mkdir -p /opt
sudo git clone https://github.com/k1zzo/shashkoffVPN.git /opt/shashkoffVPN
cd /opt/shashkoffVPN
```

### 4. Reality-ключи

```bash
xray x25519
# PrivateKey → пойдёт в config.base.json
# Password   → это PUBLIC key, пойдёт в .env как VPN_REALITY_PUBLIC_KEY

openssl rand -hex 8
# Это short ID — в оба места: config.base.json и .env
```

Сохрани все три значения временно куда-нибудь — используем в шагах 5 и 7.

### 5. Xray

```bash
sudo mkdir -p /usr/local/etc/xray
sudo cp deploy/config.base.json.example /usr/local/etc/xray/config.base.json
sudo nano /usr/local/etc/xray/config.base.json
# Заменить REPLACE_WITH_YOUR_PRIVATE_KEY и REPLACE_WITH_YOUR_SHORT_ID
```

Watcher и merge-скрипт:
```bash
sudo cp deploy/shashkoffvpn-merge-xray-clients.py /usr/local/lib/
sudo cp deploy/shashkoffvpn-reload-xray.sh /usr/local/bin/shashkoffvpn-reload-xray
sudo chmod +x /usr/local/lib/shashkoffvpn-merge-xray-clients.py \
              /usr/local/bin/shashkoffvpn-reload-xray
sudo cp deploy/shashkoffvpn-xray-watcher.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shashkoffvpn-xray-watcher.path
```

Стартуем Xray (при первом запуске нет clients fragment — копируем base
как config напрямую):
```bash
sudo cp /usr/local/etc/xray/config.base.json /usr/local/etc/xray/config.json
sudo systemctl enable --now xray
sudo ss -tlnp | grep xray   # должен слушать 127.0.0.1:8444
```

### 6. Caddy

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # email на свой
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy
sudo ss -tlnp | grep caddy            # должен слушать 127.0.0.1:9443
```

### 7. HAProxy

```bash
sudo cp deploy/haproxy.cfg /etc/haproxy/haproxy.cfg
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo systemctl enable --now haproxy
sudo ss -tlnp | grep haproxy          # должен слушать *:443
```

### 8. Приложение

```bash
cd /opt/shashkoffVPN
sudo cp .env.example .env
sudo nano .env
# Минимум:
#   APP_BASE_URL=https://your-domain.com
#   VPN_SERVER=your-domain.com
#   VPN_SNI=www.apple.com
#   VPN_REALITY_PUBLIC_KEY=<Password из xray x25519>
#   VPN_REALITY_SHORT_ID=<тот же short ID, что в config.base.json>

cd docker
sudo docker compose up -d --build
sudo docker compose logs --tail=30 app
```

### 9. Создать первого пользователя

```bash
cd /opt/shashkoffVPN
sudo docker exec -it shashkoffvpn-app python -m backend.cli create-user \
    --username test --token testtoken --device-limit 5
```

Это создаст пользователя в БД, app запишет `data/xray-clients.json`,
watcher это засечёт через inotify, запустит reload-xray, и Xray
перезапустится с обновлённым списком клиентов.

### 10. Проверки

```bash
# Все listeners
sudo ss -tlnp | grep -E ':(443|8000|8444|9443|10085)\b' | sort

# Сайт и сертификат
curl -i https://your-domain.com/health
echo | openssl s_client -servername your-domain.com \
    -connect your-domain.com:443 2>/dev/null \
    | openssl x509 -noout -subject -issuer -dates

# Reality-маскировка отвечает (apple.com сертификат)
echo | openssl s_client -servername www.apple.com \
    -connect your-domain.com:443 2>/dev/null \
    | openssl x509 -noout -subject -issuer | head -2
```

## Восстановление существующего сервера

Критичные файлы для бэкапа:

```bash
sudo tar czf shashkoff-backup-$(date +%F).tar.gz \
    /opt/shashkoffVPN/.env \
    /opt/shashkoffVPN/data \
    /usr/local/etc/xray/config.base.json
```

Восстанавливая, положи их обратно ПЕРЕД стартом сервисов. Иначе:
- Без `.env` — приложение не стартанёт.
- Без БД — исчезнут все пользователи.
- Без `config.base.json` с правильными ключами — все существующие
  VLESS-ссылки клиентов перестанут работать (нужна полная перевыдача).

## Логи

```bash
sudo journalctl -u haproxy -f
sudo journalctl -u caddy -f
sudo journalctl -u xray -f
sudo journalctl -u shashkoffvpn-xray-watcher.service -f   # события watcher'а
sudo docker logs -f shashkoffvpn-app
```

Проверка выпуска сертификата:
```bash
sudo journalctl -u caddy -n 50 --no-pager | grep -iE 'certificate|acme|tls-alpn'
```

## Обновление конфигов из репо

```bash
cd /opt/shashkoffVPN && sudo git pull

sudo cp deploy/haproxy.cfg /etc/haproxy/haproxy.cfg
sudo haproxy -c -f /etc/haproxy/haproxy.cfg && sudo systemctl reload haproxy

sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy

# Watcher-скрипты — если что-то менялось:
sudo cp deploy/shashkoffvpn-reload-xray.sh /usr/local/bin/shashkoffvpn-reload-xray
sudo cp deploy/shashkoffvpn-merge-xray-clients.py /usr/local/lib/
sudo cp deploy/shashkoffvpn-xray-watcher.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart shashkoffvpn-xray-watcher.path
```

`reload` не рвёт активные коннекты, в отличие от `restart`.

## HandlerService live client management

Начиная с этой версии, добавление/удаление устройств применяется к
живому процессу Xray через gRPC HandlerService, без `systemctl restart`.
Активные коннекты других пользователей не прерываются.

### Что обязательно поменять в Xray-конфиге

1. В блоке `api` подключить `HandlerService` рядом со `StatsService`:
   ```json
   "api": { "tag": "api", "services": ["HandlerService", "StatsService"] }
   ```
2. У VLESS Reality inbound должен быть стабильный `tag`. По умолчанию
   приложение использует `vless-reality-in` (см. `XRAY_VLESS_INBOUND_TAG`).
   В `deploy/config.base.json.example` это уже выставлено.

После правки `config.base.json` один раз перезапусти Xray (только в этот
переход; все последующие изменения клиентов уже не требуют рестарта):
```bash
sudo systemctl restart xray
```

### Что обязательно поменять в `.env`

```
XRAY_API_ADDR=172.18.0.1:10085         # тот же, что и для StatsService
XRAY_VLESS_INBOUND_TAG=vless-reality-in
XRAY_USE_HANDLER_API=true              # default
XRAY_RECONCILER_INTERVAL_SECONDS=60    # default
XRAY_HANDLER_API_TIMEOUT_SECONDS=5     # default
```

### Архитектура (defense in depth)

1. **Диск:** `apply_xray_client_changes` всегда пишет `data/xray-clients.json`
   — это снимок для холодного старта Xray и для legacy watcher-пути.
2. **Runtime (HandlerService):** на каждое изменение устройства приложение
   делает `list_users` → diff → `add_user`/`remove_user`. Делает это
   ДО `db.commit()`: если gRPC упал с `XrayApiError`, транзакция
   откатывается и роут возвращает 500. На `XrayApiUnavailable` (Xray
   ненадолго недоступен) — коммит проходит, файл-снимок свежий,
   синхронизация дойдёт через reconciler.
3. **Reconciler:** фоновая задача в lifespan FastAPI, тикает каждые
   `XRAY_RECONCILER_INTERVAL_SECONDS`. Сравнивает желаемое состояние
   (БД, с фильтром по `expires_at`) с тем, что реально в Xray, и
   доводит разницу. Лечит дрейф после простоев, ручных правок и
   истечения подписок.

### Legacy reload остаётся в коде

`shashkoffvpn-reload-xray.sh` НЕ удалён — он нужен для редких правок
`config.base.json` (новые routing-правила, смена ключей). Watcher тоже
жив и работает как раньше: запускает merge+systemctl reload при
изменении `xray-clients.json`. Для штатных операций с устройствами он
теперь избыточен — HandlerService применяет всё мгновенно. Если хочется
полностью отключить watcher для операций с клиентами, оставь
`XRAY_USE_HANDLER_API=true` (default) — приложение само не дёргает
reload, watcher всё равно отреагирует на `xray-clients.json`, но это
безопасно: при `reload` (а не `restart`) активные коннекты не рвутся.

### Откат

Если что-то пошло не так:
```
# в .env
XRAY_USE_HANDLER_API=false
```
Перезапусти контейнер:
```bash
cd /opt/shashkoffVPN/docker && sudo docker compose restart app
```
Поведение возвращается к legacy: `apply_xray_client_changes` пишет
файл-снимок, watcher merge+`systemctl restart xray`. Активные коннекты
снова рвутся при каждом изменении устройства — но всё работает как до
миграции.

### Побочный эффект: истёкшие пользователи

Раньше `User.expires_at` не учитывался в `build_active_xray_clients`,
поэтому пользователи с просроченной подпиской продолжали ходить через
VPN до ручного переподключения. Теперь:

- `build_active_xray_clients` сразу исключает таких пользователей.
- Reconciler в течение одного интервала (по умолчанию 60 секунд)
  выпишет их UUID из живого Xray.
- Активная VPN-сессия истёкшего пользователя обрывается — это
  правильное поведение срока действия. Операторов предупредить!

Восстановить доступ: `python -m backend.cli extend-user --token ...`
— одна запись в БД, новый reconciler-тик вернёт UUID в Xray.

### CLI диагностика

```bash
# Сравнить DB и Xray, увидеть дрейф
docker exec -it shashkoffvpn-app python -m backend.cli xray-state

# Применить дрейф один раз (то же, что один тик reconciler)
docker exec -it shashkoffvpn-app python -m backend.cli xray-reconcile
```

### Генерация proto-стабов (опционально)

`backend/xray_handler_api.py` сам кодирует/декодирует нужные protobuf-
сообщения вручную (как и `xray_stats.py`) — никакой кодогенерации в
рантайме не требуется. Скрипт `deploy/generate_xray_protos.sh` есть
для тех, кому захочется добавить новые RPC из HandlerService:

```bash
pip install "grpcio-tools>=1.50,<2.0"
bash deploy/generate_xray_protos.sh
# Сгенерированные модули появятся в backend/xray_proto/.
```
