"""Каждая ручка API чем-то охраняется (27.08).

⚠ ЗАЧЕМ СТОРОЖ. Обход 141 ручки 27.08 показал, что дыр НЕТ — и это ровно тот
результат, который нечем удержать: новая ручка добавляется одной строкой, а
забытая охрана видна только тому, кто пойдёт проверять заново.

Проверка статическая, по дереву разбора: у каждой функции-обработчика должна
быть зависимость-охрана — `require_permission(...)` прямо в подписи, алиас,
объявленный в шапке модуля (`manage = require_permission("bots:manage")`),
зависимость роутера (`dependencies=[Depends(require_lead_token)]`) или
`get_current_user`, если ручка про самого себя.

Публичные ручки перечислены поимённо ниже: список — часть проверки, и
пополнять его можно только осознанно.
"""

import ast
import pathlib

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2] / "app" / "api" / "routes"

#: Ручки, у которых охраны нет НАМЕРЕННО, и чем они закрыты вместо права.
ПУБЛИЧНЫЕ: dict[tuple[str, str], str] = {
    ("POST", "/login"): "вход по паролю — охранять нечем",
    ("POST", "/refresh"): "обмен refresh-токена, он и есть пропуск",
    ("GET", "/invite/{token}"): "приглашение открывают до входа; пропуск — сам токен",
    ("POST", "/invite/accept"): "то же приглашение, шаг подтверждения",
    ("GET", "/avito/connect/{token}"): "переход к согласию Авито по одноразовому токену",
    ("GET", "/avito/callback"): "возврат от Авито; сверяется state",
    ("POST", "/avito/callback"): "тот же возврат методом POST",
    ("GET", "/api/health"): "проба живости для балансировщика",
    ("GET", "/api/health/deep"): "глубокая проба для дежурного",
    ("POST", "/api/hooks/avito/{account_id}"): "вебхук Авито; проверяется подпись",
    ("GET", "/media/{relpath:path}"): "подписанная ссылка на файл, срок в подписи",
    # «Прав нет по построению — человек как раз не может войти» (шапка support.py).
    # От перебора её держит ограничение частоты: 429 с `retry_after_sec`.
    ("POST", "/support/password-reset"): "заявка на сброс пароля со страницы входа",
}

#: Имена зависимостей, которые считаются охраной сами по себе.
ОХРАНЫ = {"get_current_user", "current_user", "require_service_token", "require_lead_token"}


def _алиасы(дерево: ast.Module) -> set[str]:
    """`manage = require_permission("bots:manage")` в шапке модуля."""
    имена = set()
    for узел in дерево.body:
        if isinstance(узел, ast.Assign) and isinstance(узел.value, ast.Call):
            func = узел.value.func
            if isinstance(func, ast.Name) and func.id == "require_permission":
                for цель in узел.targets:
                    if isinstance(цель, ast.Name):
                        имена.add(цель.id)
    return имена


def _зависимости(узел: ast.AST) -> set[str]:
    """Имена внутри всех `Depends(...)` поддерева."""
    найдено = set()
    for под in ast.walk(узел):
        if not isinstance(под, ast.Call):
            continue
        func = под.func
        if isinstance(func, ast.Name) and func.id == "Depends" and под.args:
            арг = под.args[0]
            if isinstance(арг, ast.Name):
                найдено.add(арг.id)
            elif isinstance(арг, ast.Call) and isinstance(арг.func, ast.Name):
                найдено.add(арг.func.id)
        elif isinstance(func, ast.Name) and func.id == "require_permission":
            найдено.add("require_permission")
    return найдено


def _ручки() -> list[tuple[str, str, str, set[str]]]:
    """(файл, метод, путь, имена зависимостей) для каждой ручки."""
    out = []
    for путь_файла in sorted(КОРЕНЬ.glob("*.py")):
        дерево = ast.parse(путь_файла.read_text(encoding="utf-8"))
        алиасы = _алиасы(дерево)
        for узел in ast.walk(дерево):
            if not isinstance(узел, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for дек in узел.decorator_list:
                if not (isinstance(дек, ast.Call) and isinstance(дек.func, ast.Attribute)):
                    continue
                метод = дек.func.attr.upper()
                if метод not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                первый = дек.args[0] if дек.args else None
                путь = первый.value if isinstance(первый, ast.Constant) else ""
                # зависимости берём и из подписи, и из самого декоратора
                зав = _зависимости(узел.args) | _зависимости(дек)
                зав |= {и for и in зав if и in алиасы}
                охрана = {и for и in зав if и in алиасы or и in ОХРАНЫ or и == "require_permission"}
                out.append((путь_файла.name, метод, путь, охрана))
    return out


РУЧКИ = _ручки()


def test_routes_are_discovered_at_all():
    """Сторож бесполезен, если разбор перестал находить ручки."""
    assert len(РУЧКИ) > 100, f"нашлось всего {len(РУЧКИ)} ручек — разбор сломался"


@pytest.mark.parametrize("файл,метод,путь", [(f, m, p) for f, m, p, _ in РУЧКИ])
def test_every_route_has_a_guard(файл: str, метод: str, путь: str):
    охрана = next(o for f, m, p, o in РУЧКИ if (f, m, p) == (файл, метод, путь))
    if (метод, путь) in ПУБЛИЧНЫЕ:
        # ⚠ ЗДЕСЬ СТОЯЛО `assert not охрана or True` — выражение, истинное всегда.
        # Проверка публичной ручки не должна быть пустой: если охрана у неё
        # ПОЯВИЛАСЬ, запись в списке устарела и молча разрешает будущую ручку с
        # тем же путём.
        assert not охрана, (
            f"{файл}: {метод} {путь} значится публичной, но охрана появилась ({sorted(охрана)}). "
            "Уберите её из ПУБЛИЧНЫЕ."
        )
        return
    assert охрана, (
        f"{файл}: {метод} {путь} — ни права, ни `get_current_user`. "
        "Если ручка публична намеренно, впишите её в ПУБЛИЧНЫЕ с объяснением."
    )


def test_public_list_has_no_stale_entries():
    """⚠ Список публичных ручек — не свалка. Запись, которой больше нет в коде,
    молча разрешала бы будущую ручку с тем же путём."""
    живые = {(m, p) for _, m, p, _ in РУЧКИ}
    лишние = sorted(k for k in ПУБЛИЧНЫЕ if k not in живые)
    assert not лишние, f"в списке публичных остались несуществующие ручки: {лишние}"
