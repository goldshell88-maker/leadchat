"""Неожиданная ошибка доставки не хоронит сообщение (27.08).

⚠ ЧТО БЫЛО. Задача `deliver_message` написана в расчёте на пять попыток с
задержкой, но ARQ повторяет ТОЛЬКО по `Retry` и `CancelledError`; всё остальное
закрывает job на первой же попытке. Три ветки `except` ловили ошибки Авито —
лимит, отзыв доступа, HTTP-сбой, — а обрыв соединения с Postgres, рестарт
Redis, таймаут задачи и ошибка расшифровки токена не наследуются ни от
`httpx.HTTPError`, ни от `AvitoApiError`.

Такая ошибка проваливалась мимо всех трёх, `_fail` не звался, и строка
оставалась `delivery_status='pending'` НАВСЕГДА. Состояние невидимо целиком:
красную метку диалогу ставит `refresh_undelivered` по `failed`, а сторож
«клиент ждёт» молчит — `awaiting_since` обнулено нажатием «Отправить». Ответ
клиенту не ушёл, и об этом не знает ни оператор, ни система.
"""

import inspect

from app.workers import deliver


def test_unexpected_error_is_caught_at_all():
    """Общий улов есть, и он не проглатывает законный повтор."""
    тело = inspect.getsource(deliver.deliver_message)
    assert "except Exception" in тело, (
        "ошибка вне веток Авито закроет job на первой попытке, и сообщение "
        "останется в pending навсегда"
    )
    # ⚠ `except Retry: raise` обязан стоять ДО общего улова, иначе законный
    # повтор превратился бы в отказ.
    assert тело.index("except Retry:") < тело.index("except Exception"), (
        "общий улов перехватит Retry, поднятый ветками выше"
    )


def test_unexpected_error_retries_and_then_fails():
    """Поведение то же, что у ошибок Авито: повтор, а на исходе бюджета — отказ."""
    тело = inspect.getsource(deliver.deliver_message)
    хвост = тело[тело.index("except Exception") :]
    assert "_fail(ctx, message_id, EXHAUSTED_ERROR)" in хвост, (
        "на исходе попыток сообщение обязано стать failed — оператор увидит "
        "красное и нажмёт «Повторить»"
    )
    assert "Retry(defer=backoff(attempt))" in хвост, "до исхода попыток — повтор"


def test_the_error_is_logged_as_unexpected():
    """В журнале такую ошибку надо отличать: у неё другая причина и другой разбор."""
    тело = inspect.getsource(deliver.deliver_message)
    хвост = тело[тело.index("except Exception") :]
    assert "unexpected=True" in хвост
    assert "type(exc).__name__" in хвост, "без имени класса разбирать нечего"
