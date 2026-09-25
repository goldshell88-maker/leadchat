"""Вложения от клиента приходят в НАШЕМ виде, а не в чужом (#29).

ЧТО БЫЛО. Из разбора вебхука уходил кусок ответа Авито как есть:
``{"type": "image", "sizes": {...}}``. Лента ждёт наш договор — ``kind``,
``url``, ``name``, ``size``. Ни одно поле не совпадало, и фотография от клиента
показывалась строкой «📎 undefined · NaN Б» без ссылки. Не всплывало это
только потому, что встроенный имитатор Авито шлёт один текст.

ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, — устойчивость к форме, которой я не видел.
Виды содержимого у Авито документированы не полностью и меняются, а боевой
аккаунт подключается сегодня. Цена ошибки несимметрична: неизвестное вложение,
показанное как «Вложение (sticker)», — мелкое неудобство; оно же в виде
«undefined · NaN Б» выглядит поломкой всей системы. Поэтому ни один тест ниже
не проверяет «мы знаем все формы»; они проверяют «ни одна форма не даёт
мусора на экране».
"""

import pytest

from app.integrations.avito.adapter import _extract_attachments

# Форма ответа Авито для фотографии: словарь размеров.
IMAGE = {
    "image": {
        "sizes": {
            "140x105": "https://avito.ru/img/small.jpg",
            "640x480": "https://avito.ru/img/big.jpg",
            "1280x960": "https://avito.ru/img/huge.jpg",
        }
    }
}


def test_photo_becomes_a_picture_with_the_largest_version() -> None:
    """Берём самую крупную версию.

    Клиент фотографирует шильдик с номером модели — на превью 140×105 его не
    прочитать, а именно ради номера фотографию и прислали.
    """
    (att,) = _extract_attachments(IMAGE)

    assert att["kind"] == "image"
    assert att["url"] == "https://avito.ru/img/huge.jpg"
    assert att["name"] == "Фотография"
    assert att["avito_type"] == "image"


def test_text_is_not_an_attachment() -> None:
    assert _extract_attachments({"text": "здравствуйте"}) == []


def test_photo_alongside_text() -> None:
    """«Вот такая деталь» + фото — самое частое сообщение в ремонте техники."""
    atts = _extract_attachments({"text": "вот такая деталь", **IMAGE})
    assert len(atts) == 1
    assert atts[0]["kind"] == "image"


def test_voice_without_a_link_is_still_readable() -> None:
    """Голосовое приходит идентификатором, без прямого адреса.

    Оператор обязан увидеть, что клиент прислал голосовое, — даже если открыть
    его прямо сейчас негде. Пустая строка на этом месте читается как поломка.
    """
    (att,) = _extract_attachments({"voice": {"voice_id": "v-777"}})

    assert att["name"] == "Голосовое сообщение"
    assert att.get("url") is None
    assert att["size"] is None
    assert att["media_id"] == "avito_voice_v-777"


@pytest.mark.parametrize(
    ("payload", "expected_name"),
    [
        ({"video": {"video_id": "vid-1"}}, "Видео"),
        ({"location": {"lat": 55.7, "lon": 37.6}}, "Геопозиция"),
        ({"call": {"status": "missed"}}, "Звонок"),
        ({"link": {"url": "https://example.com"}}, "Ссылка"),
        # Вид, которого я не видел и которого может не быть в документации.
        ({"sticker": {"id": 42}}, "Вложение (sticker)"),
        # Значение вообще не словарь — так тоже бывает.
        ({"whatever": "строкой"}, "Вложение (whatever)"),
    ],
)
def test_every_kind_gets_a_human_name(payload: dict, expected_name: str) -> None:
    (att,) = _extract_attachments(payload)
    assert att["name"] == expected_name
    assert att["media_id"], "у строки ленты обязан быть ключ"


