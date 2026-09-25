"""Входящее без тела и без вложений не сохраняется НИКОГДА (правка 8).

ЧТО СЛУЧИЛОСЬ НА БОЕВОЙ. Сообщение ``e0000000-0000-4000-8000-000000000811``
(11 августа, 12:14 UTC) приехало в базу как ``direction=in``,
``sender_type=client``, ``body=null``, ``attachments=[]``. Запись есть,
показать нечего. Лента рисовала ей «…», превью строки списка — «Вложение»:
два разных ответа об одном сообщении, и оба неправда.

КОРЕНЬ ТОТ ЖЕ, ЧТО У ВЧЕРАШНЕЙ БЕДЫ СО СКЛЕЙКОЙ КЛИЕНТОВ: 11 августа включили
загрузку ВСЕЙ истории, и она принесла виды сообщений, которых разбор истории
не знает. У таких сообщений ``content`` приходит пустым (или его нет вовсе) —
ни текста, ни ключа, из которого собирается вложение. Разбор молча отдавал
пустое событие, а ``_insert_message_idempotent`` так же молча его сохранял.

ЧТО ПРОВЕРЯЕТСЯ НИЖЕ. Не «мы знаем все виды Авито» — перечня видов у нас нет
и выдумывать его запрещено (docs/30 §«Чего мы не знаем»). Проверяется ровно
одно свойство: из разбора НЕ ВЫХОДИТ событие, у которого пусто и тело, и
вложения. Знакомый вид получает человеческую подпись, незнакомый — честное
«откройте диалог в Авито» и строку предупреждения в журнал, по которой видно,
какой вид и какое сообщение.
"""

import pathlib
import re
from typing import Any

import pytest
import structlog

from app.integrations.avito.adapter import (
    UNSUPPORTED_MESSAGE_TEXT,
    AvitoAdapter,
    ChatInfo,
)

ACCOUNT_UID = 770200
CLIENT_UID = 999201
CREATED = 1754914440  # 11 августа 2026, 12:14 UTC — время боевого случая


def _chat() -> ChatInfo:
    return ChatInfo(
        external_chat_id="u2i-AbCd123",
        client_external_id=str(CLIENT_UID),
        client_name="Клиент",
        item_title="Ремонт стиральных машин",
        item_url="https://www.avito.ru/bryansk/predlozheniya_uslug/remont-1",
        item_price=None,
        has_unread=False,
        unread_count=None,
        last_message_at=None,
    )


def _history(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "e0000000-0000-4000-8000-000000000811",
        "author_id": CLIENT_UID,
        "created": CREATED,
    }
    raw.update(over)
    return raw


def _normalize(**over: Any):
    return AvitoAdapter.normalize_history_message(
        _history(**over), chat=_chat(), account_user_id=ACCOUNT_UID
    )


def подпись(событие: Any) -> str | None:
    """Слово, которым названо непоказуемое содержимое.

    ⚠ ЧИТАЕМ ВЛОЖЕНИЕ, А НЕ ТЕЛО (правка 07.09, жалоба владельца «такое
    ощущение что клиент сам пишет»). До 07.09 подпись лежала в `text` — в том
    же поле, куда попадают слова клиента, — и выглядела его репликой во всех
    восьми местах, которые это поле читают. Теперь она лежит вложением без
    ссылки: структурный признак, который каждый читатель уже умеет обходить.
    Утверждения тестов не изменились, изменилось место, откуда берётся ответ.

    ⚠ ЗВОНОК СЮДА НЕ ОТНОСИТСЯ. `appCall` остаётся ТЕЛОМ серой записи Авито
    (`direction='system'`), и его тесты по-прежнему читают `text`: серый чип
    вложений не рисует вовсе, а бот считает звонки по слову в теле.
    """
    for a in событие.attachments or []:
        имя = a.get("name")
        if isinstance(имя, str) and имя:
            return имя
    return None


def _webhook(**value_over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "e0000000-0000-4000-8000-000000000811",
        "chat_id": "u2i-AbCd123",
        "user_id": ACCOUNT_UID,
        "author_id": CLIENT_UID,
        "created": CREATED,
    }
    value.update(value_over)
    return {"id": "env-1", "version": "v3.0.0", "payload": {"type": "message", "value": value}}


# ------------------------------------------------------------ сам боевой случай


