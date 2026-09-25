"""Сторож миграции 0069: кого она забирает из истории, кого обязана оставить.

⚠ ЖАЛОБА ВЛАДЕЛЬЦА 07.09: пузыри «Видео — посмотреть можно только в приложении
Авито» и «Файл» выглядят словами клиента. Разбор с 07.09 кладёт такую подпись во
вложение; миграция 0069 переводит туда же 3276 накопленных записей.

Проверяется здесь ОТБОР — место, где ошибка стоит дороже всего. Взять лишнее
значит стереть слова живого человека из ленты, из поиска и из карточки заявки;
взять недостаточно — оставить жалобу в силе. Перенос и откат тоже гоняются
по-настоящему: миграция написана кроссдиалектно (`sa.case` над JSON-литералами,
без `jsonb_build_*`), поэтому на SQLite исполняются те же самые функции, что
поедут в бой. Партиции, генерируемая колонка `search` и настоящий
`alembic downgrade` — за интеграционным набором.

ДИВЕРСИИ (каждая проведена, результат в отчёте):

* из `отбор()` убрано `attachments == []` -> красный на
  `test_stroku_s_vlozheniem_ne_trogaem`;
* из `отбор()` убраны ОБА условия про автора (`direction` и `sender_type`) ->
  красный на `test_seryy_chip_avito_ostayotsya_na_meste` и
  `test_ishodyashchee_operatora_ne_trogaem`. ⚠ Одного мало: сломав только
  `sender_type`, серый чип не откроешь — его держит ещё и `direction='system'`.
  Проверено отдельным прогоном: тот сторож остался зелёным;
* в `вложение()` `kind` переписан на «image» -> красный на
  `test_forma_vlozheniya_sovpadaet_s_razborom`;
* в `ПОДПИСИ` «Файл» переписан как «Фаил» (то же число букв) -> красный на
  `test_spisok_podpisey_sovpadaet_s_adapterom`;
* в `отбор_отката()` полное сравнение массива заменено на `attachments != []`
  -> красный на `test_otkat_ne_est_nastoyashchee_vlozhenie`;
* точное равенство `body IN (...)` заменено на `LIKE «начинается с»` -> красный
  на `test_slova_klienta_ryadom_s_podpisyu_ne_beryom` (ревью);
* имя вложения считается по виду, а не берётся из тела -> красный на
  `test_forma_vlozheniya_sovpadaet_s_razborom` и на круге
  `test_perenos_vozvrashchaet_podpis_bayt_v_bayt` (ревью);
* ⚠ из `ПОДПИСЕЙ` убрана «Клиент звонил через приложение Авито» — 2834 боевые
  строки остались бы речью клиента, а набор был ЗЕЛЁНЫМ: проверки перебирают
  сам список. Дыра закрыта `test_ni_odna_boevaya_podpis_ne_zabyta` (ревью);
* из `перенести()` убрано `attachments=выбор` (тело обнулено, вложения нет —
  беда 11.08) -> красный на `test_perenesyonnoe_ne_vypadaet_iz_istorii_bota`
  (ревью).

`__pycache__` миграции снесён перед каждым прогоном: диверсия словом той же
длины оставляет старый `.pyc` и врёт в обе стороны.
"""

import datetime as dt
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.bots.engine import ScenarioEngine
from app.integrations.avito.adapter import (
    _ATTACHMENT_LABELS,
    _CONTENT_FREE_KINDS,
    _placeholder_attachment,
)
from app.models import Message

МИГРАЦИЯ = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "0069_placeholder_to_attachment.py"
)


def _миграция():  # noqa: ANN202 — модуль без объявленного интерфейса
    spec = importlib.util.spec_from_file_location("migration_0069", МИГРАЦИЯ)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m0069 = _миграция()

#: Внутри окна правки и заведомо после границы (март 2026).
В_ОКНЕ = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)

#: Настоящее вложение фотографии, у которой не разобралась ссылка. Живой случай:
#: имя `_extract_attachments` берёт из того же словаря, что и подпись, а `url`
#: у него отсутствует. От нашей заглушки отличается только `media_id`.
НАСТОЯЩЕЕ_ФОТО: dict[str, Any] = {
    "media_id": "avito_image_9911",
    "kind": "image",
    "name": "Фотография",
    "size": None,
    "avito_type": "image",
}


#: Замер боя 07.09, снятый ЧТЕНИЕМ и повторённый на ревью: подпись -> сколько
#: строк её ждёт. Все 3276 — `direction='in'`, `sender_type='client'`,
#: `attachments = '[]'`; строк с `attachments IS NULL` нет ни одной (колонка
#: `NOT NULL`), раньше границы по дате — тоже ни одной.
БОЕВЫЕ_СТРОКИ: dict[str, int] = {
    "Клиент звонил через приложение Авито": 2834,
    "Видео": 348,
    "Файл": 66,
    "Видео — посмотреть можно только в приложении Авито": 25,
    "Фотография": 3,
}