def test_nothing_ever_yields_undefined_or_nan() -> None:
    """Сводная проверка ровно того, на что жаловался заказчик.

    Перебираем и знакомые, и заведомо кривые формы: у каждого вложения должны
    быть непустое имя, вид из двух допустимых и размер либо настоящий, либо
    отсутствующий. Ноль вместо неизвестного размера тоже запрещён — он значил
    бы «пустой файл».
    """
    payloads = [
        IMAGE,
        {"voice": {"voice_id": "v1"}},
        {"video": {}},
        {"image": {}},  # фото без единого размера
        {"image": {"sizes": {}}},
        {"image": {"sizes": {"плохо": "https://x/y.jpg"}}},
        {"file": {"name": "смета.pdf", "size": 2048, "url": "https://x/y.pdf"}},
        {"unknown": None},  # пустое значение отбрасывается целиком
        {"strange": []},
    ]
    for payload in payloads:
        for att in _extract_attachments(payload):
            assert isinstance(att["name"], str) and att["name"].strip()
            assert att["kind"] in {"image", "file"}
            assert att["size"] is None or (isinstance(att["size"], int) and att["size"] > 0)
            assert "url" not in att or (isinstance(att["url"], str) and att["url"])


def test_real_file_keeps_its_own_name_and_size() -> None:
    """Если Авито назвал файл и его размер — берём их, а не свою подпись."""
    (att,) = _extract_attachments(
        {"file": {"name": "смета.pdf", "size": 2048, "url": "https://x/y.pdf"}}
    )
    assert att["name"] == "смета.pdf"
    assert att["size"] == 2048
    assert att["url"] == "https://x/y.pdf"
    assert att["kind"] == "file"


def test_two_attachments_get_distinct_keys() -> None:
    """Одинаковые ключи в ленте — это React, рисующий одну строку вместо двух."""
    atts = _extract_attachments({"image": IMAGE["image"], "voice": {"voice_id": "v9"}})
    assert len({a["media_id"] for a in atts}) == 2


def test_bot_marker_is_not_an_attachment() -> None:
    """`flow_id` — пометка «это написал чат-бот Авито», а не вложение.

    Поле объявлено в каталоге API Авито (MessageContent) наравне с image и
    voice, но это строка, а не объект. Без отдельного исключения оно
    превращалось во «Вложение (flow_id)» и висело скрепкой под каждым
    системным сообщением.
    """
    assert _extract_attachments({"text": "здравствуйте", "flow_id": "seller_discount"}) == []


def test_shapes_match_the_avito_catalog() -> None:
    """Формы содержимого — дословно из каталога API Авито (MessageContent).

    Проверялись против скачанной спецификации: `image.sizes` — словарь
    «ШxВ → адрес», `voice.voice_id` — строка, `call.status`, `location.lat/lon`,
    `link.preview`, `item.item_url`. Тест держит нашу нормализацию привязанной
    к настоящему формату, а не к тому, что отдаёт наш же имитатор.
    """
    catalog = {
        "image": {"sizes": {"140x105": "https://a/s.jpg", "1280x960": "https://a/big.jpg"}},
        "voice": {"voice_id": "v-1"},
        "call": {"status": "missed", "target_user_id": 94235311},
        "location": {"kind": "street", "lat": 55.5997, "lon": 37.6},
        "link": {"url": "https://example.com", "preview": {"domain": "example.com"}},
        "item": {"item_url": "https://avito.ru/item", "image_url": "https://avito.ru/i.webp"},
    }
    atts = _extract_attachments(catalog)

    assert len(atts) == len(catalog)
    by_type = {a["avito_type"]: a for a in atts}
    assert by_type["image"]["url"] == "https://a/big.jpg"
    assert by_type["image"]["kind"] == "image"
    assert by_type["voice"]["media_id"] == "avito_voice_v-1"
    assert by_type["link"]["url"] == "https://example.com"
    for att in atts:
        assert att["name"].strip()
        assert att["size"] is None or att["size"] > 0


def test_геоточка_несёт_адрес_и_координаты() -> None:
    """Владелец 15.09: «Место» от клиента — адрес и точка идут в строку адреса."""
    (att,) = _extract_attachments(
        {
            "location": {
                "kind": "street",
                "lat": 56.0123,
                "lon": 37.1234,
                "text": "Московская область, городской округ Химки, посёлок Берёзово, "
                "СНТ Берёзово, 27",
                "title": "СНТ Берёзово, 27",
            }
        }
    )
    assert att["avito_type"] == "location"
    assert (att["lat"], att["lon"]) == (56.0123, 37.1234)
    assert att["address"].startswith("Московская область")
    # Без координат — без точки (ноль не выдумываем).
    (без,) = _extract_attachments({"location": {"text": "Калуга, Ленина 5"}})
    assert "lat" not in без and без["address"] == "Калуга, Ленина 5"
