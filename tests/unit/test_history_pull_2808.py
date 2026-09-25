"""ДОГРУЗКА ИСТОРИИ: МАССОВО, ПОСТОЯННО И СРАЗУ ПО ПРИХОДЕ ЧАТА (28.08).

Просьба владельца дословно: «плохо работает сгрузка диалогов, и сделай её
массовой и постоянной, чтобы если чат приходит во входящие, он автоматом
подтягивал историю сообщений… сейчас у меня всё делается долго и иногда
встаёт».

Замер боя 28.08, ради которого всё и делается:

* `history-status` по тридцати активным каналам: десять «сорвалась» на ~290 из
  1100 чатов, двадцать «не идёт» — не начинали ни разу;
* ключ хода работы канала GLEB: started_at 17:11:15, updated_at 17:16:15 —
  ровно 300 секунд, то есть заход убил таймаут задачи;
* журнал воркера: 22 упавших прогона сверки из 291, из них
  `AvitoApiError: Авито: история чата -> HTTP 503` — отказ на ОДНОМ чате уносил
  прогон канала целиком.

Здесь три проверки на три оставшихся куска: устойчивость сверки, догрузка
истории конкретного чата и то, что она вообще заказывается.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import InboundEvent
from app.models import Message
from app.workers import reconciliation as mod

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 770800
CLIENT_UID = 999801
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(ACCOUNT_UID)


def _chat(chat_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        external_chat_id=chat_id,
        has_unread=True,
        last_message_at=NOW,
        item_title="Ремонт холодильников",
        item_url=None,
        item_price=None,
        client_external_id=str(CLIENT_UID),
        client_name="Клиент",
    )


def _event(msg_id: str, *, chat_id: str) -> InboundEvent:
    return InboundEvent(
        external_chat_id=chat_id,
        external_message_id=msg_id,
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text=f"текст {msg_id}",
        created_at=NOW - timedelta(minutes=5),
        client_name="Клиент",
    )


async def test_one_bad_chat_does_not_kill_the_account_run(
    monkeypatch, db_sessionmaker, redis, account
) -> None:
    """Отказ Авито на одном чате не уносит остальные.

    ⚠ БОЕВОЙ СЛУЧАЙ. В журнале прода: `102.51s ! reconcile:...failed,
    AvitoApiError: Авито: история чата -> HTTP 503`. Отказ поднимался наружу и
    убивал задачу вместе со всеми чатами, до которых она ещё не дошла. А
    следующий прогон начинает список сначала и в том же порядке — значит чаты
    ЗА сбойным могли не проверяться никогда: страховка от потерянных вебхуков
    переставала работать ровно там, где нужна.
    """
    порядок: list[str] = []

    class FakeAdapter:
        async def fetch_chats(self, _account, *, unread_only=True):
            for cid in ("здоровый-1", "больной", "здоровый-2"):
                yield _chat(cid)

        async def fetch_history(self, _account, chat, *, since=None):
            порядок.append(chat.external_chat_id)
            if chat.external_chat_id == "больной":
                raise RuntimeError("Авито: история чата -> HTTP 503")
            yield _event(f"m-{chat.external_chat_id}", chat_id=chat.external_chat_id)

    monkeypatch.setattr(mod, "get_adapter", lambda _ctx: FakeAdapter())

    итог = await mod.reconcile_account(
        {"db_session_factory": db_sessionmaker, "redis": redis}, account.id
    )

    assert порядок == ["здоровый-1", "больной", "здоровый-2"], (
        "прогон оборвался на сбойном чате — чаты за ним не проверяются никогда"
    )
    assert итог["chats_failed"] == 1, "сбойный чат не посчитан: беда стала невидимой"
    async with db_sessionmaker() as db:
        ids = set((await db.execute(sa.select(Message.external_message_id))).scalars().all())
    assert "m-здоровый-2" in ids, "сообщение из чата ЗА сбойным не догнано"


async def test_new_conversation_orders_its_history(monkeypatch, db, redis, account) -> None:
    """Диалог, впервые появившийся во «Входящих», ЗАКАЗЫВАЕТ свою историю.

    ⚠ ПОЧЕМУ ЭТОГО НЕ ДЕЛАЛА СВЕРКА. Она берёт границу истории по последнему
    ВХОДЯЩЕМУ диалога, а у диалога, только что созданного вебхуком, это ровно
    то сообщение, которым он и создан. Дальше стоит быстрый отсев «последнее
    сообщение чата уже у нас» — и он срабатывает всегда. Переписку, бывшую в
    чате ДО первого дошедшего вебхука, не подтягивал никто и никогда: оператор
    открывал диалог и видел одну строку без всякого «что было раньше».
    """
    from app.services import avito_accounts as accounts_svc
    from app.services.inbound import apply_inbound_event

    заказы: list[tuple] = []

    async def fake_enqueue(account_id, conversation_id, external_chat_id, *, live_since=None):
        заказы.append((account_id, conversation_id, external_chat_id, live_since))

    monkeypatch.setattr(accounts_svc, "enqueue_conversation_history", fake_enqueue)

    событие = InboundEvent(
        external_chat_id="чат-новый",
        external_message_id="m-1",
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text="Здравствуйте, а вы стиральные машины чините?",
        created_at=NOW,
        client_name="Клиент",
    )
    assert await apply_inbound_event(db, redis, account, событие) is True
    assert len(заказы) == 1, "история нового диалога не заказана"
    assert заказы[0][2] == "чат-новый"
    # Граница живого потока — отметка создавшего сообщения (проверка 24.09).
    assert заказы[0][3] == NOW

    # Второе сообщение в ТОТ ЖЕ чат заказа не повторяет: диалог уже не новый, а
    # лишний заказ — это два запроса к Авито на каждую реплику клиента.
    ещё = InboundEvent(
        external_chat_id="чат-новый",
        external_message_id="m-2",
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text="Алло?",
        created_at=NOW + timedelta(minutes=1),
        client_name="Клиент",
    )
    assert await apply_inbound_event(db, redis, account, ещё) is True
    assert len(заказы) == 1, "история заказана повторно на каждое сообщение клиента"


async def test_history_import_does_not_order_more_history(monkeypatch, db, redis, account) -> None:
    """Массовая загрузка не заказывает догрузку того, что грузит прямо сейчас.

    Без этой границы каждый импортированный чат ставил бы задачу импортировать
    тот же чат — на тысяче чатов это тысяча лишних пар запросов к Авито и
    очередь, забитая собственным эхом.
    """
    from app.services import avito_accounts as accounts_svc
    from app.services.inbound import apply_inbound_event

    заказы: list[tuple] = []

    async def fake_enqueue(*args):
        заказы.append(args)

    monkeypatch.setattr(accounts_svc, "enqueue_conversation_history", fake_enqueue)

    событие = InboundEvent(
        external_chat_id="чат-архивный",
        external_message_id="старое-1",
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text="прошлогоднее",
        created_at=NOW - timedelta(days=300),
        client_name="Клиент",
    )
    assert await apply_inbound_event(db, redis, account, событие, backfill=True) is True
    assert заказы == [], "импорт заказал догрузку сам себе"


def test_inbound_is_wired_to_order_history() -> None:
    """ПРОВОДКА. Без неё все остальные проверки зеленеют впустую.

    Задачу можно написать, зарегистрировать в воркере и не позвать ни разу —
    этот проект уже несколько раз попадался ровно на такой дыре.
    """
    import inspect
    import re

    from app.services import inbound as inbound_svc

    src = inspect.getsource(inbound_svc.apply_inbound_event)
    src = re.sub(r"#[^\n]*", " ", src)
    assert "enqueue_conversation_history" in src, (
        "вход не заказывает историю нового чата — диалог останется с одной строкой"
    )
    assert re.search(r"if conv_created and not backfill:", src), (
        "история заказывается не только на СОЗДАНИИ живого диалога: на каждом "
        "сообщении это лишний поход в Авито, а на импорте — задача импортировать "
        "то, что импортируется прямо сейчас"
    )


def test_worker_knows_the_job() -> None:
    """Задача зарегистрирована в воркере.

    Без строки в `WorkerSettings.functions` задача ставится в очередь и висит в
    pending: история не грузится, а виноватого не видно нигде.
    """
    from app.workers.main import WorkerSettings

    имена = {getattr(f, "__name__", "") for f in WorkerSettings.functions}
    assert "backfill_conversation" in имена


def test_scheduler_supervises_the_backfill() -> None:
    """Сторож догрузки зарегистрирован в расписании.

    Без этой строки догрузка снова становится ручной — а именно от ручной
    владелец и отказался: двадцать каналов из тридцати не начинали загрузку
    ни разу.
    """
    from app.scheduler.main import build_scheduler

    ids = {job.id for job in build_scheduler().get_jobs()}
    assert "backfill_supervise" in ids


# --------------------------------------------------- решения сторожа догрузки


def _ход(**kw) -> dict:
    состояние = {"phase": "loading", "updated_at": NOW.isoformat()}
    состояние.update(kw)
    return состояние


def test_supervisor_leaves_a_live_run_alone() -> None:
    """Заход идёт прямо сейчас — не мешаем.

    Второй заход по тому же каналу ничего не сломает (множество разобранных
    чатов общее), но зря сходит за списком чатов: на канале с тысячей чатов это
    одиннадцать лишних запросов к Авито каждые пять минут.
    """
    from app.scheduler.main import _заход_живой

    assert _заход_живой(_ход(), NOW + timedelta(seconds=30)) is True


def test_supervisor_revives_a_dead_run() -> None:
    """Ход работы застыл — заход умер, поднимаем.

    Ровно этот случай и был в бою: воркер убивал заход таймаутом, ключ
    оставался в фазе «loading» с застывшим временем, и НИЧТО его не
    возобновляло. Десять каналов простояли так с прошлого вечера.
    """
    from app.scheduler.main import BACKFILL_STALE_AFTER, _заход_живой

    поздно = NOW + BACKFILL_STALE_AFTER + timedelta(seconds=1)
    assert _заход_живой(_ход(), поздно) is False


def test_supervisor_waits_out_the_census() -> None:
    """Перепись длинная и своего хода не двигает.

    По времени обновления она неотличима от застывшей загрузки — а перезапуск
    посреди переписи заставил бы канал считать чаты заново, и так по кругу.
    """
    from app.scheduler.main import BACKFILL_STALE_AFTER, _заход_живой

    поздно = NOW + BACKFILL_STALE_AFTER + timedelta(hours=1)
    assert _заход_живой(_ход(phase="census"), поздно) is True


def test_supervisor_survives_a_broken_progress_key() -> None:
    """Ход работы старого формата или битый — не повод молчать вечно.

    Прежний формат ключа — голое число без времени обновления. Сочти мы такой
    ход живым, канал не поднялся бы никогда; поэтому «времени нет» читается как
    «заход умер».
    """
    from app.scheduler.main import _заход_живой

    assert _заход_живой({"phase": "loading"}, NOW) is False
    assert _заход_живой(_ход(updated_at="не дата"), NOW) is False


async def test_supervisor_picks_exactly_the_unfinished_channels(
    monkeypatch, db_sessionmaker, redis, make_avito_account
) -> None:
    """НАСТОЯЩИЙ ПРОГОН СТОРОЖА: кого он поднимает, а кого нет.

    ⚠ ПЕРВАЯ РЕДАКЦИЯ ЭТОЙ ПРОВЕРКИ СВЕРЯЛА ИСХОДНИК на вхождение слов
    «history_loaded_at» и «_stop_key». Диверсия «убрать условие выборки»
    её НЕ РОНЯЛА: слова остаются в тексте функции и после того, как условие из
    неё убрано. Проверка исходником годится на проводку («вызов вообще есть»),
    но не на решение — решение проверяется прогоном.

    Три канала на входе:
      · «незагруженный» — история не грузилась ни разу: обязан подняться;
      · «загруженный» — `history_loaded_at` стоит: трогать нечего;
      · «остановленный» — человек нажал «Остановить»: его решение старше нашего.
    """
    import contextlib

    from app.scheduler import main as sched
    from app.services.avito_accounts import _stop_key

    незагруженный = await make_avito_account(770851)
    загруженный = await make_avito_account(770852)
    остановленный = await make_avito_account(770853)

    async with db_sessionmaker() as db:
        row = await db.get(type(загруженный), загруженный.id)
        row.history_loaded_at = NOW
        row.history_loaded_depth = "all"
        await db.commit()
    await redis.set(_stop_key(остановленный.id), "1")

    поставленные: list[tuple] = []

    class FakePool:
        async def enqueue_job(self, name, *args, **kw):
            поставленные.append((name, args, kw.get("_job_id")))

    @contextlib.asynccontextmanager
    async def fake_scope():
        async with db_sessionmaker() as db:
            yield db

    monkeypatch.setattr(sched, "arq_pool", FakePool())
    monkeypatch.setattr(sched.db_mod, "session_scope", fake_scope)
    monkeypatch.setattr(sched.redis_mod, "get_client", lambda: redis)

    await sched.enqueue_backfill_supervise()

    поднятые = {args[0] for _name, args, _jid in поставленные}
    assert незагруженный.id in поднятые, (
        "канал, у которого история не грузилась ни разу, не поднят — а таких в бою "
        "было двадцать из тридцати"
    )
    assert загруженный.id not in поднятые, (
        "сторож перезапускает законченную загрузку — она пойдёт по кругу навсегда"
    )
    assert остановленный.id not in поднятые, (
        "сторож отменяет решение человека: кнопка «Остановить» перестанет работать, "
        "и не будет видно почему"
    )
    assert all(name == "backfill_account" for name, _a, _j in поставленные)


async def test_supervisor_does_not_flood_the_worker(
    monkeypatch, db_sessionmaker, redis, make_avito_account
) -> None:
    """За тик поднимается не больше `BACKFILL_SUPERVISE_BATCH` каналов.

    Заходы загрузки идут в том же воркере, что доставка исходящих. Подними
    сторож все тридцать разом — история заняла бы пул, и ответы клиентам встали
    бы в очередь за прошлогодней перепиской.
    """
    import contextlib

    from app.scheduler import main as sched

    for uid in range(770861, 770861 + sched.BACKFILL_SUPERVISE_BATCH + 3):
        await make_avito_account(uid)

    поставленные: list[tuple] = []

    class FakePool:
        async def enqueue_job(self, name, *args, **kw):
            поставленные.append((name, args))

    @contextlib.asynccontextmanager
    async def fake_scope():
        async with db_sessionmaker() as db:
            yield db

    monkeypatch.setattr(sched, "arq_pool", FakePool())
    monkeypatch.setattr(sched.db_mod, "session_scope", fake_scope)
    monkeypatch.setattr(sched.redis_mod, "get_client", lambda: redis)

    await sched.enqueue_backfill_supervise()

    assert len(поставленные) == sched.BACKFILL_SUPERVISE_BATCH


def test_supervisor_batch_is_small() -> None:
    """Порция намеренно маленькая: две трети пула всегда свободны для живой работы."""
    from app.scheduler.main import BACKFILL_SUPERVISE_BATCH

    assert 0 < BACKFILL_SUPERVISE_BATCH < 10
