"""Фото и голосовые клиента доезжают до бота (правка 21.08).

ЧТО БЫЛО СЛОМАНО. ``load_dialog_for_ai`` пропускала сообщение с пустым телом:

    body = (msg.body or "").strip()
    if not body:
        continue

У сообщения с одним вложением тело пустое ВСЕГДА — фотография живёт в
``attachments``. То есть каждое сообщение, где клиент прислал только фото или
голосовое, вырезалось из истории целиком, и бот отвечал так, будто последним
словом клиента было предыдущее текстовое сообщение. Замер по живой выгрузке
подсказок: 196 из 2219 реплик клиента (8.8%) — пустые, и на каждой бот
подсказывал вслепую.

Отдельная потеря: у лид-бота ЕСТЬ зрение. Он разбирает «🖼 URL» в тексте,
скачивает картинку и отдаёт модели картинкой (brain/server.py, `_content_with_images`).
Этот механизм не получал на вход ничего — LeadChat никогда не передавал ссылку.

ЧТО ПРОВЕРЯЕТСЯ. Вложение превращается в строку диалога: картинка со ссылкой —
в «🖼 URL» для зрения; вложение без ссылки и не-картинка — в честную подпись,
чтобы бот знал, что клиент что-то прислал, и не переспрашивал в пустоту.
Своей модели (mask=True) ссылка НЕ отдаётся: там дорога другая и согласия на
выгрузку фотографий клиента наружу нет — уходит только подпись.
"""

from __future__ import annotations

import uuid

import pytest

from app.bots.engine import ScenarioEngine


class _Row:
    def __init__(self, body, attachments=None, sender="client"):
        self.body = body
        self.attachments = attachments or []
        self.sender_type = sender
        self.id = uuid.uuid4()


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Db:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        # порядок в запросе — по убыванию, движок разворачивает сам
        return _Result(list(reversed(self._rows)))


class _Conv:
    def __init__(self):
        self.id = uuid.uuid4()


def _engine(rows):
    eng = ScenarioEngine.__new__(ScenarioEngine)
    eng.db = _Db(rows)
    eng.conv = _Conv()
    return eng


ФОТО = {"kind": "image", "name": "Фотография", "url": "https://cdn.avito.ru/a/b.jpg"}
ФОТО_БЕЗ_ССЫЛКИ = {"kind": "image", "name": "Фотография"}
ГОЛОС = {"kind": "file", "name": "Голосовое сообщение", "avito_type": "voice"}


@pytest.mark.asyncio
async def test_фото_доезжает_ссылкой_для_зрения():
    eng = _engine([_Row("Здравствуйте, сколько будет?"), _Row(None, [ФОТО])])
    dialog = await eng.load_dialog_for_ai(10, mask=False)
    assert len(dialog) == 2, "сообщение с фото не должно исчезать"
    assert "🖼 https://cdn.avito.ru/a/b.jpg" in dialog[-1]["content"]


@pytest.mark.asyncio
async def test_своей_модели_ссылка_не_уходит():
    eng = _engine([_Row(None, [ФОТО])])
    dialog = await eng.load_dialog_for_ai(10, mask=True)
    assert len(dialog) == 1
    assert "http" not in dialog[0]["content"]
    assert "фото" in dialog[0]["content"].lower()


@pytest.mark.asyncio
async def test_вложение_без_ссылки_и_голосовое_превращаются_в_подпись():
    eng = _engine([_Row(None, [ФОТО_БЕЗ_ССЫЛКИ]), _Row(None, [ГОЛОС])])
    dialog = await eng.load_dialog_for_ai(10, mask=False)
    assert len(dialog) == 2
    assert "фото" in dialog[0]["content"].lower()
    assert "голосов" in dialog[1]["content"].lower()


@pytest.mark.asyncio
async def test_текст_с_фото_сохраняет_и_то_и_другое():
    eng = _engine([_Row("вот такое у меня", [ФОТО])])
    dialog = await eng.load_dialog_for_ai(10, mask=False)
    assert "вот такое у меня" in dialog[0]["content"]
    assert "🖼 https://cdn.avito.ru/a/b.jpg" in dialog[0]["content"]


@pytest.mark.asyncio
async def test_пачка_фото_не_раздувает_реплику():
    много = [dict(ФОТО, url=f"https://cdn.avito.ru/{i}.jpg") for i in range(9)]
    eng = _engine([_Row(None, много)])
    dialog = await eng.load_dialog_for_ai(10, mask=False)
    assert dialog[0]["content"].count("🖼") <= 4, "зрение бота берёт не больше четырёх"


@pytest.mark.asyncio
async def test_пустое_без_вложений_по_прежнему_пропускается():
    eng = _engine([_Row(None, []), _Row("  ", []), _Row("есть текст")])
    dialog = await eng.load_dialog_for_ai(10, mask=False)
    assert len(dialog) == 1
    assert dialog[0]["content"] == "есть текст"
