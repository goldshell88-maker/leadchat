"""Ключ подписи ссылок обязателен, а заглушка на проде не проходит (#36).

ЧЕМ ЭТО БЫЛО ОПАСНО. Раздача вложений идёт БЕЗ входа по паролю — пропуском
служит подпись ссылки (app/api/routes/media.py). Значит ключ подписи и есть
единственный замок на переписке клиентов: фотографиях техники, адресах,
телефонах.

У этого ключа стоял пустой дефолт с объяснением «CI и dev не задают
MEDIA_SIGN_KEY». Оба утверждения были неправдой — задают и CI, и tests/conftest,
и .env.example. Комментарий пережил свой повод и оправдывал дыру: приложение
поднималось с пустым ключом молча, подпись превращалась в md5("{exp}{uri} ") и
вычислялась кем угодно.

ВТОРАЯ ПОЛОВИНА БЕДЫ — ЗАГЛУШКА. Обязательность поля закрывает только случай
«переменной нет». Остаётся «переменная есть, но это строка CHANGE_ME, видная на
GitHub», — а именно она лежит в .env.prod.example. То есть путь, которым прод
получает заглушку, это не небрежность, а поведение по умолчанию: скопировал
образец, заполнил половину полей, выкатил. Подстановка в docker-compose
(`${MEDIA_SIGN_KEY:?…}`) ловит только пустое и незаданное.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from pydantic import ValidationError

from app.core.config import PLACEHOLDER_SECRETS, Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Минимум, без которого Settings не собирается вовсе. Ключ подписи сюда НЕ
#: входит — его наличие и есть предмет проверки.
BASE = {
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "token_enc_key": "0" * 44,
    "jwt_secret": "s",
    "avito_client_id": "id",
    "avito_client_secret": "secret",
}

REAL_KEY = "9f2c7b41ad5e08c6f3b1d97e2a486c50"


def _settings(**over: object) -> Settings:
    # `_env_file=None` обязателен: без него pydantic подхватит .env из корня
    # репозитория и тест начнёт проверять содержимое рабочей машины.
    return Settings(_env_file=None, **{**BASE, **over})  # type: ignore[arg-type]


def test_settings_refuse_to_build_without_the_signing_key(monkeypatch) -> None:
    # ПЕРЕМЕННУЮ НАДО СНЯТЬ ЯВНО. `_env_file=None` отключает чтение файла, но
    # не окружение процесса, а tests/conftest.py ставит MEDIA_SIGN_KEY для
    # всего прогона. Без этой строки тест зеленел бы, ничего не проверив —
    # ровно тот случай, который в этом проекте уже ловили дважды.
    monkeypatch.delenv("MEDIA_SIGN_KEY", raising=False)
    with pytest.raises(ValidationError) as exc:
        _settings()
    assert "media_sign_key" in str(exc.value)


@pytest.mark.parametrize("stub", sorted(PLACEHOLDER_SECRETS - {""}))
def test_production_refuses_every_known_placeholder(stub: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(env="production", media_sign_key=stub)
    # Сообщение обязано объяснять последствие, а не только факт. Человек читает
    # его в три часа ночи, когда прод не поднимается.
    assert "скачать" in str(exc.value)


def test_production_accepts_a_real_key() -> None:
    """Сторож не должен зеленеть, отвергая всё подряд."""
    assert _settings(env="production", media_sign_key=REAL_KEY).media_sign_key == REAL_KEY


def test_development_keeps_working_with_the_usual_stub() -> None:
    """Иначе сторож положит собственный CI — и проживёт до первого разбора.

    В разработке и в тестах заглушка не только допустима, но и единственный
    разумный вариант: настоящий секрет в репозитории хуже заглушки.
    """
    assert _settings(env="development", media_sign_key="test-media-sign-key")


@pytest.mark.parametrize("sample", [".env.example", ".env.prod.example"])
def test_the_blacklist_covers_what_the_samples_actually_contain(sample: str) -> None:
    """Иначе завтра в образец впишут новую заглушку, и валидатор её пропустит.

    Список заглушек и файлы-образцы живут порознь и обязаны сходиться. Это
    ровно та связь, которая рвётся молча: образец правят на один символ, а
    сторож продолжает зеленеть.
    """
    text = (ROOT / sample).read_text(encoding="utf-8")
    found = re.search(r"^MEDIA_SIGN_KEY=(.*)$", text, re.MULTILINE)
    assert found, f"в {sample} нет строки MEDIA_SIGN_KEY= — контракт 05 §4 нарушен"
    value = found.group(1).strip().strip("\"'")
    assert value in PLACEHOLDER_SECRETS, (
        f"{sample} держит MEDIA_SIGN_KEY={value!r}, и этого значения нет в чёрном списке "
        "PLACEHOLDER_SECRETS. Значит на проде оно пройдёт как настоящий ключ."
    )