def _строка(
    *,
    body: str | None,
    direction: str = "in",
    sender_type: str = "client",
    attachments: list[dict[str, Any]] | None = None,
    created_at: dt.datetime = В_ОКНЕ,
    conversation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "conversation_id": conversation_id or uuid.uuid4(),
        "external_message_id": uuid.uuid4().hex[:12],
        "direction": direction,
        "sender_type": sender_type,
        "sender_user_id": None,
        "body": body,
        "attachments": attachments if attachments is not None else [],
        "delivery_status": "delivered",
        "reply_to_id": None,
        "reply_to_created_at": None,
        "voice_transcript": None,
        "voice_transcript_status": None,
        "created_at": created_at,
    }


async def _засеять(engine: AsyncEngine, *строки: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.insert(Message.__table__), list(строки))


async def _отобранные(engine: AsyncEngine) -> set[uuid.UUID]:
    async with engine.connect() as conn:
        строки = await conn.execute(sa.select(m0069.messages.c.id).where(m0069.отбор()))
        return {r[0] for r in строки}


async def _тело_и_вложения(
    engine: AsyncEngine, ключ: uuid.UUID
) -> tuple[str | None, list[dict[str, Any]]]:
    async with engine.connect() as conn:
        строка = (
            await conn.execute(
                sa.select(m0069.messages.c.body, m0069.messages.c.attachments).where(
                    m0069.messages.c.id == ключ
                )
            )
        ).one()
    return строка[0], строка[1]


# ------------------------------------------------------------------ что берём


@pytest.mark.parametrize("подпись", sorted(m0069.ПОДПИСИ))
async def test_kazhdaya_podpis_iz_spiska_popadaet_v_perenos(
    engine: AsyncEngine, подпись: str
) -> None:
    """Все пять накопленных подписей отбираются — ни одна не забыта.

    Список не выдуман: это ровно те тексты, что лежат на бою в теле входящих
    сообщений клиента (замер 07.09 — 3276 строк, от 3 «Фотографий» до 2834
    записей о звонке).
    """
    свой = _строка(body=подпись)
    await _засеять(engine, свой)
    assert await _отобранные(engine) == {свой["id"]}


# --------------------------------------------------------------- что НЕ берём


async def test_stroku_s_vlozheniem_ne_trogaem(engine: AsyncEngine) -> None:
    """Граница (д): у кого вложение уже есть — того не касаемся.

    Случай живой, а не выдуманный: `_extract_attachments` подставляет имя из
    того же словаря, когда своего имени у файла нет. Настоящая фотография
    клиента лежит в базе с именем «Фотография» и отличается от нашей подписи
    ровно тем, что она ВЛОЖЕНИЕ, а не тело.
    """
    await _засеять(engine, _строка(body="Фотография", attachments=[НАСТОЯЩЕЕ_ФОТО]))
    assert await _отобранные(engine) == set()


async def test_seryy_chip_avito_ostayotsya_na_meste(engine: AsyncEngine) -> None:
    """Служебная запись Авито о звонке — не наша забота, и трогать её опасно.

    На бою таких 24 290 против 2834 наших (замер 07.09). Живой разбор оставляет
    ей подпись ТЕЛОМ намеренно (`adapter::_подпись_пустому`, ветка `служебная`):
    серый чип вложений не показывает вовсе, а бот считает по слову в её теле
    звонки клиента. Обнулив ей тело, мы сломали бы работающее ради жалобы,
    которой к ней нет.
    """
    await _засеять(
        engine,
        _строка(
            body="Клиент звонил через приложение Авито",
            direction="system",
            sender_type="avito",
        ),
    )
    assert await _отобранные(engine) == set()


async def test_ishodyashchee_operatora_ne_trogaem(engine: AsyncEngine) -> None:
    """Исходящих с такой подписью на бою три; это наши слова, а не наш разбор."""
    await _засеять(engine, _строка(body="Видео", direction="out", sender_type="operator"))
    assert await _отобранные(engine) == set()


async def test_do_granicy_po_date_ne_zaglyadyvaem(engine: AsyncEngine) -> None:
    """Граница (а): за март 2026 миграция не заходит.

    Не из осторожности, а по замеру: раньше марта подходящих строк на бою нет ни
    одной, а условие по `created_at` отсекает двадцать партиций из двадцати
    восьми — это и есть разница между узкой правкой и проходом по всей таблице в
    377 493 строки.
    """
    await _засеять(
        engine, _строка(body="Видео", created_at=dt.datetime(2026, 2, 28, tzinfo=dt.UTC))
    )
    assert await _отобранные(engine) == set()


