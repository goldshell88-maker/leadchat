# RUNBOOK — выкатка LeadChat в прод

> Пошаговая инструкция для человека. Проектные решения — в
> [docs/05-DEPLOY-OPS.md](../../docs/05-DEPLOY-OPS.md); здесь только «что нажимать».
> Каждый шаг можно выполнять отдельно и повторно: всё идемпотентно.
>
> Домен: **chat.partner-lead-centre.ru** · Поддержка: Telegram, контакт — в `frontend/public/.well-known/security.txt`
>
> Время на всё с нуля — около 90 минут, из них ~20 ждём DNS.

## Содержание

| # | Шаг | Когда |
|---|---|---|
| 1 | [Что должно быть готово заранее](#1-что-должно-быть-готово-заранее) | до старта |
| 2 | [Требования к VPS](#2-требования-к-vps) | до старта |
| 3 | [DNS](#3-dns) | первым делом (ждёт распространения) |
| 4 | [Подготовка сервера](#4-подготовка-сервера) | один раз |
| 5 | [Сертификат TLS](#5-первичный-выпуск-сертификата) | один раз |
| 6 | [Файл .env](#6-файл-env) | один раз + при смене секретов |
| 7 | [GitHub Secrets и первый деплой](#7-github-secrets-и-первый-деплой) | один раз |
| 8 | [Первый админ и вход](#8-первый-админ-и-вход) | один раз |
| 9 | [Переключение с fake-avito на боевой Авито](#9-переключение-с-fake-avito-на-боевой-авито) | перед боем |
| 10 | [Бэкапы и cron](#10-бэкапы-и-cron) | один раз |
| 11 | [Мониторинг](#11-мониторинг) | один раз |
| 12 | [Smoke и чек-лист приёмки](#12-чек-лист-приёмки) | после каждого деплоя |
| 13 | [Обычный релиз](#13-обычный-релиз) | всегда |
| 14 | [Откат](#14-откат) | когда плохо |
| 15 | [Инциденты](#15-инциденты) | когда очень плохо |

---

## 1. Что должно быть готово заранее

- [ ] VPS в РФ, куплен, есть root-доступ по ssh.
- [ ] Домен `partner-lead-centre.ru` — доступ в панель DNS.
- [ ] GitHub-репозиторий с этим кодом, права администратора (заводить секреты).
- [ ] Аккаунт в кабинете разработчика Авито (`developers.avito.ru`) — `client_id`/`client_secret`.
- [ ] S3-совместимое хранилище для офсайт-бэкапов (**не** у того же хостера, что VPS).
- [ ] Менеджер секретов компании — туда положим копию `.env`.

---

## 2. Требования к VPS

| Параметр | Значение | Почему так |
|---|---|---|
| CPU | 4 vCPU | api(2 воркера) + worker + scheduler + postgres + nginx |
| RAM | 8 GB | сумма лимитов контейнеров ~5.2 GB + запас ОС и pagecache |
| Диск | 160 GB NVMe | БД + вложения на 2–3 года роста + локальные дампы |
| ОС | Ubuntu 24.04 LTS | под неё написаны все команды ниже |
| Docker | Engine 27+, compose v2.27+ | `deploy.resources.limits` без Swarm, `--env-file` |
| Порты наружу | 22, 80, 443 | всё остальное — только внутренняя docker-сеть |

Проверить после логина под root:

```bash
lsb_release -a          # Ubuntu 24.04
nproc && free -g && df -h /
docker --version && docker compose version
```

---

## 3. DNS

A-запись на IP сервера, TTL 300 на время запуска (потом можно поднять):

```
chat.partner-lead-centre.ru.   300   IN   A   <IP_VPS>
```

Ждём и проверяем **с любой машины** (не с VPS — там может быть свой резолвер):

```bash
dig +short chat.partner-lead-centre.ru
```

Пока запись не разошлась, сертификат не выпустится — шаг 5 просто не пройдёт.

---

## 4. Подготовка сервера

Всё под `root`, один раз.

```bash
# --- 4.1. Пакеты и время ---------------------------------------------------
apt update && apt upgrade -y
apt install -y ca-certificates curl gnupg jq ufw fail2ban rclone
timedatectl set-timezone UTC          # всё в UTC (05 §4)

# --- 4.2. Docker Engine (официальный репозиторий) --------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# --- 4.3. Пользователь деплоя ----------------------------------------------
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
mkdir -p /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
# положить сюда ПУБЛИЧНЫЙ ключ, приватная половина которого уедет в GitHub Secrets
nano /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys

# --- 4.4. Каталоги (раскладка 05 §1) ---------------------------------------
mkdir -p /opt/leadchat/bin /var/leadchat/{media,download,certbot,whisper} /var/backups/leadchat
chown -R deploy:deploy /opt/leadchat /var/leadchat /var/backups/leadchat
touch /var/log/leadchat-deploy.log /var/log/leadchat-backup.log
chown deploy:deploy /var/log/leadchat-*.log

# --- 4.5. Firewall: наружу только 22/80/443 --------------------------------
ufw default deny incoming && ufw default allow outgoing
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp
ufw --force enable && ufw status verbose

# --- 4.6. fail2ban для sshd ------------------------------------------------
systemctl enable --now fail2ban
fail2ban-client status sshd
```

> **Важно про ufw и Docker.** Опубликованный контейнером порт открывается в
> обход ufw (Docker пишет свои правила в iptables). В нашем compose наружу
> публикует порты только nginx (80/443) — postgres и redis сидят во внутренней
> сети без `ports:`. Если кто-то добавит `ports:` к базе, ufw это **не**
> прикроет. Проверка снаружи после запуска: `nmap -p- <IP>` — должны быть
> видны только 22/80/443 (07 §4.5 N5).

**4.7. Права на каталог вложений.** Контейнеры `api`/`worker` работают под
непривилегированным пользователем `leadchat` из образа, а bind-mount приносит
владельца с хоста. Узнаём uid/gid из самого образа и выставляем их (после
первого `docker login`, шаг 7.2 — либо позже, но обязательно до первой
загрузки вложения):

```bash
IMG=ghcr.io/lead-partner/leadchat-api:latest
UIDGID=$(docker run --rm --entrypoint sh "$IMG" -c 'id -u leadchat; id -g leadchat' | paste -sd: -)
echo "uid:gid контейнера = $UIDGID"
chown -R "$UIDGID" /var/leadchat/media
# Тот же владелец нужен каталогу весов Whisper: туда воркер качает модель при
# первой расшифровке голосового (464 МБ, в образе их нет намеренно).
chown -R "$UIDGID" /var/leadchat/whisper
```

**4.8. ssh-ключ для CI.** Пару генерируем на своей машине, не на сервере:

```bash
ssh-keygen -t ed25519 -C "github-actions-leadchat" -f ~/.ssh/leadchat_deploy
# публичную часть -> /home/deploy/.ssh/authorized_keys (шаг 4.3)
# приватную часть -> GitHub Secret VPS_SSH_KEY (шаг 7.1)
```

Проверка: `ssh -i ~/.ssh/leadchat_deploy deploy@<IP> docker ps` — должно работать
без пароля.

---

## 5. Первичный выпуск сертификата

Сертификат нужен **до** первого старта nginx: с TLS-конфигом и без файлов
сертификата nginx не поднимется. Выпускаем standalone-режимом (порт 80 в этот
момент свободен — сервисы ещё не запущены), дальше его продлевает контейнер
`certbot` через webroot, а nginx перечитывает файлы сам (reload раз в 6 часов).

```bash
docker run --rm -p 80:80 \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/leadchat/certbot:/var/www/certbot \
  certbot/certbot certonly --standalone \
  -d chat.partner-lead-centre.ru \
  --email admin@partner-lead-centre.ru --agree-tos --no-eff-email

ls -l /etc/letsencrypt/live/chat.partner-lead-centre.ru/
# нужны fullchain.pem и privkey.pem
```

Если Let's Encrypt отвечает «unauthorized» — DNS ещё не разошёлся (шаг 3) или
80-й порт закрыт (шаг 4.5).

---

## 6. Файл .env

```bash
# со своей машины, из корня репозитория
scp .env.prod.example deploy@<IP>:/opt/leadchat/.env
ssh deploy@<IP> 'chmod 600 /opt/leadchat/.env'
```

> **Если `.env.prod.example` не нашёлся в свежем клоне** — значит в `.gitignore`
> ещё не поправлена строка `.env.*`: она глушит и шаблон тоже. Проверка —
> `git check-ignore -v .env.prod.example`; лечится одной строкой после
> `!.env.example`:
> ```
> !.env.prod.example
> ```
> Без неё файл физически есть только у того, кто его создал, и в репозиторий
> не попадает.

Дальше на сервере под `deploy` заполняем. Секреты генерируем прямо там и
вставляем — руками ничего не придумываем:

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
echo "TOKEN_ENC_KEY=$(openssl rand -base64 32)"
echo "JWT_SECRET=$(openssl rand -base64 64 | tr -d '\n')"
echo "MEDIA_SIGN_KEY=$(openssl rand -hex 16)"

nano /opt/leadchat/.env
```

Что обязательно проверить перед сохранением:

- [ ] `GHCR_NAMESPACE` совпадает с владельцем GitHub-репозитория (`ghcr.io/<org>`);
- [ ] пароль в `DATABASE_URL` **тот же**, что в `POSTGRES_PASSWORD` (две строки, легко разъехаться);
- [ ] `FRONTEND_BASE_URL=https://chat.partner-lead-centre.ru` — на него бэкенд
      редиректит после OAuth-callback'а Авито. Пустое значение деривируется в
      `https://{DOMAIN}` и прод не сломает, но дефолт в коде —
      `http://localhost:5173`, поэтому строку задаём явно;
- [ ] `MEDIA_SIGN_KEY` заполнен — без него контейнер `nginx` **не стартует**
      (в compose стоит `${MEDIA_SIGN_KEY:?…}`), и им же подписываются ссылки
      на вложения: значение обязано совпадать у `api` и у `nginx`, то есть
      быть ровно одной строкой в этом файле;
- [ ] `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` из кабинета разработчика;
- [ ] `INTERNAL_SERVICE_TOKEN` — без него скрипты не докладывают в центр уведомлений;
- [ ] `RCLONE_REMOTE` — имя remote из шага 10;
- [ ] `GATEWAY_URL=http://10.10.0.2:8792` и `GATEWAY_TOKEN` — шлюз внешних API на
      Амстердаме (docs/46): без него молчат помощники адреса и ответы ботов;
      ключи DaData/Яндекса/OpenRouter/Groq/Mistral/Anthropic здесь НЕ лежат —
      они в `/opt/leadchat-gateway/.env` на Амстердаме (ставится `gateway/deploy.sh`);
- [ ] `grep -n CHANGE_ME /opt/leadchat/.env` не находит ничего.

**Копию заполненного `.env` — в менеджер секретов компании.** Без
`TOKEN_ENC_KEY` бэкап БД бесполезен: токены Авито не расшифруются (05 §6.4).

---

## 7. GitHub Secrets и первый деплой

### 7.1. Секреты репозитория (Settings → Secrets and variables → Actions)

| Секрет | Значение |
|---|---|
| `VPS_HOST` | IP или DNS-имя сервера |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | приватный ключ из шага 4.8 целиком, включая строки BEGIN/END |
| `SENTRY_DSN_FRONTEND` | DSN проекта `leadchat-frontend` (можно пустым) |
| `SMOKE_EMAIL`, `SMOKE_PASSWORD` | учётка `smoke@leadpartner.local`, роль `manager` (шаг 12.1) |
| `SMOKE_ADMIN_EMAIL`, `SMOKE_ADMIN_PASSWORD` | учётка `smoke-admin@leadpartner.local`, роль `head` — для SM-10 (шаг 12.1) |

Пуш образов в GHCR идёт под стандартным `GITHUB_TOKEN` — отдельный PAT не нужен.

### 7.2. Доступ VPS к GHCR

Если пакеты приватные (по умолчанию — да), сервер должен уметь их тянуть.
Личный токен с правом `read:packages`, на сервере под `deploy`:

```bash
echo '<GHCR_READ_TOKEN>' | docker login ghcr.io -u <github-login> --password-stdin
```

Проверка: `docker pull ghcr.io/lead-partner/leadchat-api:latest` (после первой
сборки). Альтернатива — сделать пакеты публичными в настройках организации.

### 7.3. Первый деплой

```bash
# со своей машины
git tag v0.1.0 && git push origin v0.1.0
```

Workflow `Deploy` сделает: сборку образов `leadchat-api` и `leadchat-web` →
push в GHCR → `scp` compose и скриптов в `/opt/leadchat` → `deploy.sh <sha>` →
smoke → итог в логе выкатки.

Следим за ходом: вкладка **Actions** в GitHub, параллельно на сервере

```bash
tail -f /var/log/leadchat-deploy.log
cd /opt/leadchat && docker compose ps
```

Ожидаемый финал: все контейнеры `healthy`, в логе `== OK: <sha> ==`,
`curl https://chat.partner-lead-centre.ru/api/health` отдаёт
`{"status":"ok","db":true,"redis":true,"version":"<sha>"}`.

> Если первый деплой упал на `pull` — скорее всего шаг 7.2 (доступ к GHCR)
> или `GHCR_NAMESPACE` в `.env`. Скрипт в этом случае возвращает `IMAGE_TAG`
> обратно и **ничего не меняет** на сервере.

---

## 8. Первый админ и вход

```bash
cd /opt/leadchat
docker compose exec api python -m app.cli create-admin \
  --email admin@partner-lead-centre.ru --full-name "Администратор"
```

Команда напечатает пароль (или одноразовую ссылку-инвайт). Заходим на
`https://chat.partner-lead-centre.ru`, логинимся, меняем пароль.

Восстановление пароля сотрудникам — **только через админа** (самостоятельного
сброса по почте в системе нет). Приглашение сотрудника:

```bash
docker compose exec api python -m app.cli invite \
  --email manager@partner-lead-centre.ru --role manager --full-name "Имя Фамилия"
```

---

## 9. Переключение с fake-avito на боевой Авито

**Ровно две переменные** в `/opt/leadchat/.env`:

```diff
- AVITO_API_BASE=http://fake-avito:8020
- AVITO_AUTH_URL=http://fake-avito:8020/oauth
+ AVITO_API_BASE=https://api.avito.ru
+ AVITO_AUTH_URL=https://avito.ru/oauth
```

(в `.env.prod.example` боевые значения стоят изначально — этот раздел нужен,
если стенд поднимали на моке).

Дополнительно проверить, что совпадают:

- `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` — из кабинета `developers.avito.ru`;
- `AVITO_REDIRECT_URI` — **буква в букву** такой же, как в кабинете:
  `https://chat.partner-lead-centre.ru/api/v1/avito/callback`;
- `PUBLIC_BASE_URL` — от него строится URL вебхука, который мы отдаём Авито.

Применить:

```bash
cd /opt/leadchat && docker compose up -d api worker scheduler
```

Дальше — в UI: `/settings/accounts` → «Подключить аккаунт» → OAuth-консент
Авито → аккаунт становится `active`, вебхук регистрируется автоматически.
Проверка живьём: написать в объявление с телефона — сообщение должно
появиться в интерфейсе меньше чем за 2 секунды.

---

## 10. Бэкапы и cron

### 10.1. rclone (офсайт)

Под пользователем `deploy`:

```bash
rclone config          # создать remote с именем offsite (S3-совместимый)
rclone lsd offsite:    # должен ответить без ошибок
chmod 600 ~/.config/rclone/rclone.conf
```

Имя remote должно совпадать с `RCLONE_REMOTE` в `.env`
(`offsite:leadchat-backups`). Хранилище — **не** у хостера VPS: смысл офсайта
в том, чтобы пережить потерю сервера целиком.

### 10.2. Шифрование офсайт-копий

Наружу — в бакет и на второй сервер — копии уходят только зашифрованными
([age](https://age-encryption.org), X25519 + ChaCha20-Poly1305). Открытый дамп
остаётся лишь в `BACKUP_DIR` на самом сервере (каталог `700`).

| Что | Где |
|---|---|
| публичный ключ (шифрует) | `deploy/backup-age-recipients.txt` — едет с кодом |
| секретный ключ (расшифровывает) | `~/.config/leadchat/backup-age-key.txt` на рабочем маке владельца, права `600`; на сервере его нет |
| зашифрованные копии | `s3:…/db/*.dump.age`, `…/db-monthly/`, `…/media-archive/*.tar.age`, второй сервер `/var/backups/leadchat-offsite/` |

**Секретный ключ — в менеджер секретов компании, рядом с `.env`.** Без него
офсайт-копии не открыть: ни бакет, ни второй сервер, ни сам прод его не хранят —
взлом любого из них не раскрывает архив переписки. Учению восстановления
(`restore-check.sh`) ключ передают на время: `BACKUP_AGE_IDENTITY=<путь>`.

```bash
sudo apt install -y age                       # на сервере, один раз
age-keygen -o ~/.config/leadchat/backup-age-key.txt   # только при заведении нового ключа
age-keygen -y ~/.config/leadchat/backup-age-key.txt   # публичная половина → в recipients
```

Восстановление вручную:

```bash
rclone copyto offsite:<бакет>/db/db_YYYYMMDD_HHMM.dump.age ./db.dump.age
age -d -i ~/.config/leadchat/backup-age-key.txt -o db.dump db.dump.age
age -d -i ~/.config/leadchat/backup-age-key.txt media_YYYYMMDD_HHMM.tar.age | tar -x -C /var/leadchat/media
```

Смена ключа: новый `age-keygen`, публичную половину — в
`backup-age-recipients.txt` (старую оставить строкой рядом, пока в хранилищах
живут копии под ней — 30 суток для суточных, год для месячных).

### 10.3. Первый прогон вручную

```bash
/opt/leadchat/bin/backup.sh
ls -la /var/backups/leadchat/          # появился db_YYYYMMDD_HHMM.dump
rclone ls offsite:leadchat-backups/db/ # на офсайте — db_YYYYMMDD_HHMM.dump.age
```

### 10.4. Проверка восстановления

```bash
/opt/leadchat/bin/backup-verify.sh     # поднимет одноразовый postgres, восстановит, посчитает
```

В `/var/log/leadchat-backup.log` должна появиться строка
`backup-verify OK: users=… convs=… msgs=…`.

### 10.5. Cron

```bash
sudo -u deploy crontab /opt/leadchat/bin/crontab.leadchat
sudo -u deploy crontab -l
```

Расписание: бэкап 03:30 UTC ежедневно, проверка восстановления 5-го числа в
06:00 UTC. Раз в квартал — учение руками: `/opt/leadchat/bin/restore-check.sh`
(печатает отчёт и чек-лист для тикета, целевой RTO — 2 часа).

---

## 11. Мониторинг

**Uptime-Kuma** ставится **не на этот VPS** (умрёт вместе с ним) — на любую
другую дешёвую машину. Мониторы (05 §7.2):

| Монитор | Тип | Интервал |
|---|---|---|
| `https://chat.partner-lead-centre.ru/` | HTTPS + cert expiry | 60 c, алерт за 14 дней до истечения серта |
| `GET /api/health` | HTTP keyword `"ok"` | 60 c |
| `GET /api/health/deep` | HTTP keyword `"ok"` | 5 мин |
| Webhook-канарейка | Push | grace 30 мин |
| `GET /download/latest.json` | HTTP 200 | 15 мин |

> **Ручки `/api/health/deep` в коде пока нет** — на момент спринта 3 приложение
> отдаёт только `/api/health`. Монитор из третьей строки заводить рано: он
> будет постоянно красным и обесценит алерты. По той же причине smoke-проверки
> SM-6 (очередь) и SM-7 (планировщик) уходят в `SKIP` — снаружи состояние
> очереди и scheduler'а больше ниоткуда не видно. Заводим монитор и включаем
> `--strict` в smoke сразу после появления ручки (контракт — 05 §7.2 плюс поле
> `scheduler.alive_age_sec`, которое `deploy/smoke.sh` уже читает).
>
> То же про `GET /download/latest.json`: каталог `/var/leadchat/download`
> наполняет CI десктопа (04 §6.1) — до первого релиза десктопа монитор
> заводить не нужно.

Push-URL канарейки кладём в `KUMA_WEBHOOK_CANARY_URL` и перезапускаем worker.

**Sentry**: `SENTRY_DSN` в `.env` (бэкенд), `SENTRY_DSN_FRONTEND` в GitHub
Secrets (инлайнится в бандл при сборке). Проверка — тестовое исключение,
событие должно появиться в проекте `leadchat-backend`.

Все уведомления — в центр внутри системы; туда же докладывают `backup.sh`,
`backup-verify.sh` и `healthcheck-alert.sh`. `deploy.sh` пишет только в лог
выкатки: тот, кто её запустил, по определению на месте.

### 11.1 Куда приходят тревоги

**Внешних каналов у системы нет — решение заказчика от 8 августа, разбор в
`docs/23-ALERTS-DECISION.md`.** Всё, что система замечает о себе, она
показывает в собственном центре уведомлений; скрипты на сервере сообщают туда
же служебной ручкой `POST /internal/notify` (нужен `INTERNAL_SERVICE_TOKEN`).

Цена решения названа в том же документе и повторяется здесь, потому что читать
её будут в момент разбора: **о ночной поломке узнают утром**, а смерть сервера
целиком не заметит никто — центр уведомлений лежит вместе с системой.

Проверить, что путь в центр жив:

```bash
cd /srv/leadchat && docker compose exec -T api python -c "
from app.core.config import settings
print('служебный токен задан:', bool(settings.internal_service_token))
"
```


---

## 12. Чек-лист приёмки

Автоматическая часть — `deploy/smoke.sh` (16 проверок SM-1…SM-16 из 07 §6,
наружу в Авито не уходит ничего):

```bash
# со своей машины, из корня репозитория
SMOKE_EMAIL=smoke@leadpartner.local SMOKE_PASSWORD=... make smoke

# или на сервере
SMOKE_EMAIL=... SMOKE_PASSWORD=... /opt/leadchat/bin/smoke.sh
```

Проверки, для которых ещё нет ручки на бэкенде, отмечаются `SKIP` и прогон не
роняют. Перед релизом гоняем с `--strict` (`STRICT=1 make smoke`) — тогда
`SKIP` считается провалом.

### 12.1. Служебные учётки проверок

Половина набора (SM-2…SM-5, SM-10) — это вход в систему, и без учёток она
пропускается молча. Заводит их **та же команда, что и остальные служебные
сущности**, руками в базу лезть не нужно:

```bash
cd /srv/leadchat && docker compose exec -T api python -m app.cli seed-smoke \
  --password '<пароль>' --admin-password '<пароль>'
```

Учёток две, и роли у них разные не по недосмотру:

| Учётка | Роль | Зачем |
|---|---|---|
| `smoke@leadpartner.local` | `manager` | SM-2…SM-5: вход, список диалогов, WS, заметка |
| `smoke-admin@leadpartner.local` | `head` | SM-10: список каналов Авито (`accounts:read`) |

Роль `head`, а не `admin`, потому что пароль лежит в `.env` сервера: `head`
читает каналы, но не пишет клиентам и не трогает людей. Обе помечены
`is_service` — в списке сотрудников, в статистике и в раздаче их нет.

Пароли кладутся в `/srv/leadchat/.env` (`SMOKE_EMAIL`, `SMOKE_PASSWORD`,
`SMOKE_ADMIN_EMAIL`, `SMOKE_ADMIN_PASSWORD`, `SMOKE_CONVERSATION_ID`). Оттуда
их читает `deploy/workstation/ship.sh`.

`SMOKE_CONVERSATION_ID` — это id строки в базе, а не константа: если базу
восстановили из копии, снятой до появления `SMOKE-CONV`, id будет другим.
`seed-smoke` печатает его при каждом прогоне — после восстановления сверьте.

> **Строка `SMOKE_EMAIL=` в `.env` — это переключатель гейта.** Пока её нет,
> выкатка гоняет регрессию мягко и печатает жёлтое «пропущено проверок: 5».
> Как только она появилась, выкатка переходит на `--strict`, и любой `SKIP`
> становится провалом с откатом образов. Поэтому вписывать учётки в `.env`
> нужно **после** того, как ручной прогон показал 11 pass / 0 skip, а не до.

Ручная часть — **Приложение Б из 05-DEPLOY-OPS.md**:

1. [ ] DNS: A-запись `chat.partner-lead-centre.ru` → IP VPS; TTL 300 на время запуска.
2. [ ] VPS подготовлен по разделу 4 (пользователь deploy, ufw, каталоги, bootstrap-сертификат).
3. [ ] `.env` заполнен, секреты сгенерированы, копия в менеджере секретов компании.
4. [ ] `rclone config` настроен, `rclone lsd offsite:` работает.
5. [ ] GitHub Secrets заведены, первый тег `v0.1.0` запушен, деплой прошёл, `/api/health` зелёный.
6. [ ] Создан первый админ, вход в UI работает.
7. [ ] Подключён тестовый аккаунт Авито, входящее сообщение доехало до UI < 2 сек.
8. [ ] Uptime-Kuma: заведённые мониторы зелёные (см. оговорки в разделе 11 —
       `/api/health/deep` и `/download/latest.json` заводим позже), тестовый
       уведомление дошло до центра.
9. [ ] Sentry: тестовое исключение видно в проекте.
10. [ ] `backup.sh` прогнан вручную, дамп появился офсайт; `backup-verify.sh` зелёный.
11. [ ] Прогнан runbook 05 §8.1 шаг 3 (resubscribe) на тестовом аккаунте — команда работает.
12. [ ] Cron заведён (шаг 10.4).

Плюс сетевой периметр (07 §4.5) — проверить один раз с внешней машины:

```bash
curl -sI http://chat.partner-lead-centre.ru | head -1        # 301 на https
curl -sI https://chat.partner-lead-centre.ru | grep -i strict-transport
curl -s -o /dev/null -w '%{http_code}\n' https://chat.partner-lead-centre.ru/api/openapi.json  # 404
nmap -p- <IP>                                                # только 22/80/443
```

---

## 13. Обычный релиз

```bash
git tag v0.3.0 && git push origin v0.3.0
```

Дальше всё делает CI. Что происходит на сервере (`deploy/deploy.sh`):

1. запоминает текущий тег как точку отката, прописывает новый в `.env`;
2. `docker compose pull`;
3. `alembic upgrade head` — **до** обновления кода (старые контейнеры ещё
   работают, поэтому миграции обязаны быть expand-contract);
4. поочерёдно поднимает `worker` → `scheduler` → `api` → `nginx`, каждый раз
   дожидаясь `healthy`; после api делает `nginx -s reload` (иначе nginx
   держал бы старый IP пересозданного контейнера);
5. smoke `/api/health` снаружи, с проверкой, что версия равна задеплоенному тегу;
6. провал любого шага → автоматический откат на предыдущий тег + алерт.

Простой при выкатке — 5–10 секунд на пересоздании api. Фронт ретраит запросы,
WebSocket переподключается сам, вебхуки Авито ретраятся, reconciliation
догоняет остаток. Отдельного окна обслуживания не требуется.

**Правила миграций (обязательны, иначе откат перестанет работать):**

1. одна миграция — только добавляющие изменения (новая таблица, nullable-колонка,
   индекс `CONCURRENTLY`); старый код обязан работать на новой схеме;
2. удаление/переименование колонки — минимум через релиз после того, как код
   перестал её использовать (expand в N, contract в N+1);
3. backfill данных — не в миграции, а ARQ-задачей после деплоя;
4. `CREATE INDEX CONCURRENTLY` — в миграции с `AUTOCOMMIT`;
5. месячные партиции `messages` создаёт scheduler, а не миграция.

---

## 14. Откат

Образы в GHCR иммутабельны (тег = git SHA), поэтому откат — это обычный
деплой старого тега.

```bash
# на сервере: на тот тег, что работал до последнего деплоя
/opt/leadchat/bin/rollback.sh

# или явно
/opt/leadchat/bin/rollback.sh a1b2c3d
```

Автоматически то же самое делает CI, если после деплоя красный smoke.

**Схему БД не откатываем никогда** — вниз-миграции в проде запрещены. Если
проблема в миграции, ломающей старый код, чиним накатом вперёд (fix-forward):
это ошибка процесса, а не повод гонять `alembic downgrade` на боевой базе.

Посмотреть, куда откатываться:

```bash
cat /opt/leadchat/.state/previous_tag
grep IMAGE_TAG /opt/leadchat/.env
```

### 14.1. Откат релиза со статусной моделью (миграция 0026)

Особый случай, потому что схему мы не откатываем, а данные при откате КОДА
остаются в состояниях, которых старый код не знает: `waiting_client` и
`snoozed`. Старый интерфейс покажет их латиницей (`TablePage` печатает код,
если подписи нет) и не даст сменить статус — регулярка фильтра и `Literal`
схемы их отвергнут.

Колонки `status_since`, `snoozed_until`, `snoozed_by_id`, `snooze_reason`
откату не мешают: старый код их просто не читает.

Поэтому сразу после отката образа — свести данные:

```sql
UPDATE conversations
   SET status = 'in_progress', snoozed_until = NULL,
       snoozed_by_id = NULL, snooze_reason = NULL
 WHERE status IN ('waiting_client', 'snoozed');
```

`WHERE` обязателен: без него это переписывание всей таблицы и потеря
закрытых.

Диалоги при этом попадают к своим же владельцам в «В работе» — то есть в
худшем случае оператор увидит у себя обращение, которое отложил до завтра.
Это заметно и разбирается руками; невидимый диалог со статусом, которого нет
в фильтрах, — нет.

### 14.2. Порядок выката этого релиза

Расширяющее изменение, четыре шага (docs/38, «Миграция и порядок выката»):

1. **До кода** — снять цифры с боевой базы:
   `SELECT status, count(*) FROM conversations GROUP BY status;`
   Значение вне пяти известных уронит `ADD CONSTRAINT`. Больше 200 000 строк —
   ставить `CHECK` через `NOT VALID` + `VALIDATE` двумя транзакциями, индекс —
   `CONCURRENTLY`.
2. **Миграция 0026.** Данных в новых статусах ещё нет.
3. **Фронт — РАНЬШЕ бэкенда.** Он умеет отображать пять значений, но сервер их
   ещё не отдаёт; это безопасно. Обратный порядок дал бы окно, в котором
   старый клиент получает незнакомый код и печатает его латиницей.
4. **Бэкенд.** Матрица переходов, автопереходы, статистика на пять чисел.
5. **Отложка** — после того как шаг 4 отработал сутки: пока не видно, что
   автопереходы ведут себя правильно, добавлять к ним ещё и возврат по времени
   значит разбирать два источника странностей сразу.

Проверка на боевом канале до объявления готовности: написать с телефона,
ответить, поставить «Ждёт клиента», написать с телефона снова — статус обязан
сам вернуться в «В работе», а в ленте появиться системная запись.

---

## 15. Инциденты

Первая команда при любом «всё сломалось»:

```bash
cd /opt/leadchat
docker compose ps                       # кто жив, кто рестартится
docker compose logs --tail=100 api worker scheduler
curl -s https://chat.partner-lead-centre.ru/api/health/deep | jq
docker compose exec redis redis-cli XLEN webhooks:avito
/opt/leadchat/bin/smoke.sh              # «что вообще живо?» за минуту
```

Дальше — по симптому, подробные разборы в
[05-DEPLOY-OPS.md §8](../../docs/05-DEPLOY-OPS.md#8-runbook):

| Симптом | Первая команда | Runbook |
|---|---|---|
| Сообщения не приходят | `tail access.log \| grep hooks` | §8.1 |
| Аккаунт красный в настройках | `SELECT status FROM avito_accounts` | §8.2 |
| Алерт queue.len | `redis-cli XLEN webhooks:avito` | §8.3 |
| Алерт disk | `df -h; du -sh /var/leadchat/media` | §8.4 |
| Всё лежит | `docker compose ps` → `logs` | по месту |
| Непонятно | `curl /api/health/deep \| jq` | по красному полю |

Логи nginx (в контейнере они уходят в stdout/stderr, то есть в docker logs):

```bash
docker compose logs --tail=200 nginx | jq -r 'select(.uri|test("/api/hooks/"))'
```

**429 от nginx.** С 06.09 зона `api` считает запросы на человека, а не на
адрес: ключ — хвост access-токена из `Authorization: Bearer …` (map
`$lc_rl_key` в `docker/nginx/nginx.conf`), 30 r/s + burst 60 на каждого; без
токена (login, refresh по cookie, статика) ключ по-прежнему адрес. Поверх стоит
страховочная зона `api_addr` по адресу — 200 r/s + burst 400, то есть семь
наших офисов. До этого весь офис за одним NAT делил 30 r/s на всех: 06.09 за
сутки набралось 344–364 отказа 429, все свои, пачками к началу смен.

Как читать. В `access.log` 429 с пустым `upstream_rt` — отбил nginx; с непустым —
приложение (`rate_limited` с `Retry-After`, это другой лимит). Кто именно — по
имени зоны в error.log; значение ключа туда не пишется намеренно, это кусок
подписи токена:

```bash
docker compose logs --tail=2000 nginx 2>&1 | grep -o 'by zone "[a-z_]*"' | sort | uniq -c
```

`by zone "api"` — один человек (вкладка или скрипт под его токеном) держит
больше 30 r/s дольше двух секунд; `"api_addr"` — весь адрес разом больше
200 r/s, так выглядит сканер или скрипт с мусорным Bearer; `"login"` и
`"hooks"` — как раньше, по адресу. Много «api» у разных людей в одну минуту —
смотреть не в nginx, а во фронт: кто-то шлёт залп.

Сертификат: продлевается сам контейнером `certbot`, nginx перечитывает его
reload'ом раз в 6 часов. Проверить руками:

```bash
docker compose exec certbot certbot certificates
docker compose exec nginx nginx -s reload
```
