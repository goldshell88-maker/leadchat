"""Бот сам закрывает диалог: заявка собрана или клиент отказался (решение владельца 29.08).

ВОПРОС ВЛАДЕЛЬЦА: «Бот может распознавать, когда заявка готова, сам её закрывать, или
когда отказал клиент — сразу закрыть?» До этой правки — нет, ни в одном из случаев:
собранная заявка оставляла диалог висеть с заметкой «подтвердите время», отказ уходил
человеку. Очередь наполнялась обращениями, по которым говорить уже не о чем.

ЧТО ГОВОРЯТ ДАННЫЕ (боевой корпус лид-бота, 30 526 диалогов). После «записал вас»:
клиент больше не пишет — 40,9 %, отвечает вежливым «хорошо/спасибо» — около половины,
и лишь 8,6 % задают вопрос, просят перенос или досылают данные. То есть в девяти
случаях из десяти говорить дальше не о чем.

ПОЧЕМУ ЗАКРЫВАТЬ БЕЗОПАСНО. Закрытие в LeadChat переоткрываемо (DESIGN §8.3): клиент
напишет — диалог вернётся в очередь «Новым». Те самые 8,6 % возвращаются сами, потерять
обращение нельзя.

ГЛАВНАЯ ЛОВУШКА, из-за которой правка не сводится к одному вызову: `finish_closed`
сбрасывает позицию сценария «до чистого листа», и без `muted` бот вошёл бы ЗАНОВО —
клиент пишет «а можно перенести на завтра?», а бот здоровается и спрашивает, что
случилось. `muted` переживает `finish_closed` и оставляет вернувшегося человеку.
"""

from __future__ import annotations

import ast
import pathlib

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]


def _функция(имя_файла: tuple[str, ...], имя: str) -> str:
    текст = КОРЕНЬ.joinpath(*имя_файла).read_text(encoding="utf-8")
    дерево = ast.parse(текст)
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.AsyncFunctionDef | ast.FunctionDef) and узел.name == имя:
            return ast.get_source_segment(текст, узел) or ""
    raise AssertionError(f"функция {имя} не найдена")


# ── признак доезжает до движка ───────────────────────────────────────────────


def test_lead_ready_пропускается_наружу() -> None:
    """⚠ БЕЗ ЭТОГО ВЕТКА ЗАКРЫТИЯ — МЁРТВЫЙ КОД.

    `_parse` отдаёт движку не всю `meta` (там `lead` с телефоном и
    адресом), а перечисленные поля. Пока `lead_ready` в перечень не входил,
    `meta.get("lead_ready")` в движке был пуст ВСЕГДА, и заявка не закрывалась
    ни разу — при том что обе стороны кода выглядели рабочими.
    """
    тело = _функция(("app", "bots", "leadbot.py"), "_parse")
    assert '"lead_ready"' in тело, "признак готовой заявки не доедет до движка"


def test_сам_лид_наружу_по_прежнему_не_ходит() -> None:
    """Пропускаем булев флаг, а не `lead`: там телефон и адрес клиента."""
    тело = _функция(("app", "bots", "leadbot.py"), "_parse")
    assert 'answer["meta"]["lead"]' not in тело
    assert 'answer["meta"]["lead_ready"] = True' in тело


# ── обе ветки закрытия на месте ──────────────────────────────────────────────


def test_отказ_закрывает_диалог() -> None:
    тело = _функция(("app", "bots", "engine.py"), "exec_ai_answer")
    assert '"refuse"' in тело
    assert "close_by_leadbot" in тело


def test_собранная_заявка_закрывает_диалог() -> None:
    тело = _функция(("app", "bots", "engine.py"), "exec_ai_answer")
    assert 'get("lead_ready")' in тело, "движок не читает признак готовой заявки"


def test_итоги_разные_у_заявки_и_отказа() -> None:
    """`visit` кормит автозаявки (`leads.py`), `not_our_profile` — нет.

    Перепутать их значит либо потерять заявку в отчёте, либо посчитать отказ
    выездом. Итог ставится по `OUTCOMES`, и оба значения обязаны быть оттуда.
    """
    from app.services.leads import OUTCOMES

    тело = _функция(("app", "bots", "engine.py"), "exec_ai_answer")
    assert 'outcome="visit"' in тело
    assert 'outcome="not_our_profile"' in тело
    assert "visit" in OUTCOMES
    assert "not_our_profile" in OUTCOMES


# ── защита вернувшегося клиента ──────────────────────────────────────────────


def test_закрытие_глушит_бота_навсегда() -> None:
    """⚠ БЕЗ `mute` ПРАВКА ХУЖЕ, ЧЕМ ЕЁ ОТСУТСТВИЕ.

    `finish_closed` сбрасывает шаг, ожидание и handoff. Вернувшийся клиент
    переоткрывает диалог — и бот стартует заново, встречая «перенесите на
    завтра» приветствием. `muted` — единственное, что переживает
    `finish_closed` (см. его докстринг), и потому единственный способ отдать
    вернувшегося человеку.
    """
    тело = _функция(("app", "bots", "engine.py"), "close_by_leadbot")
    assert "self.state.mute()" in тело
    # Сравниваем позиции в КОДЕ, а не в тексте: `finish_closed` упомянут и в
    # докстринге выше — по нему проверка прошла бы всегда, ничего не проверяя.
    код = тело[тело.index('"""', тело.index('"""') + 3) + 3 :]
    assert код.index("self.state.mute()") < код.index("finish_closed"), (
        "mute обязан стоять ДО finish_closed"
    )


