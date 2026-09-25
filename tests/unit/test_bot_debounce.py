"""Серия сообщений клиента — один тик бота, а не тик на каждое (правка 21.08).

ЧТО ИЗМЕРЕНО. В живой выгрузке подсказок клиент пишет 2+ сообщения подряд в 370
случаях на 445 диалогов: 246 серий по два, 90 по три, 24 по четыре, 10 длиннее.
Каждое сообщение ставило свой тик, значит:

  • бот отвечал на ПОЛОВИНУ мысли — вторая половина приходила, когда ответ уже
    сочинён, и именно в ней обычно лежит главное («…а ещё смеситель течёт»);
  • один разговор оплачивался моделью столько раз, сколько клиент нажал Enter;
  • диспетчер получал столько же подсказок подряд.

ЧТО СДЕЛАНО. Тик ставится отложенно и один на серию: первый входящий заводит
окно, остальные в этом окне на него садятся. Окна разные по режиму — в подсказке
15 секунд (оператор ждёт помощи), в автоответе 35 (там ответ уходит клиенту, и
живые операторы не отвечают быстрее). Тик читает историю из базы сам, поэтому
видит ВСЮ серию, а не первое сообщение.

Окно живёт в Redis с TTL: очередь может лечь, но диалог не залипнет — ключ
истечёт сам.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.bots import runtime


class _Redis:
    """Ровно то, что нужно дебаунсу: SET NX EX и просмотр ключей."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):  # noqa: A003
        if nx and key in self.keys:
            return None
        self.keys[key] = ex or 0
        return True


class _Bot:
    def __init__(self, mode):
        self.mode = mode


@pytest.mark.asyncio
async def test_первое_сообщение_заводит_окно_остальные_садятся_на_него():
    r = _Redis()
    conv = uuid.uuid4()
    assert await runtime.debounce_claim(r, conv, _Bot("suggest")) is True
    assert await runtime.debounce_claim(r, conv, _Bot("suggest")) is False
    assert await runtime.debounce_claim(r, conv, _Bot("suggest")) is False


@pytest.mark.asyncio
async def test_разные_диалоги_не_мешают_друг_другу():
    r = _Redis()
    assert await runtime.debounce_claim(r, uuid.uuid4(), _Bot("suggest")) is True
    assert await runtime.debounce_claim(r, uuid.uuid4(), _Bot("suggest")) is True


@pytest.mark.asyncio
async def test_срок_ключа_равен_окну_серии():
    r = _Redis()
    conv1, conv2 = uuid.uuid4(), uuid.uuid4()
    текст = "Иии дальше не загружается"
    окно1 = runtime.debounce_window(_Bot("suggest"), conv1, текст=текст)
    окно2 = runtime.debounce_window(_Bot("auto"), conv2, текст=текст)
    await runtime.debounce_claim(r, conv1, _Bot("suggest"), окно=окно1)
    await runtime.debounce_claim(r, conv2, _Bot("auto"), окно=окно2)
    подсказка = r.keys[runtime.DEBOUNCE_KEY.format(conversation_id=conv1)]
    авто = r.keys[runtime.DEBOUNCE_KEY.format(conversation_id=conv2)]
    # ⚠ ЧИСЛА ПЕРЕСМОТРЕНЫ 26.08 ЗАМЕРОМ, А РАЗНИЦА РЕЖИМОВ УБРАНА ОСОЗНАННО.
    # Было 15/35 «на глаз», и минутную паузу между репликами клиента окно не
    # накрывало — владелец прислал снимок с двумя почти одинаковыми подсказками
    # подряд. По боевой базе (19 077 пар подряд идущих реплик) окно 15 с ловит 56 %
    # продолжений серии, 90 с — 90 %; дальше отдача 1–2 пункта за полминуты.
    # Разница режимов не нужна: в подсказке оператор ждёт ОДНУ внятную подсказку
    # вместо трёх спорящих, в авто пауза ещё и полезна — мгновенный ответ выдаёт бота.
    # ⚠ 26.08, ТРЕТИЙ ПЕРЕСМОТР: владелец попросил гибрид 30–90, и окно теперь выбирает
    # ФОРМА сообщения клиента. Отсюда же и главный риск: боевой путь обязан посчитать окно
    # ОДИН раз и отдать его и ключу, и задаче. Здесь проверяется ровно этот договор.
    # ⚠ 27.08, ЧЕТВЁРТЫЙ: полоса 15–30. Замер цены ожидания (шапка test_debounce_window.py)
    # показал, что ответ за ≤30 с даёт заявку в 20,1 % против 15,5 % за 30–90 с. Договор
    # «ключ и задача из одного числа» при этом не меняется — он и проверяется ниже.
    assert 15 <= подсказка <= 30, "окно вышло за полосу владельца"
    assert 15 <= авто <= 30, "окно вышло за полосу владельца"
    assert подсказка == int(окно1.total_seconds()), (
        "срок ключа разошёлся с окном — задача проснётся раньше или позже него"
    )
    assert авто == int(окно2.total_seconds())


@pytest.mark.asyncio
async def test_без_явного_окна_ключ_всё_равно_получает_срок():
    """Обратная совместимость: старые вызовы без `окно=` не должны терять TTL."""
    r = _Redis()
    conv = uuid.uuid4()
    assert await runtime.debounce_claim(r, conv, _Bot("auto")) is True
    срок = r.keys[runtime.DEBOUNCE_KEY.format(conversation_id=conv)]
    assert 15 <= срок <= 30


@pytest.mark.asyncio
async def test_ключ_всегда_с_ttl():
    """Очередь может лечь; без TTL диалог залип бы навсегда."""
    r = _Redis()
    await runtime.debounce_claim(r, uuid.uuid4(), _Bot("suggest"))
    assert all(ttl and ttl > 0 for ttl in r.keys.values())


def test_окно_совпадает_с_отложенной_постановкой():
    """Ключ и defer_by обязаны считаться из одной величины: разойдись они —
    тик ушёл бы раньше, чем истечёт окно, и серия снова разбилась бы."""
    # ⚠ 26.08, ТРЕТИЙ ПЕРЕСМОТР: числа больше нет — есть таблица форм и полоса.
    # Без номера диалога разброс не применяется, поэтому здесь ровно значения таблицы.
    # ⚠ 27.08: полоса опущена до 15–30, базы разведены так, чтобы формы не схлопывались
    # на нижней границе (при широком разбросе в узкой полосе это происходит мгновенно).
    for текст, ожидаем in (
        ("🖼 фото", 17),
        ("Иии дальше не загружается", 27),
        ("Сколько будет стоить ремонт?", 22),
    ):
        for бот in (_Bot("suggest"), _Bot("auto"), None):
            assert runtime.debounce_window(бот, текст=текст) == timedelta(seconds=ожидаем), (
                текст,
                бот,
            )
