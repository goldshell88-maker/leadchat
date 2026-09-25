"""Сторожа правок сплошного разбора 23.08.

Каждый тест закрывает одну находку и назван так, чтобы падение сразу говорило,
какое правило вернулось назад. Дефекты объединены в один файл намеренно: они
разные по зоне, но одной породы — «два места про одно правило разъехались».
"""

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from app.bots import handoff as handoff_mod
from app.bots import runtime as runtime_mod
from app.bots import validator as validator_mod
from app.bots.state import Outbox
from app.services import inbox as inbox_mod
from app.services import stats as stats_mod

КОРЕНЬ = Path(__file__).resolve().parents[2]


def код(источник: str) -> str:
    """Только исполняемые строки: комментарии и докстроки к делу не относятся.

    Сторожа ищут КОД, а объяснения правок эти же слова цитируют — иначе тест
    падал бы на собственном комментарии, который эту правку и описывает.
    """
    строки = []
    в_докстроке = False
    for строка in источник.splitlines():
        голая = строка.strip()
        if голая.count('"""') == 1:
            в_докстроке = not в_докстроке
            continue
        if в_докстроке or голая.startswith("#") or голая.startswith('"""'):
            continue
        строки.append(строка.split("  # ")[0])
    return "\n".join(строки)


# --------------------------------------------------------------- бот: подсказки


def test_тик_не_глушит_режим_подсказок() -> None:
    """Ворота тика обязаны быть зеркалом `bot_entry_block`, а не второй лестницей.

    Ворота с 21.08 пропускают подсказки мимо `muted`/`has_operator_messages`:
    подсказка это заметка, клиенту она не уходит. Тик про режим не знал и глушил
    бота на первом же диалоге с чужой историей — `muted` не сбрасывается никогда.
    С 24.09 ворота тика живут в `_tick_guards`: их проходят обе транзакции тика.
    """
    источник = код(inspect.getsource(runtime_mod._tick_guards))
    assert "подсказки" in источник, (
        "В воротах тика пропала развилка по режиму бота — режим подсказок снова "
        "будет глушиться на первом тике. Ворота: bot_entry_block."
    )
    строка_глушения = [s for s in источник.splitlines() if "has_operator_messages" in s]
    assert строка_глушения, "проверка has_operator_messages исчезла из тика"
    assert строка_глушения[0].strip().startswith("elif"), (
        "Глушение снова стало безусловным: has_operator_messages должно стоять в "
        "ветке ДЛЯ АВТО-режима (elif), а не поверх подсказок."
    )


# ------------------------------------------------------- бот: кадры о передаче


def test_кадры_передачи_собираются_из_диалога_а_не_из_литералов() -> None:
    """Диалог, оставшийся за человеком, не должен показываться всем как ничей.

    Ветка `do_handoff` намеренно НЕ трогает владельца, если диалог уже у
    человека. Кадр же собирался из литералов `"new"` / `None` / `True` — и
    экран верил ему буквально.
    """
    источник = код(inspect.getsource(handoff_mod.do_handoff))
    assert '"status": "new"' not in источник, "статус в кадре снова литерал"
    assert '"in_inbox": True' not in источник, "in_inbox в кадре снова литерал"
    assert "to_queue" in источник, "решение ветки больше не запоминается"
    assert '"status": conv.status' in источник, "статус кадра обязан браться из диалога"


def test_кадр_очереди_уходит_только_когда_диалог_в_неё_попал() -> None:
    источник = код(inspect.getsource(handoff_mod.do_handoff))
    хвост = источник[источник.index("INBOX_NEW") - 400 :]
    assert "if queue_row is not None" in хвост, (
        "INBOX_NEW снова публикуется безусловно — во «Входящих» появится строка "
        "на диалог, которого в очереди нет."
    )


# ------------------------------------------ бот: уведомления доезжают в браузер


def test_outbox_везёт_уведомления_и_чистит_их() -> None:
    outbox = Outbox()
    outbox.notification(object())
    outbox.notification(None)  # None не кладём: доставлять нечего
    assert len(outbox.notifications) == 1
    outbox.clear()
    assert outbox.notifications == []