def test_history_message_without_content_is_never_stored_empty() -> None:
    """Ровно то сообщение, что нашли на боевой: вид есть, содержимого нет."""
    event = _normalize(type="video", content={})

    assert подпись(event), "запись без тела И без вложений сохранять нельзя"
    # ⚠ ТЕКСТ ИЗМЕНЁН 02.09 ПО ЗАМЕРУ, А НЕ ПО ВКУСУ. Голое «Видео» — слово
    # без действия: файла нет, а что делать, не сказано. Все 106 боевых видео
    # приходят с пустым содержимым, дозагрузить неоткуда (у голосовых метод
    # есть, у видео нет), поэтому единственное честное — назвать место, где
    # его посмотреть.
    assert подпись(event) == "Видео — посмотреть можно только в приложении Авито"
    # ⚠ И ВЛОЖЕНИЕ РОВНО ОДНО, БЕЗ ССЫЛКИ (07.09). Здесь стояло
    # `attachments == []` — тогда подпись жила телом. Теперь пустой список
    # означал бы, что запись сохранена без единого следа: ни слов, ни
    # вложения, то есть та самая беда 11 августа. Ссылки при этом нет и быть
    # не может: Авито по видео не присылает ни адреса, ни идентификатора
    # (152 сырых сообщения из 152 с пустым содержимым).
    assert len(event.attachments) == 1
    assert event.attachments[0]["url"] is None
    assert event.text is None, "подпись не должна лежать в теле И во вложении сразу"


def test_history_message_without_content_key_at_all() -> None:
    """``content`` может не приехать вовсе — это тот же случай."""
    event = _normalize(type="image")

    assert подпись(event) == "Фотография"


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("image", "Фотография"),
        ("voice", "Голосовое сообщение"),
        # ⚠ ФАЙЛ ПЕРЕЕХАЛ В `_CONTENT_FREE_KINDS` 07.09, И ПОДПИСЬ У НЕГО ДРУГАЯ.
        # Замер: content пуст 22 из 22 за месяц, метода за файлом у Авито нет
        # (три независимые проверки). Голое «Файл» было словом без действия —
        # теперь названо единственное, что сработает. Заодно исчезла ложная
        # тревога «знакомый вид без содержимого» на каждый файл.
        ("file", "Файл — открыть можно только в приложении Авито"),
        # ⚠ ВИДЕО ОТСЮДА УБРАНО 02.09 И ПЕРЕЕХАЛО В `_CONTENT_FREE_KINDS`.
        # Замер боя: все 106 сообщений вида `video` приходят с пустым
        # содержимым, то есть пустота у них ШТАТНАЯ, а не пробел разбора. Здесь
        # оно давало ложное предупреждение о поломке на каждое видео и голое
        # слово «Видео» без действия. Проверяется теперь соседним тестом.
    ],
)
def test_known_kind_gets_human_label(source_type: str, expected: str) -> None:
    """Знакомый вид без содержимого называется словом, которое поймёт диспетчер.

    Подписи берутся из общего словаря ``_ATTACHMENT_LABELS`` — того же, каким
    подписаны вложения. Двух названий у одного вида быть не должно: оператор
    не обязан догадываться, что «Голосовое» и «Аудио» — одно и то же.
    """
    assert подпись(_normalize(type=source_type, content={})) == expected


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("location", "Геопозиция"),
        ("link", "Ссылка"),
        ("call", "Звонок"),
        ("item", "Объявление"),
    ],
)
def test_system_kind_keeps_the_label_in_the_body(source_type: str, expected: str) -> None:
    """Эти четыре вида на дороге ИСТОРИИ считаются служебными и остаются ТЕЛОМ.

    ⚠ ПОЧЕМУ НЕ ВЛОЖЕНИЕМ, КАК ВИДЕО И ФАЙЛ (разбор 07.09). Служебная запись
    рисуется серым чипом (`direction='system'`), а он показывает одну строку
    текста и вложений не рисует вовсе. Переезд подписи во вложение оставил бы
    на экране пустой чип — ровно та беда 11 августа, ради которой подпись и
    завели, только в новом месте.

    Жалоба владельца («такое ощущение что клиент сам пишет») к ним и не
    относится: у серого чипа своя подпись «Сообщение Авито», спутать его с
    репликой человека нельзя.
    """
    assert _normalize(type=source_type, content={}).text == expected


