"""Заглушка из репозитория не проходит на проде ни одним секретом (проверка 25.09).

Сторож знал только MEDIA_SIGN_KEY. Ключ шифрования токенов Авито в
.env.example был случайной строкой настоящего вида: скопировал образец на
сервер — и токены шифруются ключом, который лежит в репозитории, а ни выкатка,
ни Settings этого не замечают. Секрет подписи входа не сверялся вовсе. Ключ в
CI раскодировался в 31 байт — шифрование в CI падало бы на первом токене.

Теперь три секрета сверяются с одним списком заглушек; образцы окружения и CI
держат только заглушки из этого списка; заглушки ключа шифрования по-прежнему
раскодируются в 32 байта — `cp .env.example .env` даёт рабочую разработку; гейт
выкатки знает те же заглушки, что и Settings.

ДИВЕРСИИ: убрать поле из _GUARDED_SECRETS — краснеет первый тест; вернуть в
.env.example случайный ключ — сверка образцов; вернуть в CI ключ на 31 байт —
проверка разбора; убрать заглушку из шаблона ship.sh — сверка с гейтом.
"""

from __future__ import annotations

import base64
import itertools
import pathlib
import re

import pytest
from pydantic import ValidationError

from app.core.config import PLACEHOLDER_SECRETS, Settings
from app.services.crypto import KEY_SIZE, load_key

ROOT = pathlib.Path(__file__).resolve().parents[2]

SAMPLES = {
    ".env.example": ROOT / ".env.example",
    ".env.prod.example": ROOT / ".env.prod.example",
    "ci.yml": ROOT / ".github" / "workflows" / "ci.yml",
}
GUARDED = ("MEDIA_SIGN_KEY", "TOKEN_ENC_KEY", "JWT_SECRET")

#: «Настоящий» для сторожа — любое значение вне списка заглушек. Строки нарочно
#: читаемые: ключ случайного вида в репозитории и был предметом этой проверки.
REAL = {
    "media_sign_key": "any-value-outside-the-stub-list",
    "token_enc_key": base64.b64encode(bytes(range(KEY_SIZE))).decode(),
    "jwt_secret": "any-secret-outside-the-stub-list",
}
#: Слово, без которого сообщение не объясняет последствие.
STAKE_WORD = {"token_enc_key": "Авито", "jwt_secret": "администратора"}


def _settings(**over: object) -> Settings:
    # `_env_file=None`: без него pydantic подхватит .env рабочей машины.
    base = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "avito_client_id": "id",
        "avito_client_secret": "secret",
        **REAL,
    }
    return Settings(_env_file=None, **{**base, **over})  # type: ignore[arg-type]


def _sample_value(sample: str, name: str) -> str:
    """Значение переменной в образце: `NAME=` в .env, `NAME:` в блоке env CI."""
    text = SAMPLES[sample].read_text(encoding="utf-8")
    found = re.search(rf"^[ \t]*{name}[=:][ \t]*(.*)$", text, re.MULTILINE)
    assert found, f"в {sample} нет строки {name} — контракт 05 §4 нарушен"
    # Хвостовой комментарий отрезается так же, как его режут python-dotenv и YAML.
    return re.split(r"\s+#", found.group(1), maxsplit=1)[0].strip().strip("\"'")


@pytest.mark.parametrize(
    ("field", "stub"),
    list(itertools.product(sorted(STAKE_WORD), sorted(PLACEHOLDER_SECRETS - {""}))),
)
def test_production_refuses_every_stub_for_every_secret(field: str, stub: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(env="production", **{field: stub})
    assert field.upper() in str(exc.value)
    assert STAKE_WORD[field] in str(exc.value)


def test_every_stub_is_named_at_once() -> None:
    """Чинить по одной заглушке за перезапуск — это ночь вместо минуты."""
    with pytest.raises(ValidationError) as exc:
        _settings(
            env="production",
            media_sign_key="CHANGE_ME",
            token_enc_key="CHANGE_ME",
            jwt_secret="CHANGE_ME",
        )
    assert all(name in str(exc.value) for name in GUARDED)


def test_production_accepts_real_secrets() -> None:
    """Сторож не должен зеленеть, отвергая всё подряд."""
    built = _settings(env="production")
    assert (built.media_sign_key, built.token_enc_key, built.jwt_secret) == (
        REAL["media_sign_key"],
        REAL["token_enc_key"],
        REAL["jwt_secret"],
    )


def test_development_keeps_working_with_the_sample_stubs() -> None:
    """Иначе `cp .env.example .env` перестанет давать рабочую разработку."""
    stubs = {name.lower(): _sample_value(".env.example", name) for name in GUARDED}
    assert _settings(env="development", **stubs)


@pytest.mark.parametrize(("sample", "name"), list(itertools.product(SAMPLES, GUARDED)))
def test_the_samples_hold_only_known_stubs(sample: str, name: str) -> None:
    """Образцы и чёрный список живут порознь и обязаны сходиться.

    Значение, которого нет в списке, на проде пройдёт как настоящий секрет, —
    а ключ настоящего вида ещё и выглядит для читателя как утёкший.
    """
    value = _sample_value(sample, name)
    assert value in PLACEHOLDER_SECRETS, (
        f"{sample} держит {name}={value!r}, и этого значения нет в PLACEHOLDER_SECRETS"
    )


@pytest.mark.parametrize("sample", [".env.example", "ci.yml"])
def test_the_stub_encryption_keys_still_decode(sample: str) -> None:
    """Заглушка ключа обязана оставаться ключом: 32 байта после base64."""
    assert len(load_key(_sample_value(sample, "TOKEN_ENC_KEY"))) == KEY_SIZE


def test_the_ship_gate_refuses_every_stub() -> None:
    """Гейт выкатки держит свой шаблон заглушек в shell — списки обязаны сходиться.

    Иначе заглушка, которую знает Settings, проходит проверку секретов на
    сервере, выкатка пересоздаёт контейнеры, и прод не поднимается уже после
    замены — вместо остановки до неё.
    """
    ship = (ROOT / "deploy" / "workstation" / "ship.sh").read_text(encoding="utf-8")
    found = re.search(r"grep -qiE '\^\((.+?)\)\\\$'", ship)
    assert found, "в ship.sh нет шаблона заглушек в проверке секретов"
    gate = re.compile(rf"^({found.group(1)})$", re.IGNORECASE)
    missed = sorted(stub for stub in PLACEHOLDER_SECRETS - {""} if not gate.match(stub))
    assert not missed, f"гейт выкатки пропустит заглушки {missed} — впишите их в ship.sh"
