"""Таблица PAIRS — построчный перенос из src/test/tokenContrast.test.ts."""
from _engine import TEXT, UI

PAIRS = [
    # текст на поверхностях
    ("--lc-text-1", "--lc-bg-0", TEXT, "основной текст на фоне приложения"),
    ("--lc-text-1", "--lc-bg-1", TEXT, "основной текст на панели"),
    ("--lc-text-1", "--lc-surface", TEXT, "основной текст на карточке"),
    ("--lc-text-1", "--lc-surface-hover", TEXT, "основной текст на наведении"),
    ("--lc-text-1", "--lc-selected", TEXT, "основной текст на выбранной строке"),
    ("--lc-text-2", "--lc-bg-1", TEXT, "вторичный текст на панели"),
    ("--lc-text-2", "--lc-surface", TEXT, "вторичный текст на карточке"),
    ("--lc-text-2", "--lc-surface-hover", TEXT, "вторичный текст на наведении"),
    ("--lc-text-2", "--lc-selected", TEXT, "вторичный текст на выбранной строке"),
    ("--lc-text-3", "--lc-bg-0", TEXT, "приглушённый текст на фоне приложения"),
    ("--lc-text-3", "--lc-bg-1", TEXT, "приглушённый текст на панели"),
    ("--lc-text-3", "--lc-surface", TEXT, "приглушённый текст на карточке"),
    ("--lc-text-3", "--lc-surface-hover", TEXT, "приглушённый текст на наведении"),
    ("--lc-text-3", "--lc-selected", TEXT, "приглушённый текст на выбранной строке"),
    ("--lc-text-4", "--lc-bg-1", UI, "булавка закрепления на панели"),
    ("--lc-text-4", "--lc-selected", UI, "булавка на выбранной строке"),
    # чернила на заливках
    ("--lc-on-primary", "--lc-primary-solid", TEXT, "подпись на главной кнопке"),
    ("--lc-on-primary", "--lc-primary-solid-hover", TEXT, "главная кнопка под курсором"),
    ("--lc-on-primary", "--lc-primary-solid-active", TEXT, "главная кнопка нажата"),
    ("--lc-on-success", "--lc-success-solid", TEXT, "подпись на кнопке подтверждения"),
    ("--lc-on-success", "--lc-success-solid-hover", TEXT, "кнопка подтверждения под курсором"),
    ("--lc-on-danger", "--lc-danger-solid", TEXT, "бейдж очереди, счётчик на колокольчике"),
    ("--lc-on-danger", "--lc-danger-solid-hover", TEXT, "красная кнопка под курсором"),
    ("--lc-on-warning", "--lc-warning-solid", TEXT, "кнопка отправки в режиме заметки"),
    ("--lc-on-warn", "--lc-warn", TEXT, "минуты ожидания в залитой ячейке"),
    ("--lc-on-accent", "--lc-accent-solid", TEXT, "значок бота в строке диалога"),
    ("--lc-on-brand", "--lc-brand", TEXT, "монограмма знака Lead Partner"),
    ("--lc-unread-text", "--lc-unread-bg", TEXT, "число непрочитанных"),
    # подложки subtle
    ("--lc-primary-text", "--lc-primary-subtle", TEXT, "текст на акцентной подложке"),
    ("--lc-success-text", "--lc-success-subtle", TEXT, "текст на подложке успеха"),
    ("--lc-danger-text", "--lc-danger-subtle", TEXT, "плашка критичного, бейдж важности"),
    ("--lc-warning-text", "--lc-warning-subtle", TEXT, "полоса «связь просела», бейдж важности"),
    ("--lc-info-text", "--lc-info-subtle", TEXT, "информационная плашка"),
    ("--lc-text-2", "--lc-danger-subtle", TEXT, "тело плашки критичного"),
    ("--lc-text-2", "--lc-bg-2", TEXT, "текст на утопленном блоке"),
    ("--lc-text-3", "--lc-bg-2", TEXT, "чип важности «к сведению»"),
    ("--lc-accent-text", "--lc-accent-subtle", TEXT, "подпись бота"),
    # текст акцентом и ссылки
    ("--lc-primary-text", "--lc-bg-1", TEXT, "текст акцентом на панели"),
    ("--lc-primary-text", "--lc-surface", TEXT, "текст акцентом на карточке"),
    ("--lc-link", "--lc-bg-1", TEXT, "ссылка на панели"),
    ("--lc-link", "--lc-surface", TEXT, "ссылка на карточке"),
    ("--lc-danger-text", "--lc-bg-1", TEXT, "текст ошибки на панели"),
    ("--lc-danger-text", "--lc-surface", TEXT, "подпись под полем с ошибкой"),
    ("--lc-warn-text", "--lc-bg-1", TEXT, "минуты ожидания в списке"),
    ("--lc-warn-text", "--lc-surface-hover", TEXT, "минуты ожидания под курсором"),
    ("--lc-warn-text", "--lc-selected", TEXT, "минуты ожидания в выбранной строке"),
    # лента сообщений
    ("--lc-bubble-out-text", "--lc-bubble-out", TEXT, "текст исходящего сообщения"),
    ("--lc-bubble-out-meta", "--lc-bubble-out", TEXT, "подпись оператора и время в пузыре"),
    ("--lc-bubble-out-danger", "--lc-bubble-out", TEXT, "пометка «не доставлено»"),
    ("--lc-bubble-in-text", "--lc-bubble-in", TEXT, "текст входящего сообщения"),
    ("--lc-bubble-bot-text", "--lc-bubble-bot", TEXT, "сообщение бота"),
    ("--lc-bubble-note-text", "--lc-bubble-note", TEXT, "внутренняя заметка"),
    ("--lc-bubble-system-text", "--lc-bg-2", TEXT, "системное сообщение в ленте"),
    # статусы диалога
    ("--lc-status-new-text", "--lc-status-new-bg", TEXT, "чип «новый»"),
    ("--lc-status-inprogress-text", "--lc-status-inprogress-bg", TEXT, "чип «в работе»"),
    ("--lc-status-closed-text", "--lc-status-closed-bg", TEXT, "чип «закрыт»"),
    # элементы интерфейса
    ("--lc-primary", "--lc-bg-1", UI, "рамки и иконки акцентом на панели"),
    ("--lc-primary", "--lc-selected", UI, "рамка выбранной строки списка"),
    ("--lc-focus-border", "--lc-bg-1", UI, "кольцо фокуса на панели"),
    ("--lc-focus-border", "--lc-surface", UI, "кольцо фокуса на карточке"),
    ("--lc-danger", "--lc-bg-1", UI, "рамка поля с ошибкой"),
    ("--lc-danger", "--lc-surface", UI, "рамка карточки в отказе"),
    ("--lc-warning", "--lc-bg-1", UI, "значок «передан вам» на панели"),
    ("--lc-warning", "--lc-surface-hover", UI, "значок «передан вам» под курсором"),
    ("--lc-warning", "--lc-selected", UI, "значок «передан вам» в выбранной строке"),
    ("--lc-presence-online", "--lc-bg-1", UI, "точка «в сети» в шапке"),
    ("--lc-presence-away", "--lc-bg-1", UI, "точка «отошёл»"),
    ("--lc-presence-offline", "--lc-bg-1", UI, "точка «нет соединения» в шапке"),
    ("--lc-presence-offline", "--lc-surface", UI, "точка «нет соединения» в карточке клиента"),
    ("--lc-empty-art", "--lc-bg-1", UI, "иллюстрация пустого состояния"),
    ("--lc-empty-art", "--lc-surface", UI, "иллюстрация пустого состояния на карточке"),
    ("--lc-chart-1", "--lc-surface", UI, "столбцы и линия графика"),
    ("--lc-heat-5", "--lc-surface", UI, "верхняя ступень тепловой карты"),
    ("--lc-accent", "--lc-surface", UI, "рамка пузыря бота"),
]

# Лестница поверхностей — не входит в PAIRS, но валится тем же файлом.
LADDER = [
    ("--lc-surface", "--lc-bg-1", "карточка над панелью"),
    ("--lc-surface-hover", "--lc-bg-0", "наведение над фоном"),
    ("--lc-selected", "--lc-bg-0", "выбранное над фоном"),
    ("--lc-selected", "--lc-surface-hover", "выбранное над наведением"),
    ("--lc-border", "--lc-surface", "рамка на карточке"),
    ("--lc-bubble-in", "--lc-bg-0", "входящий пузырь над фоном ленты"),
]

SAME_FORBIDDEN = [
    ("--lc-selected", "--lc-surface-hover"),
    ("--lc-bubble-in", "--lc-bg-1"),
    ("--lc-surface", "--lc-bg-1"),
    ("--lc-surface-hover", "--lc-bg-1"),
]

# Роли, от которых акцент обязан отличаться (constraint 3 задания).
ROLE_RIVALS = ["--lc-link", "--lc-info", "--lc-accent", "--lc-warning", "--lc-danger"]