def test_передача_кладёт_уведомления_в_outbox_а_не_в_мусор() -> None:
    """Итог `notifications.notify` обязан лечь в outbox, а не быть выброшенным.

    ⚠ СЧИТАЕМ ПО ДЕРЕВУ, А НЕ ПО СТРОКЕ. Сторож искал точный текст
    `outbox.notification(await notifications.notify(` — и покраснел от одного
    только `ruff format`, который развернул тот же вызов на три строки. Правило
    здесь про СТРУКТУРУ вызова, а не про то, где стоят переносы: тест,
    краснеющий от форматирования, приучает чинить тест вместо кода.
    """
    # ⚠ СЧИТАЕМ ПО ВСЕЙ ПЕРЕДАЧЕ, А НЕ ПО ОДНОЙ ФУНКЦИИ. 28.08 «Клиент недоволен»
    # переехал в помощник `notify_negative`: его зовёт и сама передача, и
    # классификатор, когда диалог уже у человека. Правило прежнее — итог `notify`
    # обязан лечь в outbox; изменилось лишь то, в скольких телах он лежит.
    дерево = ast.parse(
        "\n".join(
            textwrap.dedent(inspect.getsource(f))
            for f in (handoff_mod.do_handoff, handoff_mod.notify_negative)
        )
    )
    найдено = 0
    for узел in ast.walk(дерево):
        if not (isinstance(узел, ast.Call) and isinstance(узел.func, ast.Attribute)):
            continue
        if узел.func.attr != "notification" or not isinstance(узел.func.value, ast.Name):
            continue
        if узел.func.value.id != "outbox" or len(узел.args) != 1:
            continue
        внутри = узел.args[0]
        if not isinstance(внутри, ast.Await) or not isinstance(внутри.value, ast.Call):
            continue
        вызов = внутри.value.func
        if isinstance(вызов, ast.Attribute) and вызов.attr == "notify":
            найдено += 1
    assert найдено == 2, (
        "Итог notify снова выбрасывается: строка ляжет в базу немой — ни тоста, "
        f"ни обновления колокольчика (список идёт без refetchInterval). Нашлось: {найдено}."
    )


def test_flush_outbox_доставляет_уведомления_после_commit() -> None:
    источник = код(inspect.getsource(runtime_mod.flush_outbox))
    assert "notifications.deliver" in источник, (
        "flush_outbox перестал доставлять уведомления — кадр в браузер не уйдёт."
    )


# ------------------------------------------------- сервер: роутеры без глушителя


def test_все_разделы_api_смонтированы() -> None:
    """Ошибка импорта не должна поднимать API без целого раздела.

    `try/except ImportError` вокруг stats/audit/avito пережил параллельные
    спринты и превратился в глушитель: контейнер healthy, smoke зелёный, а у
    диспетчеров пропали каналы.
    """
    from app.main import create_app

    пути = set(create_app().openapi().get("paths", {}))
    for нужен in ("/api/v1/stats/summary", "/api/v1/audit-log", "/api/v1/me/channels"):
        assert any(нужен in p for p in пути), f"раздел не смонтирован: {нужен}"


def test_главный_модуль_не_глушит_ошибки_импорта() -> None:
    текст = код((КОРЕНЬ / "app" / "main.py").read_text(encoding="utf-8"))
    assert "importlib" not in текст, "динамическое монтирование роутеров вернулось"
    assert "routes.unavailable" not in текст, "глушитель ошибок импорта вернулся"


# ------------------------------------------------ числа


def test_карточка_очереди_считает_очередь_а_не_статус() -> None:
    """`queue_now` обязан считаться тем же предикатом, что и сама очередь."""
    источник = код(inspect.getsource(stats_mod.snapshot_now))
    assert 'by_status["new"]' not in источник.split('"queue_now"')[1][:80], (
        "queue_now снова равен числу диалогов в статусе «Новый» — карточка "
        "разойдётся с бейджем «Входящие» на диалогах под ботом."
    )
    assert "inbox.queue_condition()" in источник, (
        "queue_now считается мимо единственного источника правды об очереди."
    )


def test_предикат_очереди_остался_единственным_источником() -> None:
    """Копия правила очереди в тексте SQL разъедется с оригиналом на первой правке."""
    текст = код((КОРЕНЬ / "app" / "services" / "stats.py").read_text(encoding="utf-8"))
    assert "offered_at IS NOT NULL" not in текст, (
        "В stats.py появилась вторая копия условия очереди — правило обязано "
        "жить только в inbox.queue_condition()."
    )
    assert callable(inbox_mod.queue_condition)


# ------------------------------------------------------- бот: валидатор сценария


@pytest.mark.parametrize("ключ", ["on_no_match", "on_invalid", "on_timeout"])
def test_валидатор_обходит_все_ветки_меню(ключ: str) -> None:
    """Движок ходит по `on_invalid` у меню (engine.py:547, :569) — значит и граф обязан.

    Пока валидатор ключа не знал, ссылка на несуществующий шаг проходила
    сохранение молча и срабатывала уже на живом клиенте: `bot.broken_ref` и
    передача человеку с причиной «сценарий изменился», не имеющей отношения к правде.
    """
    рёбра = validator_mod._edges({"type": "menu", ключ: "шаг-которого-нет", "params": {}})
    assert (ключ, "шаг-которого-нет") in рёбра, f"ветка {ключ} у меню не попадает в граф"


# ------------------------------------------------- сторож не притворяется настраиваемым