async def test_slova_klienta_ryadom_s_podpisyu_ne_beryom(engine: AsyncEngine) -> None:
    """Отбор — точное равенство, а не «похоже на служебное».

    Четверо соседей, каждый из которых попался бы на `LIKE` или на «начинается
    с»: подпись внутри фразы, она же с хвостом, подпись адаптера, которой в
    списке нет намеренно, и пустое тело без вложений.
    """
    await _засеять(
        engine,
        _строка(body="Видео пришлю позже, сейчас неудобно"),
        _строка(body="Видео!"),
        _строка(body="Голосовое сообщение"),
        _строка(body=None),
    )
    assert await _отобранные(engine) == set()


# ----------------------------------------------------------------- край: слово


async def test_klient_napisavshiy_slovo_podpisi_pereezzhaet_tozhe(engine: AsyncEngine) -> None:
    """⚠ КРАЙ, РЕШЁННЫЙ ЗАМЕРОМ: клиент, написавший ровно «Файл», переедет.

    Отличить его нечем — в базе его строка совпадает с нашей подписью до байта.
    Решение принято по данным, а не по вкусу:

    * из 3276 боевых строк 256 попадают в 30-дневное окно `webhook_raw_log`, и у
      ВСЕХ 256 вид события служебный (`video`, `appCall`, `file`, `image`) — ни
      одной с `type='text'`;
    * за всё окно журнала нет НИ ОДНОГО вебхука, где `content.text` равен любой
      из десяти подписей адаптера. Этими словами клиенты не пишут.

    И цена ошибки несимметрична. Слово клиента при переносе не пропадает: оно
    остаётся именем вложения, видно в ленте («📎 Файл») и уходит в историю бота
    строкой вложений. Теряется одно — находимость этого слова поиском. Против
    этого — 3276 строк, каждая из которых сегодня выглядит речью клиента.
    """
    свой = _строка(body="Файл")
    await _засеять(engine, свой)
    assert await _отобранные(engine) == {свой["id"]}


# ------------------------------------------------------- перенос и его откат


async def test_perenos_vozvrashchaet_podpis_bayt_v_bayt(engine: AsyncEngine) -> None:
    """Перенос -> откат -> исходное состояние, и оба шага идемпотентны (в, г).

    ⚠ ИМЯ ВЛОЖЕНИЯ — ТА ЖЕ СТРОКА, ЧТО БЫЛА В ТЕЛЕ, а не пересчитанная сегодняшним
    разбором. Иначе 348 записей «Видео» вернулись бы из отката новой
    формулировкой, и откат перестал бы быть откатом.
    """
    свои = {п: _строка(body=п) for п in m0069.ПОДПИСИ}
    await _засеять(engine, *свои.values())

    async with engine.begin() as conn:
        assert await conn.run_sync(m0069.перенести) == len(свои)
    for подпись, строка in свои.items():
        тело, вложения = await _тело_и_вложения(engine, строка["id"])
        assert тело is None
        assert вложения == [m0069.вложение(подпись)]

    async with engine.begin() as conn:
        assert await conn.run_sync(m0069.перенести) == 0, "повтор завёл второе вложение"

    async with engine.begin() as conn:
        assert await conn.run_sync(m0069.вернуть) == len(свои)
    for подпись, строка in свои.items():
        assert await _тело_и_вложения(engine, строка["id"]) == (подпись, [])

    async with engine.begin() as conn:
        assert await conn.run_sync(m0069.вернуть) == 0, "повтор отката тронул строки"


async def test_otkat_ne_est_nastoyashchee_vlozhenie(engine: AsyncEngine) -> None:
    """Откат не трогает настоящее вложение с тем же именем и без ссылки.

    Разница между ним и заглушкой одна — `media_id`: у настоящего там
    идентификатор Авито, у нашей — слово-метка. Отличать по «имя из списка плюс
    пустой url» значило бы при откате стереть фотографию клиента, у которой
    просто не разобралась ссылка.
    """
    чужое = _строка(body=None, attachments=[НАСТОЯЩЕЕ_ФОТО])
    await _засеять(engine, чужое)
    async with engine.begin() as conn:
        assert await conn.run_sync(m0069.вернуть) == 0
    assert await _тело_и_вложения(engine, чужое["id"]) == (None, [НАСТОЯЩЕЕ_ФОТО])


# ------------------------------------------------ совпадение с разбором Авито