def test_unknown_kind_says_the_truth_instead_of_guessing() -> None:
    """Незнакомый вид не выдумываем, но НАЗЫВАЕМ — правка 19.08 по просьбе владельца.

    Голое «неподдерживаемого вида» не отвечало на вопрос «а что там?»: оператор
    не знал, стоит ли открывать Авито ради стикера или там фотография поломки.
    Слово вида берётся у самого Авито и на русский НЕ переводится — выдуманная
    подпись хуже отсутствующей.
    """
    event = _normalize(type="sticker", content={})

    имя = подпись(event) or ""
    assert имя.startswith(UNSUPPORTED_MESSAGE_TEXT)
    assert "sticker" in имя, "вид обязан быть назван человеку, а не только в журнале"
    assert event.source_type == "sticker", "вид обязан доехать до записи как есть"


def test_message_with_no_kind_at_all_is_still_readable() -> None:
    """Вида нет, текста нет, вложений нет — всё равно не пустая строка."""
    assert подпись(_normalize()) == UNSUPPORTED_MESSAGE_TEXT


def test_blank_text_counts_as_empty() -> None:
    """Пробелы вместо текста — та же пустота: ``body`` из пробелов даёт «…»."""
    assert (подпись(_normalize(type="sticker", content={"text": "   "})) or "").startswith(
        UNSUPPORTED_MESSAGE_TEXT
    )


def test_unknown_kind_is_logged_with_kind_and_message_id() -> None:
    """Про незнакомый вид обязан остаться след с видом И идентификатором.

    Иначе разбирать накопившееся не по чему: сообщение уже показано словами
    «откройте диалог в Авито», и понять, ЧТО именно мы не умеем показывать,
    можно только из журнала.
    """
    with structlog.testing.capture_logs() as logs:
        _normalize(type="sticker", content={})

    warnings = [entry for entry in logs if entry["event"] == "avito.unsupported_message_kind"]
    assert len(warnings) == 1
    (warning,) = warnings
    assert warning["log_level"] == "warning"
    assert warning["source_type"] == "sticker"
    assert warning["message_id"] == "e0000000-0000-4000-8000-000000000811"
    assert warning["chat_id"] == "u2i-AbCd123"


# ------------------------------------------- содержимое важнее подписи-заглушки


def test_real_text_is_never_replaced() -> None:
    """Обычная переписка проходит нетронутой — подпись только вместо пустоты."""
    event = _normalize(type="text", content={"text": "Здравствуйте, когда мастер?"})

    assert event.text == "Здравствуйте, когда мастер?"


def test_attachment_speaks_for_itself() -> None:
    """Есть вложение — тело не подменяем.

    Голосовое приходит одним идентификатором, без текста. Строка «Голосовое
    сообщение» уже нарисована самим вложением; продублировать её в теле значит
    показать её в пузыре дважды.
    """
    event = _normalize(type="voice", content={"voice": {"voice_id": "v-777"}})

    assert event.text is None
    assert [a["name"] for a in event.attachments] == ["Голосовое сообщение"]


def test_photo_with_caption_keeps_both() -> None:
    """«Вот такая деталь» + фото — самое частое сообщение в ремонте техники."""
    event = _normalize(
        type="image",
        content={"text": "вот такая деталь", "image": {"sizes": {"640x480": "https://a/b.jpg"}}},
    )

    assert event.text == "вот такая деталь"
    assert len(event.attachments) == 1


# ------------------------------------------------------------------ вебхук тоже


def test_webhook_message_without_content_is_never_empty() -> None:
    """Живой вебхук с нетекстовым видом — тот же корень, та же защита.

    История и вебхук приходят из одного API и одними и теми же видами;
    починить одну дорогу и оставить вторую значит получить ту же беду завтра,
    когда клиент пришлёт голосовое не в историю, а вживую.
    """
    event = AvitoAdapter.parse_webhook(_webhook(type="voice", content={}))

    assert подпись(event) == "Голосовое сообщение"


def test_webhook_unknown_kind_is_honest() -> None:
    event = AvitoAdapter.parse_webhook(_webhook(type="sticker", content={}))

    имя = подпись(event) or ""
    assert имя.startswith(UNSUPPORTED_MESSAGE_TEXT)
    assert "sticker" in имя, "живой путь тоже обязан называть вид"


# --------------------------------------------------------------- общее свойство


EMPTYISH_FORMS: list[dict[str, Any]] = [
    {},
    {"type": "text"},
    {"type": "text", "content": {}},
    {"type": "text", "content": {"text": ""}},
    {"type": "text", "content": {"text": None}},
    {"type": "", "content": {}},
    {"type": "image", "content": {}},
    {"type": "image", "content": {"image": None}},
    {"type": "voice", "content": {"voice": None}},
    {"type": "deleted", "content": {}},
    {"type": "system", "content": {}},
    {"type": "appCall", "content": {}},
    {"type": "sticker", "content": {}},
    {"type": "какой-то-новый-вид", "content": {"неведомое": None}},
    {"content": None},
    {"content": []},
]


