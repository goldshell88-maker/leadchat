"""Внешних каналов тревог у системы нет — решение заказчика (docs/23).

ЗАЧЕМ СТОРОЖ НА ОТСУТСТВИЕ. В аудите от 7 августа «тревоги никому не приходят»
записано как недостаток, и первый же, кто прочитает аудит без этого документа,
честно пойдёт его чинить. Решение и его цена записаны в docs/23; здесь стоит
проверка, чтобы решение не отменили молча, по дороге к другой задаче.

ЧТО ИМЕННО СТЕРЕЖЁМ. Не слово «Telegram» — оно законно встречается в тексте
для людей (в профиле есть ссылка на поддержку). Стережём ОТПРАВКУ: обращения к
api.telegram.org и настройки бота. Разница принципиальная: сторож, который
падает на упоминании, за неделю приучает себя обходить.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Каталоги, где отправка не имеет права появиться. Файлы для людей (docs/)
#: сюда не входят: там про Telegram можно писать сколько угодно, в том числе
#: объясняя, почему его нет.
SCANNED = ("app", "deploy", "docker", ".github")

#: Признаки именно ОТПРАВКИ, а не разговора о ней.
FORBIDDEN = (
    re.compile(r"api\.telegram\.org"),
    re.compile(r"ALERT_TG_(?:BOT_TOKEN|CHAT_ID)"),
    re.compile(r"alert_tg_(?:bot_token|chat_id)"),
)

SKIP_DIRS = {"__pycache__", "node_modules", "target", ".venv", "dist"}


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for name in SCANNED:
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or SKIP_DIRS & set(path.parts):
                continue
            if path.suffix in {".pyc", ".png", ".jpg", ".svg", ".ico", ".lock"}:
                continue
            out.append(path)
    return out


def test_nothing_sends_to_telegram() -> None:
    offenders: list[str] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if any(rx.search(line) for rx in FORBIDDEN):
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{line_no}: {line.strip()[:90]}")

    assert not offenders, (
        "Вернулась отправка тревог наружу. Если это осознанно — сначала "
        "перепишите docs/23-ALERTS-DECISION.md, потом этот тест:\n" + "\n".join(offenders)
    )


def test_the_decision_is_written_down() -> None:
    """Обратная сторона: сторож не должен зеленеть на пустом месте.

    Если документ с решением исчезнет, проверка выше останется зелёной и будет
    выглядеть как техническое правило, у которого нет причины. Причина обязана
    лежать рядом и быть читаемой человеком.
    """
    doc = ROOT / "docs" / "23-ALERTS-DECISION.md"
    assert doc.exists(), "решение о тревогах потерялось — docs/23 больше нет"
    text = doc.read_text(encoding="utf-8")
    # Цена решения названа прямо: без неё документ превращается в «мы так
    # захотели», и первый же спор начнётся с нуля.
    assert "узнают утром" in text, "в решении пропала его цена"


@pytest.mark.parametrize(
    "script",
    ["deploy/backup.sh", "deploy/backup-verify.sh", "deploy/healthcheck-alert.sh"],
)
def test_scripts_still_have_somewhere_to_report(script: str) -> None:
    """Убрали канал — но не адресата.

    Главный риск этой чистки: удалить отправку вместе с сообщением и получить
    тишину вместо канала. Каждый скрипт обязан сохранить оба пути — лог и
    центр уведомлений.
    """
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "notify_center" in text, f"{script} больше никому не сообщает"
    assert "log()" in text, f"{script} перестал писать в лог"