def test_каждая_настройка_сторожа_существует_в_конфигурации() -> None:
    """`_int_setting("watchdog_…")` обязан звать поле, которое есть в `Settings`.

    До 23.08 пять порогов читались по именам, которых в конфигурации нет и не
    было: `getattr` всегда отдавал None, значит всегда работал default. Человек,
    вписавший `WATCHDOG_AVITO_DOWN_RED=42` в окружение, не менял ничего — и
    узнать об этом ему было неоткуда. Такое имя не «настройка», а её изображение.

    Сторож ловит не только возврат снятых пятерых, но и любую новую опечатку:
    ошибиться в строке легко, а поведение при ошибке — тишина.
    """
    import re

    from app.core.config import Settings

    текст = (КОРЕНЬ / "app" / "scheduler" / "jobs" / "watchdog.py").read_text(encoding="utf-8")
    имена = set(re.findall(r'_(?:int|str)_setting\(\s*"([a-z_]+)"', текст))
    поля = set(Settings.model_fields)
    чужие = sorted(имена - поля)
    assert not чужие, (
        f"Сторож читает несуществующие поля конфигурации: {чужие}. "
        "Либо заведите поля в Settings И строки в примерах окружения, либо "
        "сделайте пороги честными константами модуля."
    )
    assert имена, "вызовы _int_setting исчезли — проверка перестала что-либо стеречь"


# ---------------------------------------------- дефекты второго круга (23.08)


def test_scenario_revision_не_течёт_в_переменные_бота() -> None:
    """Поле снято, но ключ обязан остаться читаемым.

    `scenario_revision` писался на каждое входящее и не читался никем. Снять его
    из `KNOWN_KEYS` было нельзя: у живых диалогов он лежит в `bot_vars`, а
    `from_dict` любой НЕизвестный скаляр мигрирует в переменные бота — и те идут
    и в подстановку шаблонов, и в сводку оператору при передаче. У каждого
    диалога появилась бы видимая человеку переменная «scenario_revision = 3».
    """
    from app.bots.state import KNOWN_KEYS, BotState

    assert "scenario_revision" in KNOWN_KEYS, (
        "Ключ убран из KNOWN_KEYS — старые значения потекут в переменные бота."
    )
    состояние = BotState.from_dict(
        {"bot_id": "b1", "scenario_revision": 3, "step": "ask_city", "vars": {"city": "Брянск"}}
    )
    assert "scenario_revision" not in состояние.vars
    assert состояние.vars == {"city": "Брянск"}
    assert "scenario_revision" not in состояние.dump(), "поле снова пишется в базу"


def test_кадр_зависшего_бота_не_отбирает_диалог_у_владельца() -> None:
    from app.scheduler.jobs import bot_stuck

    источник = код(inspect.getsource(bot_stuck.release_in_session))
    assert "ушёл_в_очередь" in источник, "решение ветки снова не запоминается"
    assert '"assignee": None' not in источник.split("патч")[0], "assignee снова безусловен"


def test_обогащение_берёт_бюджет_и_не_хоронит_429() -> None:
    from app.services import client_enrich

    источник = код(inspect.getsource(client_enrich))
    assert 'bucket="bulk"' in источник, (
        "Обогащение снова ходит в Авито мимо лимитера — платит за это доставка "
        "ответов клиентам, они делят одно окно."
    )
    assert "except RateLimited" in источник, (
        "429 снова попадает в общую ветку и заканчивается строкой в журнале: "
        "имя клиента и объявление не появятся никогда."
    )


def test_график_и_карточка_читают_один_источник() -> None:
    """Витрина обновляется раз в час; карточка на том же экране считается живьём."""
    from app.services import stats

    assert set(stats._TS_LIVE_TEMPLATES) == {"conversations_new", "frt_operator_median"}, (
        "Живые близнецы шаблонов графика пропали — график снова отстанет от карточки на час."
    )
    источник = код(inspect.getsource(stats.timeseries))
    assert "_TS_LIVE_TEMPLATES" in источник and "today_msk()" in источник


def test_отказ_от_передачи_доставляет_уведомление() -> None:
    from app.api.routes import conversations as routes

    хвост = код(inspect.getsource(routes._transfer_reply))
    assert "notifications.deliver" in хвост, (
        "Уведомление об отказе снова остаётся в базе немым: ни тоста, ни обновления колокольчика."
    )
    assert хвост.index("db.commit()") < хвост.index("notifications.deliver"), (
        "deliver обязан идти ПОСЛЕ commit'а — иначе весть о событии придёт раньше самого события."
    )


def test_обещанная_подрезка_маркеров_вызываема() -> None:
    """Докстринг обещает «крон/скрипт» — скрипт обязан существовать."""
    from app.cli import app as cli

    имена = {c.name for c in cli.registered_commands}
    assert "prune-read-markers" in имена, (
        "Команда пропала, а докстринг prune_marker_hash по-прежнему обещает "
        "вызов скриптом — обещание снова без исполнения."
    )
