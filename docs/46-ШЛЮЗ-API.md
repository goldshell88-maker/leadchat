# Шлюз внешних API на Амстердаме (проект, 16.09.2026)

Решение владельца 16.09: «пусть все взаимодействия с API будут на стороне
Амстердама, а на самом LeadChat кода их работы не было: если сервер лид-бота
отключён — API полностью не работают. Лид-бот — основа, где хранятся и бот,
и сами API; между ними мост, чтобы спокойно общаться».

Документ написан по коду обеих сторон (LeadChat — эта ветка; Амстердам —
`ssh leadchat-watch`, Ubuntu 26.04, Python 3.14, лид-бот как systemd-юнит на
системном `python3`, Caddy, WireGuard `10.10.0.2` с единственным пиром
`10.10.0.1` — прод LeadChat).

---

## 1. Что меняется, а что нет

| Что | Было | Станет |
|---|---|---|
| Ключи DaData, Яндекса, OpenRouter, Groq, Mistral, Anthropic | `.env` прода LeadChat | только `/opt/leadchat-gateway/.env` на Амстердаме |
| Код походов к провайдерам и разбора их ответов | `app/integrations/*.py`, `app/bots/ai.py` | `gateway/` (свой сервис на Амстердаме) |
| Цепочка проверки адреса, вердикты, кэш ответов (7 дней), суточные счётчики, кулдауны, настройки, монитор | LeadChat | LeadChat, без изменений |
| Мост OpenRouter в Caddy `10.10.0.2:8791` | нужен | не нужен: шлюз ходит в OpenRouter сам (после перехода — убрать блок из Caddyfile) |
| Авито, Telegram, Whisper (локальная модель) | напрямую | напрямую — это канал и локальная работа, не помощник |

Что НЕ переезжает и почему: цепочка адресов знает диалог (город из других
чатов, семья дома, пункт, названный для улицы) и держится сотнями тестов;
это правила LeadChat про его же данные. Шлюз — «ключи + сеть + разбор чужого
JSON», без состояния.

---

## 2. Шлюз: сервис `leadchat-gateway`

- Код: `gateway/` в этом репозитории (отдельный пакет, свой `pyproject.toml`
  и `requirements.txt`, без зависимости от `app.*`). Запуск на Амстердаме:
  systemd-юнит `leadchat-gateway.service`, `/opt/leadchat-gateway/.venv/bin/
  uvicorn leadchat_gateway.main:app --host 10.10.0.2 --port 8792`, окружение из
  `/opt/leadchat-gateway/.env` (`EnvironmentFile`), ключи там же.
- Слушает ТОЛЬКО адрес внутри WireGuard (`10.10.0.2`), как мост 8791. Из
  интернета недостижим. Дополнительно — сервисный токен `GATEWAY_TOKEN` в
  заголовке `Authorization: Bearer …`, сравнение через `hmac.compare_digest`.
- Без базы, без Redis: состояние (кэш, счётчики, кулдауны) остаётся в
  LeadChat. Шлюз перезапускается и обновляется независимо.
- Логи — journald, JSON (structlog). Тела запросов и адреса клиентов в лог
  не пишутся никогда: те же правила, что у `GeocodeError` (без URL и тела).

### 2.1 Ручки

Все ответы — JSON. Успех: HTTP 200 и `{"ok": true, …}`. Отказ провайдера —
тоже HTTP 200, но `{"ok": false, "kind": …, "status": <код провайдера или
null>, "detail": <короткая причина без адреса>}`. Так LeadChat отличает
«шлюз жив, провайдер отказал» от «шлюза нет» (обрыв/5xx/401 шлюза).

`kind` ∈ `network` (таймаут/обрыв — повтор уместен), `blocked` (403/429 или
HTML вместо JSON — повторять вредно), `bad_response` (JSON не той формы),
`auth` (401 — ключ отвергнут), `no_key` (ключа в окружении шлюза нет),
`exhausted` (все точки читателя исчерпаны).