def test_muted_переживает_закрытие() -> None:
    """Свойство, на котором держится предыдущий тест, — проверяем его само."""
    from datetime import UTC, datetime

    from app.bots.state import BotState

    st = BotState()
    st.mute()
    st.finish_closed("leadbot", datetime.now(UTC))
    assert st.muted is True, "mute обязан пережить finish_closed"
    assert st.step is None, "позиция сценария обязана сброситься"


def test_muted_не_пускает_бота_обратно() -> None:
    """Заслон на входе: `bot_entry_block` обязан знать про `muted`."""
    тело = _функция(("app", "bots", "runtime.py"), "bot_entry_block")
    assert "state.muted" in тело
    assert '"muted"' in тело


# ── что закрытие делает со строкой диалога ───────────────────────────────────


def test_закрытие_ставит_статус_и_гасит_бота() -> None:
    тело = _функция(("app", "bots", "engine.py"), "close_by_leadbot")
    assert 'set_status(self.conv, "closed"' in тело
    assert "self.conv.bot_active = False" in тело
    assert "clear_snooze" in тело, "у закрытого диалога отложка обязана сниматься"


def test_закрытие_пишет_в_журнал_и_шлёт_кадр() -> None:
    """Иначе строка в чужих вкладках останется живой до перезагрузки."""
    тело = _функция(("app", "bots", "engine.py"), "close_by_leadbot")
    assert "conversation.status_changed" in тело
    assert "conversation:updated" in тело
    assert 'note("close"' in тело


def test_заметка_объясняет_что_диалог_вернётся() -> None:
    """Оператор должен знать, что закрытие не окончательно, — иначе решит,
    что обращение потеряно, и полезет искать его руками."""
    тело = _функция(("app", "bots", "engine.py"), "exec_ai_answer")
    assert "вернётся в очередь" in тело


def test_молчаливое_закрытие_по_таймауту_не_вернулось() -> None:
    """Миграция 0053 отменила закрытие ПО МОЛЧАНИЮ клиента, и это в силе.

    Наша правка закрывает по сказанному клиентом (отказ) и по собранной заявке.
    Ветка таймаута ожидания обязана по-прежнему вести к человеку.
    """
    текст = КОРЕНЬ.joinpath("app", "bots", "engine.py").read_text(encoding="utf-8")
    assert "close_by_leadbot" in текст
    тело = _функция(("app", "bots", "engine.py"), "close_by_leadbot")
    assert "ask_timeout" not in тело, "таймаут молчания закрывать не должен"


# ── сквозная проверка: от JSON лид-бота до решения движка ────────────────────


def test_ответ_лид_бота_доносит_признак_до_движка() -> None:
    """⚠ ЦЕПОЧКА ИЗ ТРЁХ ЗВЕНЬЕВ, И ЛЮБОЕ МОЛЧА ЛОМАЕТ ОСТАЛЬНЫЕ.

    Лид-бот кладёт `meta.lead_ready` (brain/leadchat.py), `_parse` решает, что
    пропустить движку, движок читает и закрывает. Проверяем середину на
    настоящем теле ответа: именно её отсутствие делало ветку закрытия мёртвой.
    """
    from app.bots.leadbot import _parse

    тело = {
        "reply": "Записал вас, мастер подъедет завтра к 11:00",
        "confidence": 0.9,
        "needs_operator": False,
        "meta": {
            "layer": "claude-sonnet-5",
            "lead_ready": True,
            # ⚠ телефон и адрес в `lead` есть всегда — и наружу идти не должны
            "lead": {"phone": "9990001122", "address": "Ленина 1"},
        },
    }
    ответ = _parse(тело)
    assert ответ is not None
    assert ответ["meta"]["lead_ready"] is True, "движку нечего будет прочитать"
    assert "lead" not in ответ["meta"], "телефон и адрес наружу не отдаём"


def test_без_готовой_заявки_признака_нет() -> None:
    """Пустой ключ означал бы, что бэкенд про заявку что-то сказал."""
    from app.bots.leadbot import _parse

    ответ = _parse(
        {
            "reply": "Подскажите модель телевизора?",
            "confidence": 0.8,
            "needs_operator": False,
            "meta": {"layer": "claude-sonnet-5", "lead_ready": False},
        }
    )
    assert ответ is not None
    assert "lead_ready" not in (ответ.get("meta") or {})


def test_отказ_доносится_тем_же_путём() -> None:
    from app.bots.leadbot import _parse

    ответ = _parse(
        {
            "reply": "С битой матрицей, к сожалению, не помогу",
            "confidence": 0.9,
            "needs_operator": False,
            "meta": {"layer": "роутер", "flag": {"kind": "refuse", "why": "матрица"}},
        }
    )
    assert ответ is not None
    assert ответ["meta"]["flag"]["kind"] == "refuse"


def test_отказ_клиента_доносится_и_закрывает() -> None:
    """Боевой диалог 29.08 (Пятигорск): «мне выезд не подходит» → бот попрощался,
    а диалог остался живым в очереди. Теперь `client_closed` едет наружу и движок
    закрывает с итогом «отказался» — работа наша, отказался клиент."""
    from app.bots.leadbot import _parse

    ответ = _parse(
        {
            "reply": "К сожалению, я только на выезд работаю. Если что-то изменится, напишите",
            "confidence": 0.9,
            "needs_operator": False,
            "meta": {"layer": "claude-sonnet-5", "client_closed": True},
        }
    )
    assert ответ is not None
    assert ответ["meta"]["client_closed"] is True

    тело = _функция(("app", "bots", "engine.py"), "exec_ai_answer")
    assert 'get("client_closed")' in тело, "движок не читает признак"
    assert 'outcome="declined"' in тело, "итог отказа клиента — «отказался», не «непрофиль»"