@pytest.mark.parametrize("form", EMPTYISH_FORMS)
def test_no_form_ever_produces_a_blank_record(form: dict[str, Any]) -> None:
    """Сводная проверка ровно того, на что жаловался заказчик.

    Ни одна форма — знакомая, кривая или ещё не виданная — не имеет права дать
    событие, у которого пусто и тело, и вложения. Такая запись показывается
    «…» в ленте и «Вложение» в списке, то есть врёт дважды и по-разному.
    """
    event = _normalize(**form)

    assert (event.text and event.text.strip()) or event.attachments, (
        f"форма {form!r} дала запись без тела и без вложений"
    )


@pytest.mark.parametrize("form", EMPTYISH_FORMS)
def test_webhook_never_produces_a_blank_record(form: dict[str, Any]) -> None:
    event = AvitoAdapter.parse_webhook(_webhook(**form))

    assert (event.text and event.text.strip()) or event.attachments, (
        f"форма {form!r} дала запись без тела и без вложений"
    )


# ------------------------------------------------ одна фраза на сервер и фронт


_PREVIEW_TS = (
    pathlib.Path(__file__).resolve().parents[2] / "frontend/src/shared/lib/messagePreview.ts"
)


def test_unsupported_text_matches_frontend() -> None:
    """Фраза на сервере и на фронте — ОДНА, буква в букву.

    Фронт держит её у себя не от лени: записи, накопленные до 12 августа,
    лежат в базе с `body=null`, и лента обязана называть их теми же словами,
    какими сервер называет новые. Разъедься эти две строки — вернётся ровно та
    беда, ради которой правка делалась: два разных ответа об одном сообщении.
    """
    source = _PREVIEW_TS.read_text(encoding="utf-8")
    match = re.search(r'UNSUPPORTED_MESSAGE_TEXT\s*=\s*"([^"]+)"', source)

    assert match, f"в {_PREVIEW_TS.name} не нашлась строка UNSUPPORTED_MESSAGE_TEXT"
    assert match.group(1) == UNSUPPORTED_MESSAGE_TEXT


# --- звонок через приложение (замер боя 15 августа) ---------------------------


def test_app_call_is_named_a_call_not_an_unsupported_kind() -> None:
    """`appCall` — клиент ЗВОНИЛ, и лента обязана говорить именно это.

    ⚠ ЦЕНА СТАРОГО ПОВЕДЕНИЯ ИЗМЕРЕНА НА БОЮ: из 30 «сообщений
    неподдерживаемого вида» 14 оказались звонками клиентов через приложение
    Авито. Самое горячее действие клиента — он не написал, он ПОЗВОНИЛ —
    лежало под вывеской «откройте диалог в Авито», и диспетчер не перезванивал,
    потому что не знал, что был звонок.
    """
    event = _normalize(type="appCall", content={})

    assert event.text == "Клиент звонил через приложение Авито"
    assert event.source_type == "appCall"


