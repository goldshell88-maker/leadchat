"""SM-10: какие каналы Авито выпали в needs_reauth ПОСЛЕ выкатки (проверка 24.09).

Проверка ищет регресс деплоя — выкатку, убившую обновление токенов. Но она
падала на ЛЮБОМ канале в needs_reauth, в том числе на выпавшем раньше и по
внешней причине: 24.09 в 14:10 UTC Авито перестал принимать токен канала
«Степан КП», и следующая выкатка откатила исправный код, хотя обновление
токенов было ни при чём. Вернуть такой канал может только его владелец —
переподключением, — а выкатки до того вставали бы все.

ship.sh снимает список до выкатки (`SMOKE_REAUTH_BEFORE`, id через запятую),
smoke.sh передаёт сюда список каналов из API. Провал — только на НОВЫХ;
прежние называются, но выкатку не роняют. Сравниваются сами каналы, а не их
число: вернулся один и выпал другой — это регресс, хотя число прежнее.

Вход: JSON ответа `GET /avito-accounts` на stdin, список «до» первым
аргументом. Выход: «<новых> <прежних>». Разобрать JSON не удалось — код 2.
Только стандартная библиотека: скрипт идёт на хосте, без окружения приложения.
"""

from __future__ import annotations

import json
import sys


def split_reauth(accounts_json: str, before: str) -> tuple[list[str], list[str]]:
    """(новые, прежние) каналы в needs_reauth относительно списка «до»."""
    items = json.loads(accounts_json).get("items", [])
    now = sorted(str(item["id"]) for item in items if item.get("status") == "needs_reauth")
    earlier = {part for part in before.split(",") if part}
    return [a for a in now if a not in earlier], [a for a in now if a in earlier]


def main() -> int:
    try:
        new, old = split_reauth(sys.stdin.read(), sys.argv[1] if len(sys.argv) > 1 else "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return 2
    print(f"{len(new)} {len(old)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
