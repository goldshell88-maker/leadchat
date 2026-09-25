"""Role → permissions mapping (01 §12; source of truth — DESIGN §5.1).

``require_permission("messages:send")`` in api/deps.py checks against this
mapping; the role is always read from the DB row, never trusted from the JWT.
"""

ROLES: tuple[str, ...] = ("admin", "head", "manager", "observer")

# Catalog order (01 §12) — used for stable `permissions` lists in /auth/me.
PERMISSIONS: tuple[str, ...] = (
    "conversations:read",
    "messages:send",
    "conversations:manage",
    "notes:read",
    "notes:write",
    "templates:own",
    "templates:shared",
    "stats:own",
    "stats:all",
    "bots:manage",
    "accounts:read",
    "accounts:manage",
    "users:manage",
    "audit:read",
    # Настройки работы команды (автораспределение). Право отдельное от
    # `users:manage` намеренно: заводить сотрудников и решать, как между ними
    # раздаются диалоги, — разные полномочия, и однажды их придётся развести
    # по разным людям.
    "settings:manage",
    # ⚠ ЗАКРЫТЬ ОБРАЩЕНИЕ, КОТОРОЕ ЕЩЁ НИКТО НЕ ВЗЯЛ (решение владельца 22.08).
    #
    # Диалог в очереди «Входящие» закрытие убирает НАСОВСЕМ: условие очереди
    # требует `status != closed`, а матрица переходов разрешает `new → closed`.
    # То есть один случайный «Закрыть» тихо уносит обращение, которому никто не
    # ответил, — а клиент ждёт.
    #
    # Оператору для «я сейчас занят» есть «Отклонить»: диалог уходит с глаз
    # того, кто нажал, на три минуты и возвращается в общую очередь. Закрытие
    # из очереди значит «с этим обращением покончено», и это решение
    # администратора: он разбирает спам и явный мусор и отвечает за то, что
    # обращение действительно не нужно.
    #
    # Право ОТДЕЛЬНОЕ от `conversations:manage` намеренно: вести свой диалог и
    # хоронить чужой, ещё не начатый, — разные полномочия.
    "conversations:close_queued",
    # ⚠ РАЗБОР ДИАЛОГОВ — ОТДЕЛЬНОЕ ПРАВО, И ЭТО РЕШЕНИЕ ВЛАДЕЛЬЦА 27.08:
    # «вкладку Разбор диалогов нужно сделать доступной для всех, Менеджер и тд».
    #
    # Раньше таблица висела на `stats:all`, то есть на праве статистики. Просто
    # выдать `stats:all` менеджеру было нельзя: вместе с разбором ему открылись
    # бы Статистика по всем сотрудникам и Живая лента — надзорные экраны,
    # которых он не просил. Поэтому у таблицы теперь своё право, и выдано оно
    # всем четырём ролям.
    #
    # ⚠ НОВЫХ ДАННЫХ ЭТО НЕ ОТКРЫВАЕТ — проверено 27.08 по трём дверям.
    # Сначала я записал обратное («расширение видимости»), а сверка показала:
    # вкладка «Все» обычного списка роль не фильтрует вовсе (`_tab_condition`
    # → `sa.true()`), карточка диалога отдаёт телефон по `conversations:read`,
    # и то и другое есть у менеджера с наблюдателем давно. Таблица показывает
    # ТЕ ЖЕ диалоги в другой форме — построчно и со скоростью ответа.
    # Заметки в неё не попадают: поиск ограничен `direction in ('in','out')`,
    # а лента и WS отсекают их по `notes:read` (tests/unit/test_observer_notes.py).
    "dialogs:read",
    # ⚠ ВЕРНУТЬ ПРИНЯТЫЙ ДИАЛОГ В ОЧЕРЕДЬ (решение владельца 28.08 дословно:
    # «сделай, чтобы вернуть в очередь мог только бот или администратор, у
    # менеджеров эту функцию отключи и удали, чтобы её не было»).
    #
    # Раньше кнопка висела на `messages:send` — то есть была у КАЖДОГО, кто
    # умеет отвечать клиенту. Возврат снимает ответственного и отдаёт диалог,
    # с которым человек уже поговорил, обратно тринадцати: клиент получает
    # второго собеседника с нуля, а история разговора остаётся за прежним.
    #
    # У оператора для «я сейчас занят» есть «Отклонить» — оно про диалог, ЕЩЁ
    # не начатый, и возвращает его коллегам через три минуты. Это разные вещи,
    # и одну не стоит делать тем же жестом, что другую.
    #
    # АВТОМАТИКИ ЭТО НЕ КАСАЕТСЯ, И ЭТО ВАЖНО. Бот при передаче человеку,
    # сторож возврата розданного (7.7) и сторож «клиент ждёт» зовут
    # `inbox.return_to_queue` В СЕРВИСНОМ СЛОЕ, мимо HTTP и мимо прав. Правило
    # запирает ручку, а не саму операцию, — иначе вместе с кнопкой встали бы и
    # все три сторожа.
    "conversations:release",
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(PERMISSIONS),
    "head": frozenset(
        {
            "conversations:read",
            "conversations:manage",
            "notes:read",
            "notes:write",
            "templates:own",
            "templates:shared",
            "stats:own",
            "stats:all",
            "accounts:read",
            "audit:read",
            "dialogs:read",
        }
    ),
    "manager": frozenset(
        {
            "conversations:read",
            "messages:send",
            "conversations:manage",
            "notes:read",
            "notes:write",
            "templates:own",
            "stats:own",
            "dialogs:read",
        }
    ),
    "observer": frozenset({"conversations:read", "dialogs:read"}),
}


def permissions_for(role: str) -> list[str]:
    """Permissions of a role in stable catalog order (for /auth/me)."""
    granted = ROLE_PERMISSIONS.get(role, frozenset())
    return [p for p in PERMISSIONS if p in granted]
