"""RBAC matrix (07 §1.1.3): every endpoint × every role, plus the guard
test — any new endpoint must be added to MATRIX, EXEMPT (public) or
COVERED_ELSEWHERE (RBAC покрыт целевыми тестами модуля)."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.exc import DatabaseError

from app.core.rbac import PERMISSIONS, ROLE_PERMISSIONS, ROLES

A, H, M, O = "admin", "head", "manager", "observer"  # noqa: E741 — matrix shorthand (07 §1.1.3)
ALLOW, DENY = "allow", "deny"  # observer on write endpoints must get exactly 403, not 401
# ALLOW, но успешный ответ ручки требует PostgreSQL (оконные функции, jsonb,
# materialized view — 06 §2/§3): на SQLite-стеке юнитов проверяем ровно то,
# ради чего эта матрица существует — «RBAC пропустил роль» (не 401/403).
ALLOW_PG = "allow_pg"

# Sprint 1: auth. Sprint 2: conversations read-side + WS ticket (01 §13).
MATRIX = [
    ("GET", "/api/v1/auth/me", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    # Своё состояние «на месте» / «отошёл» (#34, чтение добавлено 28.08). Права
    # не спрашивает по замыслу — человек читает СВОЁ, и чужое здесь не задать:
    # идентификатор берётся из сессии. В отличие от парного PUT эта ручка без
    # тела, поэтому проверяется здесь честно, а не через список исключений.
    ("GET", "/api/v1/presence", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    # Правило освобождения для себя — читает каждый про себя самого.
    ("GET", "/api/v1/presence/release", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    ("POST", "/api/v1/auth/logout", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    ("GET", "/api/v1/conversations", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    # Числа над вкладкой «Мои»: право то же, что на чтение списка — они не
    # показывают ничего, чего смотрящий не увидел бы, открыв вкладку.
    ("GET", "/api/v1/conversations/counts", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    ("GET", "/api/v1/conversations/{conversation_id}", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    (
        "GET",
        "/api/v1/conversations/{conversation_id}/messages",
        {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW},
    ),
    (
        "POST",
        "/api/v1/conversations/{conversation_id}/read",
        {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW},
    ),
    ("POST", "/api/v1/ws/ticket", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    # OAuth-зона (01 §4): чтение — здесь; мутирующие ручки — COVERED_ELSEWHERE.
    ("GET", "/api/v1/avito/connect-url", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Ключи приложения Авито: читать может только администратор — секрет
    # наружу не уходит, но остальное (адреса, кто задан) тоже не для всех.
    ("GET", "/api/v1/avito/app", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    ("GET", "/api/v1/avito-accounts", {A: ALLOW, H: ALLOW, M: DENY, O: DENY}),
    # Sprint 3: read-side ручек управления диалогами, шаблонов и исполнителей.
    (
        "GET",
        "/api/v1/conversations/{conversation_id}/client-history",
        {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW},
    ),
    ("GET", "/api/v1/templates", {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    ("GET", "/api/v1/templates/folders", {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    ("GET", "/api/v1/users/assignable", {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    # Sprint 4: статистика (01 §9, 06 §4) и журнал аудита (01 §9.7).
    # `stats:all` — admin/head; менеджеру весь раздел закрыт, кроме своего
    # виджета (`stats:own`); observer — 403 на всё (DESIGN §5.1).
    ("GET", "/api/v1/stats/summary", {A: ALLOW_PG, H: ALLOW_PG, M: DENY, O: DENY}),
    ("GET", "/api/v1/stats/timeseries", {A: ALLOW_PG, H: ALLOW_PG, M: DENY, O: DENY}),
    ("GET", "/api/v1/stats/heatmap", {A: ALLOW_PG, H: ALLOW_PG, M: DENY, O: DENY}),
    ("GET", "/api/v1/stats/managers", {A: ALLOW_PG, H: ALLOW_PG, M: DENY, O: DENY}),
    ("GET", "/api/v1/stats/my/today", {A: ALLOW_PG, H: ALLOW_PG, M: ALLOW_PG, O: DENY}),
    ("GET", "/api/v1/audit-log", {A: ALLOW, H: ALLOW, M: DENY, O: DENY}),
    ("GET", "/api/v1/audit-log/filters", {A: ALLOW, H: ALLOW, M: DENY, O: DENY}),
    # Спринт 6: боты (01 §8, §12). `bots:manage` есть только у admin — сценарий
    # бота говорит с клиентами от имени сервиса, это код, а не настройка.
    ("GET", "/api/v1/bots", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Надзор за ботом (docs/45) — то же право `bots:manage`, и руководителю его
    # не дали намеренно: он смотрит за людьми, а за ботом смотрит тот, кто им
    # управляет. Увидеть сбой и не иметь возможности его починить — худший из
    # видов надзора.
    # Забрать диалог у бота (запрос владельца 29.08). Право то же, что у
    # отправки, — `messages:send`: кнопка заменяет собой путь «ответь, чтобы
    # заглушить бота», и требовать за неё больше прав было бы странно.
    #
    # ⚠ В MATRIX, А НЕ В ИСКЛЮЧЕНИЯХ, хотя соседние ручки очереди с тем же
    # правом лежат там. У них ALLOW-ветка второго запроса упирается в 409
    # «уже принят»; здесь повтор — штатный успех (`taken: false`), и проверка
    # остаётся проверкой доступа, а не состояния диалога.
    (
        "POST",
        "/api/v1/conversations/{conversation_id}/bot/mute",
        {A: ALLOW, H: DENY, M: ALLOW, O: DENY},
    ),
    # Вход в чужой рабочий диалог (просьба владельца 03.09: «чтобы можно было
    # спокойно заходить в чужой диалог… и он появлялся так же у тебя в „Мои"»).
    # Право то же, что у ответа клиенту: заходят, чтобы работать вместе.
    # Руководителю и наблюдателю места в чужом рабочем списке не полагается —
    # клиентам они не отвечают.
    (
        "POST",
        "/api/v1/conversations/{conversation_id}/enter",
        {A: ALLOW, H: DENY, M: ALLOW, O: DENY},
    ),
    # Решётка «люди × каналы» и массовое назначение (просьба владельца 04.09:
    # «я вручную по 30 раз захожу и тыкаю»). Читать состав вправе тот же, кто
    # видит каналы, — руководитель тоже; менять — только администратор, как и
    # у поканальной ручки.
    ("GET", "/api/v1/avito-accounts/operators/grid", {A: ALLOW, H: ALLOW, M: DENY, O: DENY}),
    ("GET", "/api/v1/bot-dialogs", {A: ALLOW_PG, H: DENY, M: DENY, O: DENY}),
    ("GET", "/api/v1/bot-dialogs/live", {A: ALLOW_PG, H: DENY, M: DENY, O: DENY}),
    # Лид-бот — свой раздел (решение владельца 12.08), но право то же
    # (`bots:manage`, только admin) и по той же причине: здесь включается
    # автоответ живым клиентам девяти каналов и меняется адрес чужого
    # сервиса, которому уходит переписка.
    ("GET", "/api/v1/leadbot", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Подробный след — настройка системы (`settings:manage`, только admin):
    # включение меняет объём записи о переписке клиентов.
    ("GET", "/api/v1/settings/trace", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Автозаявки: право `settings:manage` (только admin) — здесь выпускается
    # токен, открывающий наружу телефоны клиентов.
    ("GET", "/api/v1/settings/leads", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    ("GET", "/api/v1/settings/leads/handouts", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    ("GET", "/api/v1/leadbot/calls", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # «Почему бот молчит» — тот же раздел и та же строгость: в ответе лежат
    # идентификаторы диалогов и причины, по которым бот в них не вошёл.
    ("GET", "/api/v1/leadbot/silence", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Голосовое сообщение клиента — часть переписки: кто видит диалоги, тот и
    # слышит. Отдельного права заводить не за что, но и «всем подряд» нельзя.
    # Спринт 7: управление сотрудниками (01 §3, право `users:manage` — только
    # admin по DESIGN §5.1). В матрице держим единственную ручку без тела и без
    # плейсхолдеров: её ALLOW-ветка отдаёт честные 200, а не 400/404.
    ("GET", "/api/v1/users", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Блок 7.1: чтение очереди «Входящие» (15 §2.1). Право `conversations:read`
    # есть у всех четырёх ролей — руководителю очередь нужна, чтобы увидеть
    # затор и разобрать его передачей (01 §5.5), наблюдателю она просто видна.
    # Кнопка «Принять» — другое право (`messages:send`), её ручки лежат в
    # COVERED_ELSEWHERE вместе с `/assign` и `/status` по той же причине.
    ("GET", "/api/v1/inbox", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    ("GET", "/api/v1/inbox/count", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    # Блок 7.2: «Мои каналы» в профиле (15 §2.2). Отдельного права нет намеренно:
    # `accounts:read` есть только у admin и head, а вопрос «почему мне приходят
    # одни обращения и не приходят другие» задаёт как раз менеджер. Чужой
    # список — уже `accounts:read`, но у той ручки плейсхолдер {user_id},
    # поэтому она в COVERED_ELSEWHERE.
    ("GET", "/api/v1/me/channels", {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    # Настройки автораспределения (право `settings:manage` — только admin).
    # Чтение закрыто наравне с записью намеренно: потолок диалогов на человека
    # — это норма выработки, и оператору знать её незачем.
    ("GET", "/api/v1/settings/distribution", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Рабочие часы статистики (#41) — то же право, что у раздачи: и то и другое
    # решает, как работает смена, и настраивает это владелец.
    ("GET", "/api/v1/settings/work-hours", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Распознавание телефонов в тексте (правка 10 от 12 августа) — то же право
    # и по более веской причине: включённая запись «сразу в карточку» означает,
    # что цепочка цифр из чужого сообщения молча становится телефоном, по
    # которому звонят. Такое решает владелец, а не смена.
    ("GET", "/api/v1/settings/phone-detect", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    ("GET", "/api/v1/settings/address-detect", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    ("GET", "/api/v1/settings/apis", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Показ служебных записей Авито в ленте (14 августа). Право то же и по
    # своей причине: настройка меняет СОСТАВ ленты у всех тринадцати разом,
    # а не вид у одного человека. Руководителю закрыто намеренно — иначе
    # двое смотрели бы на одну переписку с разным числом сообщений.
    ("GET", "/api/v1/settings/thread", {A: ALLOW, H: DENY, M: DENY, O: DENY}),
    # Чёрный список (docs/19). Право то же, что у работы с диалогами: помечает
    # тот, кто с этим клиентом и разговаривает. Наблюдателю закрыто — он вообще
    # ничего не меняет.
    ("GET", "/api/v1/clients/blocked", {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
]

# Public endpoints (no Bearer): login, refresh, invite, health, webhook,
# OAuth callback (state-токен вместо Bearer) — 01 §13.
EXEMPT = {
    # По ссылке приходит человек с паролем от аккаунта Авито — он в LeadChat
    # не заходит и не обязан. Права заменяет одноразовый токен в пути, как у
    # приглашения сотрудника (01 §13).
    ("GET", "/api/v1/avito/connect/{token}"),
    ("GET", "/api/health"),
    ("GET", "/api/health/deep"),  # зонды мониторинга без Bearer (05 §7.2)
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("GET", "/api/v1/auth/invite/{token}"),
    ("POST", "/api/v1/auth/invite/accept"),
    ("POST", "/api/hooks/avito/{account_id}"),  # webhook_secret вместо Bearer (01 §10)
    # Автозаявки: сюда ходит РАСШИРЕНИЕ владельца, а не сотрудник. У машины нет
    # сессии и быть не может — она работает по расписанию, в том числе когда за
    # компьютером никого нет. Права заменяет собственный токен в заголовке, и
    # проверяет его `require_lead_token`; без настроенного токена обе ручки
    # закрыты для всех. Матрица ролей к ним неприменима: роли тут нет вовсе.
    # Проверки доступа — в `tests/unit/test_leads_api.py`.
    ("GET", "/api/v1/leads"),
    ("POST", "/api/v1/leads/{uid}/ack"),
    ("GET", "/api/v1/avito/callback"),
    ("POST", "/api/v1/avito/callback"),
    # Раздача вложений публична по подписи с TTL 1 ч (DESIGN §1.4, 05 §3.2):
    # Bearer здесь нет намеренно — в проде файл отдаёт nginx secure_link.
    ("GET", "/api/v1/media/{relpath}"),
    # Заявка со страницы входа (14 §2.2): прав нет по построению — человек как
    # раз не может войти. Ответ одинаков для существующей и несуществующей
    # учётки, поэтому раскрыть список сотрудников через неё нельзя.
    ("POST", "/api/v1/support/password-reset"),
    # Сервисный токен вместо Bearer, как у вебхука Авито (14 §2.1): ручку зовут
    # скрипты на хосте, у которых сессии нет и быть не может.
    ("POST", "/api/v1/internal/notify"),
}

# RBAC этих ручек заперт целевыми тестами своего модуля (403-ассерты в
# tests/unit/test_avito_oauth.py); в матрице их держать нельзя — ALLOW-ветка
# требует сложных сайд-эффектов (регистрация вебхука в Авито и т.п.).
COVERED_ELSEWHERE = {
    # Голосовое сообщение: ALLOW-ветка матрицы требует, чтобы у сообщения БЫЛО
    # вложение-голос и чтобы Авито ответил ссылкой, — иначе честный 404, а не
    # отказ прав. Право заперто в tests/unit/test_voice_message.py:
    # наблюдатель получает ссылку, аноним — 401.
    ("GET", "/api/v1/messages/{message_id}/voice"),
    # PATCH настроек требует тела, поэтому ALLOW-ветка матрицы упёрлась бы в
    # 400, а не в отказ RBAC. Право заперто в tests/unit/test_settings_api.py.
    ("PATCH", "/api/v1/settings/distribution"),
    ("PATCH", "/api/v1/settings/work-hours"),
    ("PATCH", "/api/v1/settings/phone-detect"),
    ("PATCH", "/api/v1/settings/address-detect"),
    ("PATCH", "/api/v1/settings/thread"),
    # Монитор внешних сервисов: POST требует тела, PATCH/DELETE/check —
    # существующей записи (иначе 404, а не отказ прав). Право заперто в
    # tests/unit/test_api_monitor_1609.py (менеджеру — 403 на все четыре).
    ("POST", "/api/v1/settings/apis"),
    ("PATCH", "/api/v1/settings/apis/{key}"),
    ("DELETE", "/api/v1/settings/apis/{key}"),
    ("POST", "/api/v1/settings/apis/{key}/check"),
    # Пометка клиента требует существующего клиента, поэтому ALLOW-ветка
    # матрицы упёрлась бы в 404, а не в отказ RBAC. Право заперто в
    # tests/unit/test_blocklist.py.
    ("POST", "/api/v1/clients/{client_id}/block"),
    ("POST", "/api/v1/clients/{client_id}/unblock"),
    # Личность клиента, ручной телефон и объединение карточек (правка 9 от 12
    # августа) — та же причина, что у пометки: все пять ручек требуют
    # СУЩЕСТВУЮЩЕГО клиента, и ALLOW-ветка матрицы упёрлась бы в 404, то есть
    # проверяла бы наличие строки в базе, а не отказ доступа. Право
    # (`conversations:manage`) и отказ наблюдателю заперты целевыми тестами:
    # tests/unit/test_client_phone.py::test_observer_cannot_edit_the_phone и
    # tests/unit/test_client_merge.py::test_observer_cannot_merge. Чтение
    # личности открыто `conversations:read` (24.09): наблюдателю нужен адрес.
    ("GET", "/api/v1/clients/{client_id}/identity"),
    ("GET", "/api/v1/clients/{client_id}/merge-candidates"),
    ("PUT", "/api/v1/clients/{client_id}/phone"),
    # Имя клиента руками (требование заказчика 13 августа) — та же причина и то же
    # право, что у телефона выше. Отказ наблюдателю заперт целевым тестом
    # tests/unit/test_client_name.py::test_observer_cannot_rename.
    ("PUT", "/api/v1/clients/{client_id}/name"),
    ("POST", "/api/v1/clients/{client_id}/merge"),
    ("POST", "/api/v1/clients/{client_id}/unmerge"),
    # Решение по распознанному номеру (правка 10, там же): требует и клиента, и
    # существующей строки-кандидата, поэтому ALLOW-ветка матрицы упёрлась бы в
    # 404. Право и отказ наблюдателю заперты в
    # tests/unit/test_phone_candidate_api.py::test_an_observer_cannot_decide.
    ("POST", "/api/v1/clients/{client_id}/phone-candidates/{candidate_id}/resolve"),
    # Обмен основного номера с дополнительным (жалоба владельца 09.09): ручка
    # требует ДИАЛОГ ЭТОГО клиента в теле, поэтому ALLOW-ветка матрицы упёрлась
    # бы в 422 «Диалог не этого клиента» — то есть проверяла бы состояние, а не
    # отказ доступа. Право (`conversations:manage`) и отказ наблюдателю заперты
    # в tests/unit/test_phone_primary_0909.py::test_наблюдатель_не_меняет_основной.
    ("POST", "/api/v1/clients/{client_id}/phone/primary"),
    # Адрес выезда руками и решение по распознанному адресу (09.09). Право и
    # отказ наблюдателю заперты целевыми проверками в
    # tests/unit/test_address_card_0909.py; здесь ALLOW-ветка упёрлась бы в 404
    # на несуществующей строке-кандидате, то есть проверяла бы состояние, а не
    # доступ.
    ("PUT", "/api/v1/clients/{client_id}/address"),
    ("POST", "/api/v1/clients/{client_id}/address-candidates/{candidate_id}/resolve"),
    # «Адрес неверный» (пакет 6.0а, 20.09): та же причина и то же право
    # (`conversations:manage`), что у PUT адреса; отказ наблюдателю заперт в
    # tests/unit/test_paket60a_screen_2009.py::test_без_права_403.
    ("POST", "/api/v1/clients/{client_id}/address/wrong"),
    # Ответ на предложение передачи (требование заказчика от 7 августа):
    # ALLOW-ветка матрицы упёрлась бы в 422 «вам этот диалог не передавали» —
    # то есть проверяла бы состояние диалога, а не отказ доступа. Право
    # (`messages:send`) и правило «отвечать может только адресат» заперты в
    # tests/unit/test_transfer.py.
    ("POST", "/api/v1/conversations/{conversation_id}/transfer/accept"),
    ("POST", "/api/v1/conversations/{conversation_id}/transfer/decline"),
    # Отмена своего предложения (просьба владельца 04.09) — та же причина:
    # ALLOW-ветка матрицы упёрлась бы в 422 «предложение уже снято», то есть
    # проверяла бы состояние диалога, а не отказ доступа. Дверь здесь шире
    # соседей (`conversations:read`), потому что передавать умеет и
    # руководитель, которому отправка закрыта; правило «отменяет начавший или
    # обладатель conversations:manage» и 403 наблюдателю заперты в
    # tests/unit/test_transfer_cancel_0509.py.
    ("POST", "/api/v1/conversations/{conversation_id}/transfer/cancel"),
    # Смена своего пароля (требование от 7 августа): требует тела с ВЕРНЫМ
    # текущим паролем, поэтому ALLOW-ветка матрицы упёрлась бы в 422 «текущий
    # пароль не подходит», то есть проверяла бы пароль, а не доступ. Право
    # (любая роль) и 401 анониму заперты в
    # tests/unit/test_own_profile.py.
    ("POST", "/api/v1/auth/change-password"),
    # Своё имя: тоже требует тела (пустое даёт 422 на разборе), поэтому в
    # MATRIX не встаёт. Меняет каждый сам, включая наблюдателя; роль и почта
    # здесь не трогаются — первое вопрос прав, второе логин.
    ("PATCH", "/api/v1/auth/me"),
    # Свои горячие клавиши (требование заказчика 13 августа). Прав не спрашивает по
    # тому же замыслу, что и соседи: человек правит СВОЁ рабочее место, как и свой
    # пароль, а чужое здесь не задать — идентификатор берётся из сессии. В MATRIX не
    # встаёт по той же причине: без тела ручка отвечает 422 на разборе, то есть
    # проверялась бы схема, а не доступ. Поведение заперто в
    # tests/unit/test_own_hotkeys.py.
    ("PUT", "/api/v1/auth/me/hotkeys"),
    # Своё состояние «на месте» / «отошёл» (#34). Прав не спрашивает по
    # замыслу: человек распоряжается своим состоянием, а не чужим, и чужое
    # здесь не задать — идентификатор берётся из сессии, а не из тела. В
    # MATRIX не встаёт по той же причине, что и соседи выше: без тела ручка
    # отвечает 422 на разборе, то есть проверялась бы схема, а не доступ.
    # Право (любая роль) и 401 анониму заперты в
    # tests/unit/test_presence_away.py.
    ("PUT", "/api/v1/presence"),
    # Подписки аккаунта в Авито (#39): ALLOW-ветка идёт в Авито по сети, то есть
    # проверяла бы доступность стороннего сервиса, а не отказ RBAC. Право и 401
    # анониму заперты в tests/unit/test_avito_subscriptions.py.
    ("GET", "/api/v1/avito-accounts/{account_id}/subscriptions"),
    # Закрепить/открепить у себя (требование от 7 августа). Право —
    # `conversations:read`, то есть у всех: закрепление ничего не меняет ни в
    # диалоге, ни у коллег. В MATRIX не держим: ALLOW-ветка упёрлась бы в 422
    # «закреплять можно только свои диалоги» (у seed-диалога другой
    # ответственный), то есть проверяла бы правило отметки, а не отказ
    # доступа. Право и 401 анониму заперты в
    # tests/unit/test_pins.py::test_pinning_is_for_everyone_who_can_read.
    ("POST", "/api/v1/conversations/{conversation_id}/pin"),
    ("DELETE", "/api/v1/conversations/{conversation_id}/pin"),
    # `GET /inbox/stale` и `POST /inbox/close-stale` отсюда убраны вместе с
    # самими ручками: разгрузку очереди сняли по требованию владельца от 11
    # августа (№8). Оставь мы строки — не упало бы ничего (полнота проверялась
    # только в одну сторону: «каждая ручка описана», а не «каждое исключение
    # существует»), и список исключений тихо оброс бы ссылками на удалённые
    # пути и удалённые тесты. Обратную проверку добавили ниже —
    # test_exceptions_do_not_outlive_their_endpoints.
    # Позвать коллегу в диалог (docs/19). POST требует тела — ALLOW-ветка
    # упёрлась бы в 400, а у DELETE в пути есть `{user_id}`, которого `_format`
    # не подставляет вовсе. Право (`messages:send`: зовёт тот, кто отвечает
    # клиентам) и 401 анониму заперты в
    # tests/unit/test_participants.py::test_inviting_needs_the_right_to_answer.
    # Массовое назначение требует тела (ALLOW-ветка упёрлась бы в 422). Право
    # (`accounts:manage`, только администратор) и отказ прочим заперты в
    # tests/unit/test_operators_bulk_0409.py.
    ("POST", "/api/v1/avito-accounts/operators/bulk"),
    ("POST", "/api/v1/conversations/{conversation_id}/participants"),
    ("DELETE", "/api/v1/conversations/{conversation_id}/participants/{user_id}"),
    ("POST", "/api/v1/avito-accounts/{account_id}/reconnect"),
    ("POST", "/api/v1/avito-accounts/{account_id}/disable"),
    ("POST", "/api/v1/avito-accounts/{account_id}/enable"),
    # Загрузка истории канала (17.08): право accounts:manage проверяется
    # декоратором ручки, uuid в пути не даёт ALLOW-ветке дойти до логики.
    ("POST", "/api/v1/avito-accounts/{account_id}/backfill"),
    # Удаление заметки (17.08): право notes:write + проверка автора в ручке.
    ("DELETE", "/api/v1/messages/{message_id}"),
    # Подписаться заново и убрать канал из списка (доводка экрана каналов).
    # `_format` подставляет account_id="x", а ручки объявляют его как uuid —
    # ALLOW-ветка упёрлась бы в 422 на разборе пути. Право `accounts:manage`
    # заперто в tests/unit/test_avito_channel_actions.py.
    ("POST", "/api/v1/avito-accounts/{account_id}/register-webhook"),
    ("DELETE", "/api/v1/avito-accounts/{account_id}"),
    ("PATCH", "/api/v1/avito-accounts/{account_id}"),
    # Размер переписки канала для подтверждения необратимого действия: тот же
    # uuid в пути и та же причина. Право `accounts:manage` (числа спрашивает
    # тот, кто собрался стирать) заперто в
    # tests/unit/test_avito_channel_actions.py::test_history_size_is_admin_only.
    ("GET", "/api/v1/avito-accounts/{account_id}/history-size"),
    # Ключи приложения Авито: PUT требует тела, DELETE и POST-проверка ходят в
    # Авито. Право `accounts:manage` заперто в tests/unit/test_avito_app.py.
    ("PUT", "/api/v1/avito/app"),
    ("DELETE", "/api/v1/avito/app"),
    ("POST", "/api/v1/avito/app/check"),
    # Переключатель «настоящий Авито / имитатор»: требует тела. Право
    # `accounts:manage` заперто в tests/unit/test_avito_app.py
    # (TestSwitchingOutOfTheImitator::test_only_admin_switches).
    ("POST", "/api/v1/avito/app/mode"),
    ("POST", "/api/v1/avito/connect-link"),
    # Подключение по ключам: требует тела, право `accounts:manage` заперто
    # в tests/unit/test_connect_by_keys.py.
    ("POST", "/api/v1/avito-accounts/connect"),
    # Ручное обновление токена канала (блок 8.3): `_format` подставляет
    # account_id="x", а ручка объявляет его как uuid — ALLOW-ветка упёрлась бы
    # в 422 на разборе пути. Право `accounts:manage` заперто в
    # tests/unit/test_avito_oauth.py::test_manual_token_refresh_is_admin_only.
    ("POST", "/api/v1/avito-accounts/{account_id}/refresh-token"),
    # Спринт 3. Все эти ручки требуют тела/состояния, поэтому ALLOW-ветка
    # матрицы упёрлась бы в 400 validation_error (или 404 на несуществующем
    # сообщении), а не в реальный отказ RBAC. Их RBAC заперт целевыми тестами:
    #   tests/unit/test_send_message.py, tests/unit/test_media.py,
    #   tests/unit/test_conversations_manage.py, tests/unit/test_templates.py,
    #   а ролевые сценарии head/observer — ниже в этом файле.
    ("POST", "/api/v1/conversations/{conversation_id}/messages"),
    ("POST", "/api/v1/conversations/{conversation_id}/notes"),
    ("POST", "/api/v1/messages/{message_id}/retry"),
    # «Снять неотправленное» (08.09): та же пара прав и та же причина попасть
    # сюда, что у повтора рядом — `_format` подставляет message_id="x", а ручка
    # объявляет его как uuid, и ALLOW-ветка упёрлась бы в разбор пути. Право
    # (`messages:send`, автор или админ) и отказ прочим заперты в
    # tests/unit/test_dismiss_failed_0809.py.
    ("POST", "/api/v1/messages/{message_id}/dismiss"),
    ("POST", "/api/v1/media"),
    ("PATCH", "/api/v1/conversations/{conversation_id}/status"),
    ("POST", "/api/v1/conversations/{conversation_id}/assign"),
    # «Забрать диалог у бота» (запрос владельца 29.08). Право то же, что у
    # отправки сообщения, — `messages:send`: кнопка заменяет собой путь «ответь,
    # чтобы бот замолчал», и требовать за неё больше прав не за что. В MATRIX не
    # держим по той же причине, что и `assign`: ALLOW-ветка упёрлась бы в
    # состояние seed-диалога, а не в отказ доступа. Право и поведение заперты в
    # tests/unit/test_take_from_bot.py.
    ("POST", "/api/v1/conversations/{conversation_id}/bot/mute"),
    ("POST", "/api/v1/templates"),
    ("PATCH", "/api/v1/templates/{template_id}"),
    ("DELETE", "/api/v1/templates/{template_id}"),
    # ⚠ Учёт применения (29.08). Как и у соседей выше, ALLOW-ветка матрицы
    # упёрлась бы в 404 на выдуманном идентификаторе, а не в отказ RBAC.
    # Настоящая проверка прав — в tests/unit/test_template_popularity_2908.py:
    # наблюдателю 403, менеджеру 204 на общей заготовке.
    ("POST", "/api/v1/templates/{template_id}/used"),
    # Спринт 4: асинхронный экспорт (01 §9.5) — POST требует тела, а ALLOW-ветка
    # завела бы реальную джобу ARQ. RBAC запирается тестами модуля статистики.
    ("POST", "/api/v1/stats/export"),
    ("GET", "/api/v1/stats/export/{job_id}"),
    # Спринт 6: боты (01 §8). Всем этим ручкам нужен существующий бот или живая
    # сессия песочницы, поэтому ALLOW-ветка матрицы упёрлась бы в 404/400, а не
    # в реальный отказ RBAC. Полная матрица «12 ручек × 3 роли = 403» живёт в
    # tests/integration/test_bots_api.py::test_every_bot_endpoint_is_admin_only,
    # `GET /bots` продублирован выше в MATRIX как страховка на SQLite-стеке.
    ("POST", "/api/v1/bots"),
    ("GET", "/api/v1/bots/{bot_id}"),
    ("PUT", "/api/v1/bots/{bot_id}"),
    ("DELETE", "/api/v1/bots/{bot_id}"),
    ("POST", "/api/v1/bots/{bot_id}/enable"),
    ("POST", "/api/v1/bots/{bot_id}/disable"),
    ("PUT", "/api/v1/bots/{bot_id}/accounts"),
    # Раздел лид-бота: изменяющим нужно тело, а `probe` и `test` ходят по
    # сети в чужой сервис — ALLOW-ветка матрицы упёрлась бы в таймаут, а не
    # в отказ RBAC. Право у всех одно и то же, проверяется в
    # tests/unit/test_leadbot_section.py.
    ("PATCH", "/api/v1/leadbot"),
    ("PATCH", "/api/v1/settings/trace"),
    ("POST", "/api/v1/settings/leads/token"),
    ("DELETE", "/api/v1/settings/leads/token"),
    ("PUT", "/api/v1/leadbot/connection"),
    ("POST", "/api/v1/leadbot/connection/reset"),
    ("POST", "/api/v1/leadbot/probe"),
    ("POST", "/api/v1/leadbot/test"),
    ("POST", "/api/v1/bots/sandbox/start"),
    ("POST", "/api/v1/bots/sandbox/{session_id}/message"),
    ("POST", "/api/v1/bots/sandbox/{session_id}/fire-timeout"),
    ("DELETE", "/api/v1/bots/sandbox/{session_id}"),
    # Центр уведомлений (14 §4). Роутер смонтирован в app/main.py; в MATRIX
    # ручек нет потому, что ALLOW-ветка на пустом ящике проверяла бы 200 на
    # пустом списке, а не отказ RBAC. Настоящая матрица «четыре роли × пять
    # ручек» заперта в tests/unit/test_notifications.py:
    #   test_observer_sees_nothing_at_all      — observer: 403 на всех пяти,
    #   test_anonymous_is_rejected             — без Bearer: 401 на всех пяти,
    #   test_system_notification_is_visible_only_to_admins,
    #   test_work_notification_reaches_head_and_admin_only,
    #   test_personal_notification_reaches_only_its_recipient — видимость по
    #   ролям (DESIGN §5.1): системное — админу, рабочее — руководителю,
    #   личное — адресату.
    ("GET", "/api/v1/notifications"),
    ("GET", "/api/v1/notifications/unread-count"),
    ("POST", "/api/v1/notifications/read-all"),
    ("POST", "/api/v1/notifications/{notification_id}/read"),
    ("POST", "/api/v1/notifications/{notification_id}/action"),
    # Спринт 7: управление сотрудниками (01 §3). ALLOW-ветка матрицы упёрлась бы
    # в 400 (пустое тело) или 404 (несуществующий user_id), а не в отказ RBAC.
    # Полная матрица «9 ручек × 4 роли» — 403 руководителю/менеджеру/наблюдателю,
    # 401 анониму, admin проходит везде — заперта в
    # tests/unit/test_users_admin.py::test_manage_endpoints_are_admin_only и
    # соседних тестах того же файла; `GET /users` продублирован выше в MATRIX
    # как страховка на общем стеке.
    ("POST", "/api/v1/users"),
    ("POST", "/api/v1/users/invite"),
    ("PATCH", "/api/v1/users/{user_id}"),
    ("POST", "/api/v1/users/{user_id}/invite"),
    ("POST", "/api/v1/users/{user_id}/resend-invite"),
    ("POST", "/api/v1/users/{user_id}/reset-password"),
    # Пароль сотрудника задаёт администратор напрямую и удаление сотрудника
    # (требование от 7 августа): первое требует тела, второе — существующего
    # user_id, поэтому ALLOW-ветка упёрлась бы в 400/404. Право `users:manage`
    # заперто в tests/unit/test_users_admin.py.
    ("POST", "/api/v1/users/{user_id}/set-password"),
    ("DELETE", "/api/v1/users/{user_id}"),
    ("POST", "/api/v1/users/{user_id}/deactivate"),
    ("POST", "/api/v1/users/{user_id}/activate"),
    # Форма «Написать администратору» (14 §2.2) доступна ЛЮБОЙ авторизованной
    # роли, включая observer, поэтому DENY-строки у неё нет вовсе, а ALLOW-ветка
    # требует валидного тела (пустое даёт 400). Все четыре роли получают 202 в
    # tests/unit/test_support.py::test_every_role_can_write_to_the_admin.
    ("POST", "/api/v1/support/message"),
    # Блок 7.1: очередь «Входящие» (15 §2.1). Действия очереди закрыты правом
    # `messages:send` — принимает диалоги тот, кто отвечает клиентам: admin и
    # manager; head получает `read_only_role`, observer — `forbidden`.
    # В MATRIX их держать нельзя: ALLOW-ветка второго же запроса упирается в
    # 409 «уже принят» (диалог занимает первая прошедшая роль), то есть
    # проверяла бы состояние диалога, а не отказ доступа — ровно причина, по
    # которой здесь же лежат `/assign` и `/status`.
    # Полная матрица «4 роли × 3 ручки» + 401 анониму — в
    # tests/unit/test_inbox_api.py::test_head_gets_read_only_role_on_every_queue_action
    # и соседних параметризованных тестах того же файла.
    # Таблица диалогов (план 7.3) — управленческий отчёт, право stats:all.
    ("GET", "/api/v1/conversations/table"),
    # Выгрузка той же таблицы: право обязано совпадать с экранным, иначе
    # закрытый отчёт утекает по прямой ссылке на CSV. С 24.09 файл собирает
    # воркер: POST ставит задачу, GET по job_id отдаёт статус автору, а прежний
    # GET отвечает 410 открытым до выкатки вкладкам
    # (tests/unit/test_dialogs_export_2409.py).
    ("GET", "/api/v1/conversations/table/export"),
    ("POST", "/api/v1/conversations/table/export"),
    ("GET", "/api/v1/conversations/table/export/{job_id}"),
    ("POST", "/api/v1/conversations/{conversation_id}/claim"),
    ("POST", "/api/v1/conversations/{conversation_id}/decline"),
    # Отмена отказа (UX-аудит, docs/17 §Т7) — то же право, что и у самого
    # отказа: забрать своё решение обратно может тот, кто мог его принять.
    ("POST", "/api/v1/conversations/{conversation_id}/decline/undo"),
    ("POST", "/api/v1/conversations/{conversation_id}/release"),
    # ЗДЕСЬ БЫЛИ ДВЕ РУЧКИ ОТЛОЖКИ (docs/38 §7) — `POST` и `DELETE .../snooze`.
    # Сняты 12 августа вместе со всей отложкой; исключение удалено вслед за
    # ними, иначе `test_exceptions_do_not_outlive_their_endpoints` краснеет —
    # и правильно делает: список исключений, переживший свою ручку, тихо
    # выводит из-под проверки следующую, которая случайно займёт тот же путь.
    # `GET /inbox` и `GET /inbox/count` переехали в MATRIX, как и было
    # задумано: роутер очереди монтируется в main.py, и обе ручки отвечают
    # честным 200 всем четырём ролям.
    # Блок 7.2: назначение операторов на каналы (15 §2.2). В MATRIX эти три
    # держать нельзя технически: `_format` подставляет `account_id="x"`, а
    # ручки объявляют его как uuid — ALLOW-ветка упёрлась бы в 422 на разборе
    # пути вместо ответа ручки (у `{user_id}` подстановки в `_format` нет
    # вовсе). Полная матрица «4 роли × 3 ручки» + 401 анониму заперта в
    # tests/unit/test_account_operators_api.py:
    #   test_reading_the_set_is_for_admin_and_head — GET: admin/head 200,
    #                                                manager/observer 403,
    #   test_changing_the_set_is_admin_only        — PUT: только admin,
    #   test_someone_elses_channels_need_accounts_read — чужие каналы под
    #                                                `accounts:read`, свои —
    #                                                любой роли,
    #   test_anonymous_is_rejected, test_channel_lists_reject_anonymous — 401.
    # `GET /me/channels` переехал в MATRIX выше: плейсхолдеров нет, и все
    # четыре роли получают честный 200.
    ("GET", "/api/v1/avito-accounts/{account_id}/operators"),
    ("PUT", "/api/v1/avito-accounts/{account_id}/operators"),
    ("GET", "/api/v1/users/{user_id}/accounts"),
}

CASES = [(m, p, role, exp) for m, p, perms in MATRIX for role, exp in perms.items()]


def _format(path: str, seed) -> str:
    # `message_id` — любой существующий: матрица проверяет ПРАВО, а не наличие
    # записи. Несуществующий дал бы 404 вперёд проверки роли, и ALLOW-ветка
    # перестала бы что-либо доказывать.
    return path.format(
        conversation_id=seed.conversation_id,
        message_id=getattr(seed, "message_id", "00000000-0000-0000-0000-000000000000"),
        token="x",
        account_id="x",
    )


def test_matrix_paths_are_all_mounted(app: FastAPI):
    """Обратная сторона guard'а полноты: строка матрицы про несмонтированный
    роутер молча ничего не проверяет. Спринт 4 закрыт — пропусков быть не может.
    """
    paths = app.openapi()["paths"]
    missing = sorted({p for _, p, _ in MATRIX} - set(paths))
    assert not missing, f"ручки из MATRIX не смонтированы приложением: {missing}"


@pytest.mark.parametrize(
    "method,path,role,expected", CASES, ids=[f"{m} {p} {r}={e}" for m, p, r, e in CASES]
)
async def test_rbac(app, client, tokens, seed_conversation, method, path, role, expected):
    url, headers = _format(path, seed_conversation), {"Authorization": f"Bearer {tokens[role]}"}

    if expected == ALLOW_PG:
        try:
            r = await client.request(method, url, headers=headers)
        except DatabaseError:
            # Ручка дошла до своего SQL и упала на диалекте SQLite (mv,
            # percentile_cont, jsonb — 06 §2/§3). Для матрицы это успех: до
            # запроса доходит только роль, прошедшая require_permission.
            # Успешный ответ этих ручек проверяется на Postgres (integration).
            return
        assert r.status_code not in (401, 403), r.text
        return

    r = await client.request(method, url, headers=headers)
    if expected == DENY:
        assert r.status_code == 403, r.text
    else:
        assert r.status_code < 400, r.text


@pytest.mark.parametrize("method,path", [(m, p) for m, p, _ in MATRIX])
async def test_rbac_endpoints_reject_anonymous(app, client, seed_conversation, method, path):
    r = await client.request(method, _format(path, seed_conversation))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_every_endpoint_is_in_matrix(app):
    """Guard (07 §1.1.3): an endpoint missing from MATRIX = red CI.

    Обходим OpenAPI-схему, а не app.routes: FastAPI ≥0.141 включает роутеры
    лениво (_IncludedRouter), и прямой обход маршрутов молча пропускал бы всё.
    """
    covered = {(m, p) for m, p, _ in MATRIX} | EXEMPT | COVERED_ELSEWHERE
    paths = app.openapi()["paths"]
    assert len(paths) >= 10, "OpenAPI подозрительно пуст — guard стал бы бесполезным"
    for path, methods in paths.items():
        for method in methods:
            if method.upper() in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            assert (method.upper(), path) in covered, (
                f"{method.upper()} {path} не покрыт RBAC-матрицей — "
                "добавьте строку в MATRIX или (для публичных ручек) в EXEMPT"
            )


def _mounted_endpoints(app: FastAPI) -> set[tuple[str, str]]:
    """Все ручки приложения парами «метод, путь» — включая скрытые от OpenAPI.

    Одной схемы мало: у возврата Авито (`GET /avito/callback`) и у страницы
    подключения по ссылке стоит `include_in_schema=False`, в схеме их нет, а в
    приложении они есть. Объявить их «протухшими» значило бы заставить убрать
    из списка исключений живые публичные ручки.

    Обхода `app.routes` тоже мало, и это тот самый случай, о котором
    предупреждает `test_every_endpoint_is_in_matrix`: FastAPI ≥0.141
    подключает роутеры лениво, и на верхнем уровне вместо ста с лишним
    маршрутов лежат три `_IncludedRouter`, до которых `isinstance(APIRoute)`
    не достаёт. Развёрнутый состав отдаёт их `effective_route_contexts()`;
    метод внутренний, поэтому вызывается через `getattr` — на версии, где его
    не станет, останется схема, и тест ослабнет, а не рассыплется.
    """
    mounted = {
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
        if method.upper() not in {"HEAD", "OPTIONS", "PARAMETERS"}
    }
    for route in app.routes:
        if isinstance(route, APIRoute):
            mounted |= {(m.upper(), route.path) for m in route.methods}
            continue
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts():
            mounted |= {(m.upper(), ctx.path) for m in (ctx.methods or ())}
    return {(m, p) for m, p in mounted if m not in {"HEAD", "OPTIONS"}}


def test_exceptions_do_not_outlive_their_endpoints(app: FastAPI):
    """Обратная сторона guard'а полноты — для СПИСКОВ ИСКЛЮЧЕНИЙ.

    Полнота проверялась только в одну сторону: «каждая живая ручка где-то
    описана». Для MATRIX обратную сторону закрывает
    ``test_matrix_paths_are_all_mounted``, а у EXEMPT и COVERED_ELSEWHERE её не
    было вовсе — и это заметили, когда удаляли разгрузку очереди (требование
    владельца от 11 августа, №8): две строки исключений пережили бы и свои
    ручки, и тест, на который ссылались, не уронив ничего.

    Чем это плохо. Исключение читается как обещание «RBAC этой ручки проверен
    вон там». Строка про несуществующий путь превращает список в набор
    обещаний, часть которых не о чем; а когда путь с тем же именем однажды
    заведут заново, он окажется заранее объявленным «покрытым в другом месте»
    и мимо матрицы прав проедет молча.
    """
    stale = sorted(f"{m} {p}" for m, p in (EXEMPT | COVERED_ELSEWHERE) - _mounted_endpoints(app))
    assert not stale, (
        "исключения RBAC ссылаются на ручки, которых в приложении нет: "
        f"{stale}. Ручку удалили — удалите и строку исключения"
    )


# --- сама матрица прав как закон (DESIGN §5.1, 01 §12) -----------------------
#
# Матрица endpoint'ов выше проверяет только то, для чего уже есть ручка, и
# только вопрос «пустили или нет». Но часть прав решает не доступ, а объём
# выдачи (`notes:read` — отдавать ли заметки, 01 §6.1) или ещё ждёт своих
# ручек (`users:manage`, `bots:manage`). Тихое расширение роли в
# ROLE_PERMISSIONS такая матрица не заметит — поэтому таблица 01 §12
# продублирована здесь построчно, как исполняемая копия закона.

PERMISSION_MATRIX: list[tuple[str, dict[str, bool]]] = [
    ("conversations:read", {A: True, H: True, M: True, O: True}),
    ("messages:send", {A: True, H: False, M: True, O: False}),
    ("conversations:manage", {A: True, H: True, M: True, O: False}),
    ("notes:read", {A: True, H: True, M: True, O: False}),
    ("notes:write", {A: True, H: True, M: True, O: False}),
    ("templates:own", {A: True, H: True, M: True, O: False}),
    ("templates:shared", {A: True, H: True, M: False, O: False}),
    ("stats:own", {A: True, H: True, M: True, O: False}),
    ("stats:all", {A: True, H: True, M: False, O: False}),
    ("bots:manage", {A: True, H: False, M: False, O: False}),
    ("accounts:read", {A: True, H: True, M: False, O: False}),
    ("accounts:manage", {A: True, H: False, M: False, O: False}),
    ("users:manage", {A: True, H: False, M: False, O: False}),
    ("audit:read", {A: True, H: True, M: False, O: False}),
    # Настройки работы команды: включение автораздачи и потолок диалогов.
    # Только администратор — включение меняет работу всей смены сразу.
    ("settings:manage", {A: True, H: False, M: False, O: False}),
    # Закрыть обращение, которое ещё НИКТО НЕ ВЗЯЛ (решение владельца 22.08).
    #
    # Диалог во «Входящих» закрытие убирает из очереди насовсем: её условие
    # требует `status != closed`. Один промах — и обращение, которому не
    # ответили, исчезает у всех, а клиент ждёт. Оператору для «я сейчас занят»
    # есть «Отклонить»: диалог уходит с его глаз на три минуты и возвращается
    # к коллегам.
    #
    # Только администратор: закрытие из очереди значит «с этим обращением
    # покончено», он разбирает спам и отвечает за то, что обращение не нужно.
    # Право отдельное от `conversations:manage` намеренно — вести свой диалог и
    # хоронить чужой, ещё не начатый, разные полномочия.
    ("conversations:close_queued", {A: True, H: False, M: False, O: False}),
    # ⚠ РАЗБОР ДИАЛОГОВ ОТКРЫТ ВСЕМ (решение владельца 27.08): «вкладку Разбор
    # диалогов нужно сделать доступной для всех, Менеджер и тд».
    #
    # Право отдельное от `stats:all` намеренно. Выдать менеджеру `stats:all`
    # означало бы открыть ему заодно Статистику по всем сотрудникам и Живую
    # ленту — надзорные экраны, которых он не просил. Здесь же таблица: те же
    # диалоги, что он и так ведёт, только построчно и со скоростью ответа.
    #
    # ⚠ НОВЫХ ДАННЫХ НЕ ОТКРЫВАЕТ (сверено 27.08): вкладка «Все» обычного
    # списка роль не фильтрует, телефон клиента отдаётся по `conversations:read`,
    # заметки в таблицу не попадают. Те же диалоги, другая форма.
    ("dialogs:read", {A: True, H: True, M: True, O: True}),
    # ⚠ ВЕРНУТЬ ПРИНЯТЫЙ ДИАЛОГ В ОЧЕРЕДЬ — ТОЛЬКО АДМИНИСТРАТОР (решение
    # владельца 28.08: «вернуть в очередь мог только бот или администратор, у
    # менеджеров эту функцию отключи и удали, чтобы её не было»).
    #
    # Раньше ручка висела на `messages:send`, то есть была у каждого, кто умеет
    # отвечать клиенту. Возврат снимает ответственного и отдаёт тринадцати
    # диалог, с которым человек уже поговорил: клиент получает второго
    # собеседника с нуля, а история разговора остаётся за прежним.
    #
    # Оператору для «я сейчас занят» есть «Отклонить» — оно про диалог, ЕЩЁ не
    # начатый, и возвращает его коллегам через три минуты.
    #
    # РУКОВОДИТЕЛЮ ТОЖЕ НЕТ, и это не оплошность: у него есть снятие
    # ответственного (`assign` с `assignee_id: null`) — то же по действию, но
    # над ЧУЖИМ диалогом и с другим намерением.
    #
    # Автоматики правило не касается: бот при передаче человеку и оба сторожа
    # зовут `inbox.return_to_queue` в сервисном слое, мимо HTTP и мимо прав.
    ("conversations:release", {A: True, H: False, M: False, O: False}),
]


def test_permission_catalog_matches_the_matrix():
    """Каталог 01 §12 — тот же список и в том же порядке (он едет в /auth/me)."""
    assert [perm for perm, _ in PERMISSION_MATRIX] == list(PERMISSIONS)


def test_roles_are_exactly_the_four_of_design_5_1():
    assert set(ROLES) == {A, H, M, O}
    assert set(ROLE_PERMISSIONS) == {A, H, M, O}


@pytest.mark.parametrize("role", [A, H, M, O])
def test_role_permissions_match_the_design_matrix(role: str):
    expected = {perm for perm, row in PERMISSION_MATRIX if row[role]}
    granted = set(ROLE_PERMISSIONS[role])
    assert granted == expected, (
        f"права роли {role} разошлись с DESIGN §5.1: "
        f"лишние {sorted(granted - expected)}, недостающие {sorted(expected - granted)}"
    )


@pytest.mark.parametrize("role", [A, H, M, O])
async def test_auth_me_reports_the_same_matrix(client, tokens, role: str):
    """Фронт роль→права не хардкодит: набор приходит из /auth/me (03 §5.1)."""
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens[role]}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == role
    assert body["permissions"] == [perm for perm, row in PERMISSION_MATRIX if row[role]]


# --- ролевые сценарии, которые матрица не выражает ---------------------------
#
# Матрица отвечает на вопрос «пустили или нет». Ниже — три места, где ошибка
# стоит утечки или сломанной роли: руководитель (пишет заметки, но не пишет
# клиенту), наблюдатель (не пишет ничего) и лента без заметок.


async def test_head_cannot_send_a_message_but_can_write_a_note(client, tokens, seed_conversation):
    """head: 403 read_only_role на отправку, 201 на заметку (DESIGN §5.1)."""
    conv_id = seed_conversation.conversation_id
    headers = {"Authorization": f"Bearer {tokens['head']}"}

    send = await client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        headers=headers,
        json={"text": "Здравствуйте!", "client_message_id": str(uuid.uuid4())},
    )
    assert send.status_code == 403, send.text
    error = send.json()["error"]
    # Код именно read_only_role: по нему фронт рисует плашку «Режим просмотра»
    # вместо composer'а (01 §12, 03 §5.3) — обычный forbidden сломал бы экран.
    assert error["code"] == "read_only_role"
    assert "Режим просмотра" in error["message"]

    note = await client.post(
        f"/api/v1/conversations/{conv_id}/notes",
        headers=headers,
        json={"text": "Клиент торгуется", "client_message_id": str(uuid.uuid4())},
    )
    assert note.status_code == 201, note.text
    assert note.json()["direction"] == "note"


async def test_head_can_change_status_and_reassign(
    client, tokens, users_by_role, seed_conversation, в_сети
):
    """Статус и передача — руководителю можно (DESIGN §5.1, 01 §5.4–5.5)."""
    conv_id = seed_conversation.conversation_id
    headers = {"Authorization": f"Bearer {tokens['head']}"}

    status = await client.patch(
        f"/api/v1/conversations/{conv_id}/status",
        headers=headers,
        json={"status": "in_progress"},
    )
    assert status.status_code == 200, status.text
    # head отвечать клиенту не может, поэтому «взять на себя» не должен —
    # диалог остаётся без ответственного до явного назначения.
    assert status.json()["assignee"] is None

    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(users_by_role["manager"])
    assign = await client.post(
        f"/api/v1/conversations/{conv_id}/assign",
        headers=headers,
        json={"assignee_id": str(users_by_role["manager"].id), "comment": "Возьми, пожалуйста"},
    )
    assert assign.status_code == 200, assign.text
    assert assign.json()["conversation"]["assignee"]["id"] == str(users_by_role["manager"].id)


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("POST", "/messages", {"text": "нельзя", "client_message_id": "cmid-1"}),
        ("POST", "/notes", {"text": "нельзя", "client_message_id": "cmid-2"}),
        ("PATCH", "/status", {"status": "closed"}),
        ("POST", "/assign", {"assignee_id": None}),
    ],
)
async def test_observer_is_denied_on_every_mutating_conversation_endpoint(
    client, tokens, seed_conversation, method, suffix, body
):
    r = await client.request(
        method,
        f"/api/v1/conversations/{seed_conversation.conversation_id}{suffix}",
        headers={"Authorization": f"Bearer {tokens['observer']}"},
        json=body,
    )
    assert r.status_code == 403, r.text
    # Именно forbidden: read_only_role — специализация для head, наблюдателю
    # плашку «назначьте менеджера» показывать не за что (01 §12).
    assert r.json()["error"]["code"] == "forbidden"


async def test_observer_feed_has_no_notes(client, tokens, seed_conversation):
    """Короткая страховка матрицы; подробности — tests/unit/test_observer_notes.py."""
    note = await client.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        headers={"Authorization": f"Bearer {tokens['manager']}"},
        json={"text": "секрет команды", "client_message_id": str(uuid.uuid4())},
    )
    assert note.status_code == 201, note.text

    feed = await client.get(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        headers={"Authorization": f"Bearer {tokens['observer']}"},
    )
    assert feed.status_code == 200, feed.text
    assert [m["direction"] for m in feed.json()["items"]] == ["in"]
    assert "секрет команды" not in feed.text