| Ручка | Вход | Выход (`data`) |
|---|---|---|
| `GET /health` | — (без токена) | `{"ok": true, "version": …}` |
| `GET /status` | — | по каждому провайдеру: `key_present`, `base_url` (без строки запроса) |
| `POST /geo/dadata` | `text` (одна строка запроса, уже собранная LeadChat), `region`, `city`, `near` | `hits: [GeoHit]` — один запрос к DaData |
| `POST /geo/dadata/place` | `query_text`, `region`, `city` | `hits: [PlaceHit]` |
| `POST /geo/dadata/city` | `city`, `region` | `point: [lat, lon] \| null` |
| `POST /geo/nominatim` | `mode` (`structured`/`free`), `street`, `house`, `city`, `region`, `free_text` | `hits: [GeoHit]` — один GET; шлюз сам держит темп 1 запрос/с к OSM |
| `POST /geo/yandex` | `free_text`, `city` | `hits: [GeoHit]` |
| `POST /geo/yandex-suggest` | `free_text` | `hits: [Suggested]` |
| `POST /geo/ahunter` | `text` | `hits: [Suggested]` |
| `POST /spell` | `text`, `words: [str]` | `fixes: {слово: замена}` (выбор варианта — в шлюзе) |
| `POST /llm/chat` | `system`, `user`, `deadline_sec` | `content: dict`, `model: str`, `attempts: int` — перебор OpenRouter → Groq → Mistral внутри шлюза; при отказе `attempts` тоже возвращается |
| `POST /llm/anthropic/tool` | `model`, `system`, `messages`, `tool`, `max_tokens`, `timeout_sec` | `input: dict` |
| `POST /check/{provider}` | — | итог пробы `{ok, status, ms, error}` тем же адресом и ключом, что и работа |
| `POST /check/url` | `url` | итог пробы произвольного адреса (сторож «не внутрь»: 10.10.0.0/24, 127/8, link-local, `.internal`, `localhost`, любые не-глобальные IP; по редиректам не идём) |

**Граница «что спросить / как спросить».** Обёртка LeadChat решает, ЧТО спрашивать —
циклы по текстам у DaData («пункт + улица», «улица», варианты «/»), три попытки у OSM,
какие слова проверять у Спеллера, кэш, счётчики, маппинг ошибок; шлюз — КАК спросить
конкретный API (тело, параметры, заголовки, ключ, разбор ответа, каскад статусов).
Один вызов шлюза = один HTTP-запрос к провайдеру, кроме `/llm/chat`, где перебор
точек внутри и число попыток возвращается. Так `on_request` и кэш сохраняют сегодняшнюю
семантику «на запрос», а `seen`/`wait` остаются у обёртки.

### 2.2 Общие типы

`Query` — ровно то, что LeadChat собирает из компонентов разбора (никогда из
текста сообщения): `region`, `city`, `settlement`, `street`, `house`, плюс
готовые строки `street_for_map` и `free_text` (их считает LeadChat своим
словарём типов улиц — шлюз словаря не знает). `GeoHit`, `PlaceHit`,
`Suggested` (Яндекс и Ahunter — свои наборы полей) повторяют dataclass-ы
LeadChat один в один по именам полей: обёртка в LeadChat собирает их
`GeoHit(**d)`.

### 2.3 Что переезжает из `app/integrations/`

| Модуль | В шлюз | Остаётся в LeadChat (обёртка с прежним именем) |
|---|---|---|
| `dadata.py` | HTTP, `locations_for` (фильтр регионов: ISO, КЛАДР (Крым)), тела запросов, `parse_response`, `parse_places` | `search/search_place/city_point/enabled`, цикл текстов и `house_for_query`, `seen`; кэш `geo_cache` |
| `nominatim.py` | HTTP, разбор, `user_agent` | `nominatim.search`; кэш; `Wait` (вежливость к OSM) — остаётся у воркера |
| `yandex_geocoder.py`, `yandex_suggest.py` | HTTP, разбор | `search/suggest/enabled`; кэш |
| `ahunter.py` | HTTP, `_ДОМ`, `parse_value`, `parse_response`, `query_text` | `ahunter.suggest`; кэш |
| `speller.py` | HTTP к Спеллеру, `pick` (выбор варианта) | `fix_street` (какие слова проверять, `apply`) → шлюз даёт замены; кэш |
| `openrouter.py` | точки (`endpoints`), перебор, дедлайн, `parse_content` | `chat_json`, `enabled` (= шлюз настроен и у него есть хоть один ключ), `OpenRouterError` |
| `app/bots/ai.py` | вызов Anthropic (`_call_tool`) | промпты, схемы tool-use, breaker, `AI_FAKE`, `ai_answer/classify_message/extract_entities` |

