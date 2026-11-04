# Встановлення на DigitalOcean

Від порожнього акаунта до робочого `https://ваш-домен` приблизно за 20–30 хвилин.
Команди нижче копіюйте по одній. Позначки на кшталт `ВАШ_IP` замінюйте своїми
значеннями.

**Що знадобиться**
- акаунт DigitalOcean (картка або PayPal);
- домен або піддомен, у якого можна змінити DNS. Якщо домену немає, див. крок 3,
  варіант Б;
- 10 хвилин доступу до кабінету ЄДЕССБ у браузері (для cookie);
- за бажання — бот у Telegram для сповіщень.

---

## 1. SSH-ключ (на вашому комп'ютері)

Пропустіть цей крок, якщо ключ уже є (`~/.ssh/id_ed25519.pub`).
Windows (PowerShell), macOS і Linux однаково:

```bash
ssh-keygen -t ed25519 -C "kadastr"
```

Натискайте Enter на всі питання. Потім виведіть публічний ключ, щоб його скопіювати:

```bash
cat ~/.ssh/id_ed25519.pub
```

У Windows PowerShell: `type $env:USERPROFILE\.ssh\id_ed25519.pub`.

## 2. Створити Droplet

DigitalOcean → **Create → Droplets**:

| Поле | Значення |
|---|---|
| Region | **Frankfurt (FRA1)** — найближчий до України |
| Image | **Ubuntu 24.04 (LTS) x64** |
| Size | Basic → Regular → **$6/міс (1 GB RAM, 25 GB SSD)**. Для невеликої команди цього досить |
| Authentication | **SSH Key** → *New SSH Key* → вставте вміст `id_ed25519.pub` |
| Hostname | `kadastr` |
| Backups | за бажання (+20% ціни): щотижневі знімки всього сервера |

Натисніть **Create Droplet** і скопіюйте його **IPv4-адресу** (далі `ВАШ_IP`).

## 3. Домен

**Варіант А — свій домен (рекомендовано).** У DNS-панелі реєстратора (або
DigitalOcean → Networking → Domains) створіть запис:

| Тип | Ім'я | Значення |
|---|---|---|
| A | `kadastr` (або `@`) | `ВАШ_IP` |

Застосунок буде на `kadastr.вашдомен.ua`. Записи DNS оновлюються від 5 хвилин до години.

**Варіант Б — без домену.** Використайте безкоштовний `ВАШ-IP-ЧЕРЕЗ-ДЕФІСИ.sslip.io`
(наприклад, `164-90-12-34.sslip.io`). Він сам вказує на ваш IP, і HTTPS теж працює.
Для внутрішнього інструменту це прийнятно, але свій домен надійніший.

## 4. Перший вхід і перевірка доступу до джерел

```bash
ssh root@ВАШ_IP
```

Одразу перевірте, що сервер бачить ЄДЕССБ і сервер тайлів (з Франкфурта мають
відповідати; з іншого регіону можливе геоблокування):

```bash
curl -s -o /dev/null -w "ЄДЕССБ: %{http_code}\n" https://admin.e-construction.gov.ua/
```

```bash
curl -s -o /dev/null -w "Тайли: %{http_code}\n" -H "Referer: https://admin.e-construction.gov.ua/" -H "Origin: https://admin.e-construction.gov.ua" https://data.gki.com.ua/api-user/rtile/parcels/ua/16/38296/22116.png
```

