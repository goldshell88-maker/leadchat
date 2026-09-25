"""Клиент Авито рядом с сессией базы обязан читать настройки ИЗ БАЗЫ.

ЧТО СЛУЧИЛОСЬ. Владелец нажал «Переключить на настоящий Авито». Строка в
`app_settings` сменилась на `https://api.avito.ru`, экран настроек честно
показал «боевой Авито» — он читает базу. А подключение аккаунта, обновление
токена и доставка сообщений создавали клиент обычным конструктором, который
читает КЭШ ПРОЦЕССА. Кэш никто не обновил, и система продолжала ходить во
встроенный имитатор: каждая попытка привязки возвращала один и тот же
выдуманный аккаунт `111222333`, обновляя одну и ту же строку.

Владелец написал «ничего не привязывается» — и был прав. Хуже всего то, что
интерфейс при этом не врал по своим данным: он показывал ровно то, что лежало
в базе. Расхождение было между двумя источниками правды внутри одной системы,
и увидеть его снаружи было нечем.

ПОЧЕМУ СТРАЖ, А НЕ КОММЕНТАРИЙ. Мест, где создаётся клиент, одиннадцать.
Правило «не забудьте обновить кэш» уже было записано словами в шапке
`services/avito_app` — и всё равно из четырёх мест с сессией его соблюдало
одно. Правило, которое держится на внимательности, не держится.

ПРАВИЛО. Если у функции есть сессия базы (`db`), клиент создаётся через
`AvitoClient.fresh(db)`. Нет сессии — обычный конструктор, значения приезжают
из кэша процесса и обновляются в течение получаса (`CACHE_TTL_SECONDS`).

ЧЕГО ЭТОТ СТРАЖ НЕ ЛОВИТ, СКАЖУ ЧЕСТНО: мест без сессии он не проверяет, а
там кэш всё ещё может отстать на время TTL. Это осознанный компромисс, а не
недосмотр: тянуть сессию в конструктор адаптера ради этого дороже, чем
полминуты запаздывания в фоновой сверке.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

#: Как называются классы-клиенты Авито. `_OutboundClient` — наследник в воркере
#: доставки, и он ходит в Авито ровно так же.
CLIENT_NAMES = {"AvitoClient", "_OutboundClient"}

#: Имена параметров, по которым узнаём сессию базы.
SESSION_ARGS = {"db", "session"}


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _direct_constructions_with_session() -> list[str]:
    """Места, где клиент создан конструктором, хотя сессия рядом была."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in CLIENT_NAMES
            ):
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            if owner is None:
                continue
            names = {a.arg for a in owner.args.args + owner.args.kwonlyargs}
            if names & SESSION_ARGS:
                rel = path.relative_to(APP.parent)
                offenders.append(f"{rel}:{node.lineno} в {owner.name}()")
    return offenders


def test_every_client_next_to_a_session_reads_the_database():
    """Главная проверка файла — ровно та ошибка, что стоила дня работы."""
    offenders = _direct_constructions_with_session()
    assert not offenders, (
        "клиент Авито создан конструктором там, где рядом есть сессия базы —\n"
        "он прочитает кэш процесса, а не настройки владельца.\n"
        "Используйте `await AvitoClient.fresh(db)`:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_itself_can_still_see_the_construction_sites():
    """Страж, который ничего не находит, — это зелёный тест ни о чём.

    Если клиент переименуют или переедет, разбор перестанет находить места
    создания, и первая проверка станет вечно зелёной, ничего не проверяя.
    """
    found = 0
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in CLIENT_NAMES
            ):
                found += 1
    assert found >= 5, (
        f"разбор нашёл всего {found} мест создания клиента Авито — "
        "похоже, класс переименовали и страж ослеп"
    )


def test_fresh_exists_and_refreshes_from_the_database():
    """`fresh` обязан именно перечитывать базу, а не просто быть методом."""
    from app.integrations.avito.client import AvitoClient

    source = pathlib.Path(APP / "integrations" / "avito" / "client.py").read_text(encoding="utf-8")

    assert hasattr(AvitoClient, "fresh")
    assert "ensure_fresh" in source, "fresh() не обновляет настройки из базы"