`enabled()` у провайдеров: раньше — «ключ в env», теперь — «шлюз настроен»
(`GATEWAY_URL`+`GATEWAY_TOKEN`) и по снимку `/status` у провайдера есть ключ.
Снимок живёт в процессе (`gateway.known_keys`, обновление `refresh_status()`
раз в 60 с из async-кода); пока снимка нет — считаем, что ключ есть; ответ
`kind: no_key` помечает ключ отсутствующим и поднимает `GeocodeError(provider,
"blocked")` — ровно как сегодня при пустом ключе. `enabled()` остаётся
синхронной: её зовут в шести местах воркера и ручек.

Параметр `client: httpx.AsyncClient | None` у обёрток остаётся (тесты
подставляют транспорт к ШЛЮЗУ), `on_request` — перед каждым походом в шлюз
(= перед каждым запросом к провайдеру, как сегодня); у `chat_json` — один раз
до похода и ещё `attempts-1` раз после (счётчик `geo:llm:calls` без потолка).

Кэш `geo_cache` остаётся в обёртках: payload = `{"v": 2, …аргументы вызова
шлюза}`, значение — разобранные `hits`. Старые записи `geo:cache:*` (сырой JSON
провайдеров) не читаются — в день перехода кэш холодный, переход делать
ночью; потолки DaData защищают от перерасхода как обычно.

Фильтр региона у DaData (`locations_for`, `_РЕГИОН_ФИЛЬТР`): по умолчанию —
имя региона без типа («Хабаровский»); регионы, чьё имя так не получить, и
города федерального значения с областью вокруг — готовыми объектами фильтра.
Для большинства это код ISO 3166-2 (`region_iso_code`), а для Крыма и
Севастополя — ТОЛЬКО код КЛАДР региона (`{"kladr_id": "91"}` / `"92"`, стенд
19.09): справочник DaData живёт по международному ISO (`UA-43`/`UA-40`), кодов
`RU-CR`/`RU-SEV` в нём нет, а фильтр по несуществующему коду отдаёт пустоту
молча — места Крыма «по области» уходили в `not_found`. Код КЛАДР от политики
справочника не зависит; `UA-43` в код не кладём. Строки, осуждённые за это
время, возвращает `address-recheck --regions "Республика Крым,Севастополь"`
(отбор по региону объявления через `conversation_city`; строки без города
объявления в срез не входят и идут общим сбросом) — не раньше чем через сутки
после выкатки шлюза: пустой ответ живёт в кэше `TTL_EMPTY` = 24 ч.

Anthropic для ботов: `ai.get_client()` отдаёт адаптер `GatewayAnthropic` с тем
же интерфейсом `await client.messages.create(**kwargs)` → объект с `.content`
из блока `tool_use`; `_call_tool` и тесты, подменяющие `get_client`, не меняются.

### 2.4 Отказы

| Что случилось | Шлюз отвечает | LeadChat делает |
|---|---|---|
| Провайдер: таймаут/5xx | `{ok:false, kind:"network"}` | `GeocodeError("network")` — те же кулдауны, что сегодня |
| Провайдер: 403/429/HTML | `kind:"blocked"` | `GeocodeError("blocked")` — бан провайдера на сутки, как сегодня |
| Ключа нет на шлюзе | `kind:"no_key"` | `enabled()` = False; в мониторе «ключ не задан (на шлюзе)» |
| Шлюз недоступен / 5xx / 401 | — | `GeocodeError("network")` (`OpenRouterError("network")`), лог `gateway.unreachable`; в мониторе строка «Шлюз (Амстердам)» краснеет; воркер живёт дальше без помощников — как при «карта не отвечает» |

Шлюз лёг — помощники молчат целиком: это и есть требование владельца.
Автозапись адреса, вердикты по правилам без карты (`no_map`) — работают как
сегодня без API.

### 2.5 Монитор (`/settings/apis`)

Встроенные строки остаются в LeadChat; меняется, откуда берутся два поля:
`key_present` — из снимка `/status` шлюза; «Проверить» — `POST /check/{provider}`
на шлюзе (у него ключи и сеть; `Probe` получает поле `provider`), свои записи —
`POST /check/url`. Появляется строка «Шлюз API (Амстердам)» первой в списке —
`GET /health` напрямую (единственный прямой поход монитора); без неё всё
остальное «нет связи со шлюзом». Счётчики, кулдауны, потолки, таблица
`api_registry` — как есть. `_итог`, `_чужой_адрес` и словарь ошибок переезжают
в шлюз.

---

## 3. LeadChat: один клиент