Очікується `200` або `30x`/`403` для ЄДЕССБ (сайт відповів — це головне) і `200` для
тайлів. Якщо `000`, таймаут або стабільно `503`, див. розділ
[«Український проксі»](#український-проксі-лише-якщо-джерела-блокують-сервер).

## 5. Базове налаштування сервера

Оновлення системи (якщо спитає про конфіги, лишайте поточну версію, Enter):

```bash
apt update && apt -y upgrade
```

Файрвол: відкриті лише SSH, HTTP і HTTPS:

```bash
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443 && ufw --force enable
```

Swap на 1 ГБ (на дроплеті з 1 ГБ RAM без нього може не вистачити пам'яті під час
збирання образу):

```bash
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Часовий пояс (для логів і журналу):

```bash
timedatectl set-timezone Europe/Kyiv
```

Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

## 6. Завантажити застосунок

```bash
git clone https://github.com/dexus288/kad-new444 /opt/kadastr && cd /opt/kadastr
```

> Якщо репозиторій приватний, git попросить логін і пароль. Як пароль вставте
> GitHub-токен: GitHub → Settings → Developer settings → Personal access tokens →
> *Fine-grained*, доступ лише до цього репозиторію, право *Contents: Read*.

## 7. Налаштування

```bash
cp .env.example .env && cp config/upstream.example.toml config/upstream.toml
```

```bash
nano .env
```

Змініть:

```ini
DOMAIN=kadastr.вашдомен.ua
PUBLIC_URL=https://kadastr.вашдомен.ua
REGISTRATION=approval
```

`Ctrl+O`, Enter, `Ctrl+X` — зберегти й вийти. Telegram заповните пізніше (крок 11).

`config/upstream.toml` уже налаштований на ЄДЕССБ і `data.gki.com.ua`. Змінювати
його не обов'язково. Єдине, що варто зробити, — вписати User-Agent свого браузера
(F12 → Console → `navigator.userAgent`):

```bash
nano config/upstream.toml
```

Права на теку даних (усередині контейнера застосунок працює від користувача з UID 1000):

```bash
mkdir -p data && chown 1000:1000 data && chmod 700 data && chmod 600 .env
```

## 8. Запуск

```bash
docker compose up -d --build
```

Перше збирання триває 2–5 хвилин. Перевірка:

```bash
docker compose ps
```

Усі три сервіси (`app`, `keepalive`, `caddy`) мають бути `Up`. Caddy сам
отримає HTTPS-сертифікат за хвилину після того, як DNS вказуватиме на сервер.

## 9. Перший вхід: стати адміністратором

Відкрийте `https://kadastr.вашдомен.ua` → **Реєстрація**.
**Перший зареєстрований обліковий запис автоматично стає адміністратором**, тож
зробіть це одразу після запуску.

## 10. Підключити сесію ЄДЕССБ

1. На своєму комп'ютері увійдіть в `admin.e-construction.gov.ua`.
2. F12 → **Application** → **Cookies** → `https://admin.e-construction.gov.ua`.
3. Скопіюйте **Value** рядка **`session_edessb`**.
4. У застосунку: аватар угорі праворуч → **Адміністрування** → вставте у «Нові
   cookies» → **Зберегти і перевірити**. Має з'явитися **«Сесія активна»**.
5. Вкладку ЄДЕССБ просто закрийте. **Не натискайте «Вийти»**: це завершить сесію і на сервері.

Далі keepalive сам тримає сесію: раз на ~4 хвилини, а ЄДЕССБ вбиває сесію лише
після години простою. Перевірити роботу:

```bash
docker compose logs --tail 20 keepalive
```

Мають бути рядки `ЖИВА OK (1 URL)`.

## 11. Сповіщення в Telegram (рекомендовано)

1. [@BotFather](https://t.me/BotFather) → `/newbot` → скопіюйте **токен**.
2. Напишіть своєму боту будь-яке повідомлення.
3. Відкрийте `https://api.telegram.org/botТОКЕН/getUpdates` і знайдіть
   `"chat":{"id":…}` — це **chat id**. Для групи додайте бота в групу; id групи від'ємний.
4. Впишіть обидва значення в `.env`:

```bash
nano /opt/kadastr/.env
```

```ini
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Застосуйте:

```bash
cd /opt/kadastr && docker compose up -d
```

Тепер, коли сесія впаде, прийде повідомлення з посиланням на адмінку, а після
відновлення — «✅ Сесію ЄДЕССБ відновлено».

## 12. Запросити колег

Колеги реєструються самі. Вам у меню **Адміністрування** приходить заявка →
**Прийняти**. Порада для телефонів: у браузері → «Поділитися» → **«На початковий
екран»**, і застосунок відкриватиметься як звичайна програма.

---

## Обслуговування

**Оновити застосунок до нової версії:**

```bash
cd /opt/kadastr && git pull && docker compose up -d --build
```

**Логи:**

```bash
cd /opt/kadastr && docker compose logs --tail 50 app
```

**Перевірити сесію з консолі:**

```bash
cd /opt/kadastr && docker compose exec app python manage.py check
```

**Забули пароль адміна:**

```bash
cd /opt/kadastr && docker compose exec app python manage.py set-password ЛОГІН
```

**Щоденний бекап бази** (користувачі, закладки):

```bash
apt install -y sqlite3 && (crontab -l 2>/dev/null; echo "15 3 * * * /opt/kadastr/deploy/backup.sh") | crontab -
```

Бекапи лежать у `/opt/kadastr/backups`, зберігаються 14 днів. Додатково можна
увімкнути Backups самого дроплета в DigitalOcean.

**Перезапуск усього:**

```bash
cd /opt/kadastr && docker compose restart
```

---

## Якщо щось не працює

| Симптом | Що перевірити |
|---|---|
| Сайт не відкривається / помилка сертифіката | `dig +short kadastr.вашдомен.ua` має повертати `ВАШ_IP`; `docker compose logs caddy` |
| «Сесія неактивна» одразу після вставки | Скопіювали значення не тієї cookie або вже натиснули «Вийти» в ЄДЕССБ. Увійдіть заново і вставте свіже значення |
| Сесія падає через рівно годину | keepalive не працює: `docker compose ps`, `docker compose logs keepalive` |
| Немає меж ділянок на карті | Межі видно з масштабу 13 і ближче. Далі: `docker compose logs app \| grep Тайл`. Якщо там `HTTP 503`/`403`, див. розділ нижче |
| `permission denied` на `data/` | `chown 1000:1000 /opt/kadastr/data` |
| Збирання падає з `Killed` | Немає swap (крок 5) |

---

## Український проксі (лише якщо джерела блокують сервер)

Якщо на кроці 4 ЄДЕССБ або тайли не відповідають з дроплета, потрібен
вихід в інтернет через український IP. Найпростіше — найдешевший VPS в Україні
з SSH, тунель до нього з дроплета:

```bash
apt install -y autossh && ssh-keygen -t ed25519 -N "" -f /root/.ssh/ua_proxy
```

Додайте `/root/.ssh/ua_proxy.pub` у `~/.ssh/authorized_keys` на українському
сервері. Потім створіть службу тунелю:

```bash
cat > /etc/systemd/system/ua-proxy.service <<'EOF'
[Unit]
Description=SOCKS proxy via Ukrainian VPS
After=network-online.target
[Service]
ExecStart=/usr/bin/autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i /root/.ssh/ua_proxy -D 0.0.0.0:1080 user@UA_SERVER_IP
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
```

```bash
systemctl daemon-reload && systemctl enable --now ua-proxy
```

Порт 1080 ззовні закритий файрволом (крок 5). Дозвольте до нього доступ лише
контейнерам Docker:

```bash
ufw allow from 172.16.0.0/12 to any port 1080 proto tcp
```

У `config/upstream.toml`, в секції `[general]`, додайте:

```toml
proxy = "socks5://host.docker.internal:1080"
```

Зміна підхоплюється без перезапуску. Перевірте командою
`docker compose exec app python manage.py check`.

Cookie в такому разі краще брати з браузера, що ходить через той самий
український IP (`ssh -D 1080 user@UA_SERVER_IP` на своєму комп'ютері + Chrome з
`--proxy-server="socks5://127.0.0.1:1080"`).