def test_app_call_arrives_calm_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Пустое содержимое у звонка — норма вебхука, а не пробел разбора.

    Сырцы боя: `content: {}` у всех четырнадцати. Предупреждение на каждый
    звонок было бы ложной тревогой, которая учит не читать журнал, — ровно та
    беда, которую разбирали у сторожа уведомлений.
    """
    with structlog.testing.capture_logs() as logs:
        _normalize(type="appCall", content={})
    tail = [x for x in logs if "unsupported" in str(x) or "without_content" in str(x)]
    assert not tail


def test_app_call_is_not_hidden_by_the_system_toggle() -> None:
    """Звонок не прячется выключателем служебных записей Авито.

    Выключатель («в самой Jivo их нет») гасит ШУМ Авито: приветствия
    ассистента, «пользователь создал чат». Звонок — действие клиента, и
    спрятать его — потерять обращение. Ловушка конкретная: начни подпись со
    служебного префикса — и фильтр ленты съел бы её вместе с шумом.
    """
    from app.services.conversations import AVITO_SYSTEM_PREFIX

    event = _normalize(type="appCall", content={})
    assert event.text is not None
    assert not event.text.startswith(AVITO_SYSTEM_PREFIX)


# --- фото клиента из карточки чата (просьба владельца, снимки Jivo) -----------


def _chat_raw(*, users: list) -> dict:
    return {"id": "u2i-x", "users": users}


def test_the_peer_avatar_is_taken_from_the_chat_card() -> None:
    """Аватар собеседника достаётся из `public_user_profile.avatar`.

    Форма проверена зондом на бою 15 августа: `images` — словарь
    «размер → ссылка» плюс `default`. Берём 128×128 — карточке хватает.
    """
    info = AvitoAdapter.parse_chat(
        _chat_raw(
            users=[
                {"id": 770200, "name": "Мы"},
                {
                    "id": 999201,
                    "name": "Инна",
                    "public_user_profile": {
                        "avatar": {
                            "default": "https://cdn/256.png",
                            "images": {"128x128": "https://cdn/128.png"},
                        }
                    },
                },
            ]
        ),
        account_user_id=770200,
    )
    assert info.client_avatar_url == "https://cdn/128.png"
    assert info.client_name == "Инна"


def test_without_the_wanted_size_the_default_is_good_enough() -> None:
    info = AvitoAdapter.parse_chat(
        _chat_raw(
            users=[
                {"id": 770200},
                {"id": 999201, "public_user_profile": {"avatar": {"default": "https://cdn/d.png"}}},
            ]
        ),
        account_user_id=770200,
    )
    assert info.client_avatar_url == "https://cdn/d.png"


def test_a_profile_of_any_broken_shape_never_breaks_the_chat_parse() -> None:
    """Аватар — самое необязательное поле чата: любая кривая форма → None.

    Гарантий формы у API нет (docs/30 §«Чего мы не знаем»), а разбор чата не
    имеет права упасть из-за картинки: за ним стоят имя, объявление и
    раскрытие заглушек.
    """
    for profile in (
        None,
        "строка",
        42,
        {},
        {"avatar": None},
        {"avatar": "x"},
        {"avatar": {"images": "не словарь"}},
        {"avatar": {"images": {}}},
    ):
        info = AvitoAdapter.parse_chat(
            _chat_raw(users=[{"id": 770200}, {"id": 999201, "public_user_profile": profile}]),
            account_user_id=770200,
        )
        assert info.client_avatar_url is None, f"форма {profile!r} должна давать None"


def test_голосовое_несёт_идентификатор_записи() -> None:
    """У голосового обязан быть идентификатор — без него запись не спросить.

    ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «не грузятся голосовые сообщения». Авито присылает
    голосовое ОДНИМ идентификатором, самой записи в сообщении нет: за ней надо
    идти отдельным запросом (`getVoiceFiles`). Пока запроса не было, оператор
    видел строку «Голосовое сообщение» и послушать её не мог — клиент говорит,
    а мы не слышим. На бою таких сообщений 174.

    Тест держит звено, без которого вся цепочка бессмысленна: вид вложения
    доезжает как `voice`, а идентификатор — в `media_id`.
    """
    event = _normalize(type="voice", content={"voice": {"voice_id": "2229d5a7-072e"}})

    (вложение,) = event.attachments
    assert вложение["avito_type"] == "voice", "вид обязан доехать: по нему решают, что это звук"
    assert "2229d5a7-072e" in вложение["media_id"], "без идентификатора запись не спросить"


def test_video_says_where_to_watch_and_does_not_cry_wolf(caplog: pytest.LogCaptureFixture) -> None:
    """⚠ ВЛАДЕЛЕЦ 02.09: «у нас не загружаются видео, сможешь исправить».

    Исправить нечего: смотреть было нечего с самого начала. Замер боевой базы —
    все 106 сообщений вида `video` приходят с `content: {}`. Авито сообщает
    «это видео» и больше ничего: ни ссылки, ни идентификатора файла, ни обложки.
    105 из 106 доехали в ленту словом «Видео» и нулём вложений.

    Дозагрузить неоткуда: у голосовых для этого есть `getVoiceFiles`, у видео в
    каталоге нет ничего, а сторож `_observe_message_shape` показал в бою полный
    перечень полей ответа ручки чтения — `author_id, content, created,
    direction, id, isRead, type`, внутри `content` только `text`.

    Что в нашей власти — сказать человеку правду и не поднимать ложную тревогу.
    """
    event = _normalize(type="video")

    assert подпись(event) == "Видео — посмотреть можно только в приложении Авито", (
        "голое «Видео» — слово без действия: файла нет, а что делать, не сказано"
    )
    assert not [r for r in caplog.records if "message_without_content" in r.getMessage()], (
        "предупреждение о поломке на каждое видео — ложная тревога: пустота там штатная"
    )