`app/integrations/gateway.py`: `settings.gateway_url`, `settings.gateway_token`,
`enabled()`, `status()` (кэш 60 с), `call(path, payload, *, timeout, client)`
→ `dict` или `GatewayError(kind)`. Никаких других `httpx`-клиентов к внешним
сервисам в `app/` не остаётся, кроме Авито и Telegram — это охраняет тест
`tests/unit/test_gateway_only.py`: грепом по `app/` ищет хосты провайдеров и
`api_key`-настройки провайдеров в `config.py`.

Из `.env` прода уходят `DADATA_*`, `YANDEX_*`, `OPENROUTER_*`, `GROQ_*`,
`MISTRAL_*`, `ANTHROPIC_*`; приходят `GATEWAY_URL=http://10.10.0.2:8792` и
`GATEWAY_TOKEN`. `.env.example`/`.env.prod.example` — соответственно.

---

## 4. Установка и переход

1. `bash gateway/deploy.sh` (с мака, `ssh leadchat-watch`): rsync `gateway/` →
   `/opt/leadchat-gateway/src` (без tests/.env/.venv), `python3 -m venv .venv`
   (пакет `python3.14-venv` ставится apt один раз), `pip install -r
   requirements.txt`, юнит в `/etc/systemd/system/`, `systemctl enable --now`,
   проверка `curl http://10.10.0.2:8792/health` с самого Амстердама. Каталог
   `/opt/leadchat-gateway` — 755 (юнит работает под `DynamicUser`, не root),
   секреты только в `.env` (0600, читает systemd до сброса прав). Файл
   `gateway/VERSION` пишет скрипт, он в `.gitignore`.
2. Ключи: переносятся с прода трубой сервер→сервер (`ssh leadchat 'grep …
   /srv/leadchat/.env' | ssh leadchat-watch 'cat >> /opt/leadchat-gateway/.env'`)
   — на экран не выводятся; `*_BASE_URL` мостов не переносятся (шлюз ходит к
   провайдерам напрямую). `GATEWAY_TOKEN` генерируется на Амстердаме
   (`openssl rand -hex 32`) и той же трубой дописывается в `.env` прода вместе
   с `GATEWAY_URL`. **После любой правки `.env` — `systemctl restart
   leadchat-gateway`**: окружение читается при старте.
3. Живая проба с прода: `curl` в `/health`, затем `POST /check/dadata` и т.д.
4. Выкатка LeadChat (`ship.sh`; он требует `GATEWAY_TOKEN` в `.env` прода и
   гоняет тесты шлюза); «Проверить все» в мониторе; стенд адресов на 30 днях —
   до и после, вердикты не должны разъехаться.
5. Только после этого — удалить ключи провайдеров из `.env` прода и блок
   моста 8791 из Caddyfile Амстердама.

Откат: вернуть ключи в `.env` прода и откатить образ (`deploy/rollback.sh`) —
старый код ходит к провайдерам сам.

---

## 5. Проверка

- Тесты провайдеров (respx на адреса провайдеров) переезжают вместе с кодом
  в `gateway/tests/` и гоняются из корневого окружения:
  `uv run pytest gateway/tests -q`. `ship.sh` получает этот шаг.
- В LeadChat: обёртки проверяются respx на адрес шлюза
  (`tests/unit/test_gateway_client.py`); три тестовых файла, мокавшие адреса
  DaData/Nominatim напрямую, перенастраиваются на шлюз; остальные мокают
  функции (`monkeypatch.setattr(dadata, "search", …)`) и не меняются;
  autouse-заглушки `conftest` (`_без_сети_подсказчиков`,
  `_без_запасных_читателей`) остаются.
- `ruff`/`mypy` — на `app`, `tests` и `gateway`.
- Живое: п. 4.

---

## 6. Ограничения и решения владельца

- Персональные данные клиентов (адрес — да, телефон/имя — никогда) теперь
  проходят через Амстердам так же, как сегодня через мост OpenRouter; оба
  сервера — владельца.
- Anthropic: в `app/bots/ai.py` записано, что обходить региональные
  ограничения провайдера через промежуточные узлы нельзя. Шлюз даёт ручку
  `/llm/anthropic/tool`; класть ли ключ Anthropic на Амстердам — решение
  владельца, код этого не делает сам.
- Амстердам — единственная точка отказа для всех помощников (по требованию).
  Сторож `healthcheck-alert.sh` получает проверку `/health` шлюза с прода.
