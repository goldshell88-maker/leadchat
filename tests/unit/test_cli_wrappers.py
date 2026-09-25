"""Каждая консольная команда с базой ДЕЙСТВИТЕЛЬНО запускает свою работу.

⚠ ОТКУДА СТОРОЖ. 15 августа рефакторинг вынес логику `repair-empty-messages`
в функцию и вместе со старым телом случайно снёс `_run(main)`: команда
определяла работу и молча выходила с кодом 0. Владелец запустил её на бою,
получил пустой вывод и ноль изменений — «зелёная пустышка», самый тихий класс
поломки из существующих. Тесты логики при этом были зелёными: они зовут
функцию напрямую, а обёртку не звал никто.

Проверяется устройством, а не перечнем: у всякой команды, в теле которой
объявлен `async def main`, обязан быть и вызов `_run`. Новая команда попадает
под сторож сама, без правки этого файла.
"""

import ast
import pathlib

APP_CLI = pathlib.Path(__file__).resolve().parents[2] / "app" / "cli.py"


def test_every_command_with_a_main_actually_runs_it() -> None:
    tree = ast.parse(APP_CLI.read_text())
    пустышки: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        is_command = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "command"
            for d in node.decorator_list
        )
        if not is_command:
            continue
        has_main = any(
            isinstance(child, ast.AsyncFunctionDef) and child.name == "main" for child in node.body
        )
        calls_run = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_run"
            for child in ast.walk(node)
        )
        if has_main and not calls_run:
            пустышки.append(node.name)
    assert пустышки == [], (
        f"команды определяют main и не запускают его — зелёные пустышки: {пустышки}"
    )
