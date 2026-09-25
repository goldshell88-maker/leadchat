"""Слепой сторож сертификата обязан сказать, что он слеп.

НАЙДЕНО НА БОЕВОЙ СИСТЕМЕ 12 августа. Проверка срока сертификата не работала
НИ РАЗУ: том `/etc/letsencrypt` планировщику прокинут и путь верный, но `ls`
внутри контейнера отвечает `Permission denied` — Let's Encrypt держит `live/`
и `archive/` в режиме 0700 для root, а контейнер работает не от root.

Молчала она при этом тихо: и «файла нет», и «нет прав» писались одной строкой
`cert_file_absent` уровня info — той, которую никто не ищет. Сертификат
продлевается сам, поэтому беды не случилось; случилась бы она в тот день,
когда продление сломается, и узнали бы об этом от клиентов.
"""

from __future__ import annotations

import pathlib

from structlog.testing import capture_logs

from app.scheduler.jobs import watchdog


def test_нет_прав_это_предупреждение_с_подсказкой(tmp_path: pathlib.Path) -> None:
    файл = tmp_path / "fullchain.pem"
    файл.write_bytes(b"soderzhimoe ne vazhno")
    файл.chmod(0o000)

    try:
        with capture_logs() as строки:
            assert watchdog._cert_not_after(str(файл)) is None
    finally:
        файл.chmod(0o600)  # иначе tmp_path не уберётся

    события = {s["event"]: s for s in строки}
    assert "watchdog.cert_unreadable_permissions" in события, (
        "нехватка прав снова маскируется под «файла нет» — сторож слеп и молчит"
    )
    запись = события["watchdog.cert_unreadable_permissions"]
    assert запись["log_level"] == "warning", "слепота сторожа не может быть info-строкой"
    # Подсказка обязана называть починку: без неё читатель журнала знает про
    # беду, но не знает, что делать.
    assert "chmod" in запись["hint"]


def test_настоящее_отсутствие_файла_остаётся_тихим(tmp_path: pathlib.Path) -> None:
    """Вне прода сертификата не бывает — шуметь об этом каждый день незачем."""
    with capture_logs() as строки:
        assert watchdog._cert_not_after(str(tmp_path / "нет-такого.pem")) is None

    события = {s["event"]: s for s in строки}
    assert "watchdog.cert_file_absent" in события
    assert события["watchdog.cert_file_absent"]["log_level"] == "info"
    assert "watchdog.cert_unreadable_permissions" not in события
