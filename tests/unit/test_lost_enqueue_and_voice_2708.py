"""Ответ клиенту не зависает, а голосовое не врёт про поломку (27.08).

Две находки обхода кода, обе про то, что система оставляет человека без
понятного следующего шага.
"""

import inspect

from app.api.routes import messages as routes
from app.services import messages as msgs


def test_enqueue_reports_failure_instead_of_swallowing_it():
    """⚠ ОТВЕТ ОСТАВАЛСЯ «ОТПРАВЛЯЕТСЯ» НАВСЕГДА.

    Постановка задачи доставки глотала любое исключение с комментарием «пузырь
    останется pending; ретрай руками или следующая постановка». Обоих путей в
    системе НЕТ: повторный POST с тем же `client_message_id` уходит в replay и
    задачу не ставит, а «Повторить» работает только со статусом `failed`.

    Значит при недоступной очереди ответ клиенту зависал, и допихнуть его было
    нечем — ни оператору, ни системе.
    """
    тело = inspect.getsource(msgs.enqueue_deliver)
    assert "return False" in тело and "return True" in тело, (
        "функция снова молчит о неудаче, и вызывающему не на что реагировать"
    )


def test_route_marks_the_message_failed_when_the_job_did_not_land():
    тело = inspect.getsource(routes.send_message)
    assert "mark_enqueue_failed" in тело, (
        "сообщение останется pending: его не видит ни «Повторить», ни красная метка диалога"
    )
    assert 'delivery_status="failed"' in тело, "фронт не узнает о судьбе пузыря"


def test_mark_enqueue_failed_touches_only_pending():
    """Уже доставленное или уже неудавшееся трогать нельзя: задача могла встать
    и отработать, пока мы решали."""
    тело = inspect.getsource(msgs.mark_enqueue_failed)
    assert 'delivery_status != "pending"' in тело


def test_voice_url_does_not_answer_500_on_avito_trouble():
    """⚠ «ПРОСЛУШАТЬ» ОТВЕЧАЛО 500 «СБОЙ НА НАШЕЙ СТОРОНЕ».

    Поход в Авито стоял голым, а его ошибки наследуются от обычного
    `Exception`, а не от `ApiError` — их ловил общий перехватчик. Оператор читал
    «сбой на нашей стороне, сообщите администратору» и шёл к администратору,
    тогда как недоступен был Авито или отозван токен канала: лечится это совсем
    другим.
    """
    тело = inspect.getsource(routes.voice_url)
    assert "except AvitoAuthError" in тело, "отзыв доступа надо назвать своим именем"
    assert "except AvitoUnavailable" in тело, "недоступность Авито — не наша поломка"
    assert "except AvitoApiError" in тело, "прочие отказы Авито тоже не 500"
    # Отзыв доступа — это 409 (канал требует переподключения), недоступность — 502.
    assert "status=409" in тело and "status=502" in тело