def test_spisok_podpisey_sovpadaet_s_adapterom() -> None:
    """Граница (е): замороженный список — копия словарей `adapter.py`, не выдумка.

    Импортировать словари прямо в миграцию нельзя: она правит ИСТОРИЮ,
    написанную кодом, каким он был. Пример уже случился — 02.09 подпись видео
    сменила текст, и в базе лежат обе формы; импортирующая миграция знала бы
    только новую и молча прошла бы мимо 348 старых строк.

    ⚠ КОГДА СТОРОЖ ПОКРАСНЕЕТ — МИГРАЦИЮ НЕ ПРАВИТЬ. Красный значит, что подпись
    в адаптере переименовали: история хранит старый текст, и решать, нужна ли
    новой форме СЛЕДУЮЩАЯ миграция, должен человек. Ради этого решения сторож и
    написан.
    """
    for подпись, вид in m0069.ПОДПИСИ.items():
        варианты = (_CONTENT_FREE_KINDS.get(вид), _ATTACHMENT_LABELS.get(вид))
        assert any(варианты), f"вида «{вид}» в словарях адаптера нет вовсе"
        assert подпись in варианты, (
            f"подпись «{подпись}» не совпала с тем, что адаптер пишет для «{вид}»: {варианты}"
        )


@pytest.mark.parametrize("подпись", sorted(m0069.ПОДПИСИ))
def test_forma_vlozheniya_sovpadaet_s_razborom(подпись: str) -> None:
    """Граница (е): вложение миграции — поле в поле то же, что кладёт живой разбор.

    Сверяемся с самим `_placeholder_attachment`, а не с переписанным от руки
    словарём: у истории и у новых сообщений обязана быть ОДНА форма, иначе лента
    показывает два разных вложения об одном и том же событии.

    Единственное расхождение объявлено прямо здесь: `name` берётся из тела, а не
    из сегодняшнего `_fallback_body`. Миграция переносит слова, а не переписывает
    их (см. две формы подписи у видео).
    """
    вид = m0069.ПОДПИСИ[подпись]
    assert m0069.вложение(подпись) == _placeholder_attachment(вид) | {"name": подпись}


def test_ni_odna_boevaya_podpis_ne_zabyta() -> None:
    """Список миграции держит ВСЕ пять боевых подписей — счётом, а не по кругу.

    ⚠ ЗАЧЕМ ОТДЕЛЬНО ОТ `test_kazhdaya_podpis_iz_spiska_popadaet_v_perenos`.
    Тот перебирает `m0069.ПОДПИСИ`, то есть проверяет список самим списком:
    убери из миграции строку — и проверка не покраснеет, а просто перестанет
    существовать (случай пойманный: без «Файла» набор дал 17 прогонов вместо 19
    и ни одного падения по этой причине). Спрашивать «все ли на месте» у того
    же списка бессмысленно; здесь второй стороной стоит замер боя.

    Цена пропуска считается строками: забытая «Клиент звонил через приложение
    Авито» оставит 2834 записи выглядеть речью клиента, забытое «Видео» — 348.

    ДИВЕРСИЯ: из `ПОДПИСИ` убрана строка «Файл» -> красный (сверка множеств);
    та же диверсия оставляет `test_kazhdaya_podpis...` зелёным.
    """
    assert set(m0069.ПОДПИСИ) == set(БОЕВЫЕ_СТРОКИ)


async def test_perenesyonnoe_ne_vypadaet_iz_istorii_bota(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """⚠ ЛОВУШКА ПЕРЕНОСА: пустое тело БЕЗ вложения выкидывает запись из истории.

    `ScenarioEngine.load_dialog_for_ai` пропускает сообщение, у которого нет ни
    тела, ни строки вложений (`if not body and not вложения: continue`) — ради
    этого `_attachments_line` и писался: без него из истории выпадала каждая
    двенадцатая реплика клиента (8,8 % по живой выгрузке подсказок 21.08).
    Миграция обнуляет тело у 3276 записей, и если вложение не доедет ЦЕЛЫМ —
    не список, без имени, — бот замолчит о том, что клиент что-то прислал.

    Проверка идёт через настоящий `load_dialog_for_ai` на настоящих строках
    после настоящего `перенести()`, а не сверкой словаря: беда здесь не в форме
    записи, а в том, что читатель её не понял.

    ДИВЕРСИЯ: из `перенести()` убрано `attachments=выбор` (тело обнуляется,
    вложение не появляется — ровно беда 11 августа) -> красный: история
    приезжает пустой.
    """
    диалог = uuid.uuid4()
    свои = {п: _строка(body=п, conversation_id=диалог) for п in m0069.ПОДПИСИ}
    await _засеять(engine, *свои.values())
    async with engine.begin() as conn:
        assert await conn.run_sync(m0069.перенести) == len(свои)

    движок = ScenarioEngine.__new__(ScenarioEngine)
    движок.db = db
    движок.conv = SimpleNamespace(id=диалог)
    история = await движок.load_dialog_for_ai(len(свои))

    # Скрепка вместо слов клиента — то же, что видит бот на настоящем вложении
    # без ссылки. Роль остаётся `user`: сообщение пришло от клиента, изменилось
    # только то, ЧЕМ оно себя называет.
    assert {(x["role"], x["content"]) for x in история} == {("user", f"📎 {п}") for п in свои}
