"""Чистые правила автоматики номеров (12.09): вердикт, похожесть, подсказка, имена.

ДИВЕРСИИ (каждая обязана краснеть): убрать проверку «наш» до выбора → «наш +
свой» на пустой карточке отдаст основным наш; разрешить префикс имён →
«Александр» и «Александра» станут равными; убрать границу предложения у
подсказки → «мастер» из другой фразы подпишет номер.
"""

from app.services import phone_parse
from app.services import phone_rules as r


def test_наш_и_8_800_отсекаются_до_выбора_основного() -> None:
    """«Звонил вам на 8 800…, мой 8 900…» — основным становится 8 900."""
    own = frozenset({"+79001112243"})
    assert r.classify("+78005553535", index=0, primary=None, own=own, autofill=True).kind == (
        r.SKIP_TOLL_FREE
    )
    assert r.classify("+79001112243", index=0, primary=None, own=own, autofill=True).kind == (
        r.SKIP_OWN
    )
    # Индекс считает вызывающий среди НЕ пропущенных: первый годный — основной.
    assert r.classify("+79001112251", index=0, primary=None, own=own, autofill=True).kind == (
        r.FILL_PRIMARY
    )
    assert r.classify("+79001112252", index=1, primary=None, own=own, autofill=True).kind == (
        r.EXTRA
    )


def test_заполненный_основной_не_меняется_а_повтор_молчит() -> None:
    v = r.classify("+79001112252", index=0, primary="+79001112251", own=frozenset(), autofill=True)
    assert v.kind == r.EXTRA and v.reason == "card_filled"
    v = r.classify("+79001112251", index=0, primary="+79001112251", own=frozenset(), autofill=True)
    assert v.kind == r.ALREADY_PRIMARY
    # Запись выключена — только вопрос оператору, как до 12.09.
    v = r.classify("+79001112252", index=0, primary=None, own=frozenset(), autofill=False)
    assert v.kind == r.PENDING


def test_похожие_номера_одна_цифра_или_перестановка() -> None:
    assert r.near_duplicate("+79001112245", "+79001112255")
    assert r.near_duplicate("+79001112245", "+79001112254")  # соседние переставлены
    assert not r.near_duplicate("+79001112245", "+79001112245")
    assert not r.near_duplicate("+79001112245", "+79001112253")
    # Вымышленная серия 111-22-4x/5x меняется только в двух последних цифрах, и
    # перестановку «через одну» в ней не построить; кода 009 не бывает вовсе.
    assert not r.near_duplicate("+79001112245", "+70091112245")  # не соседние
    assert not r.near_duplicate(None, "+79001112245")
    assert not r.near_duplicate("+7900111224", "+79001112245")


def test_подсказка_берёт_слово_рядом_и_не_из_другой_фразы() -> None:
    def подсказки(текст: str) -> list[str | None]:
        found = phone_parse.find_all(текст)
        return [
            r.hint_word(
                текст,
                f.start,
                f.end,
                prev_end=found[i - 1].end if i else 0,
                next_start=found[i + 1].start if i + 1 < len(found) else None,
            )
            for i, f in enumerate(found)
        ]

    assert подсказки("мой 89001112251, жены 89001112252") == [None, "жены"]
    assert подсказки("Жена: 8 900 111 22 52") == ["жена"]
    assert подсказки("89001112251 это мастер") == ["мастер"]
    assert подсказки("Мастер сказал перезвонить. Мой номер 89001112251") == [None]
    assert подсказки("перезвоните 89001112251") == [None]


def test_наши_номера_разбираются_одной_функцией_и_терпят_мусор() -> None:
    assert r.parse_own_numbers("8 (900) 111-22-51; +79001112252, мусор\n89001112243") == {
        "+79001112251",
        "+79001112252",
        "+79001112243",
    }
    assert r.parse_own_numbers(None) == frozenset()


def test_имена_равны_только_точь_в_точь() -> None:
    assert r.names_equal(" Иван  Петров", "иван петров")
    assert not r.names_equal("Александр", "Александра")
    assert not r.names_equal("Иван", "Иван Петров")
    assert not r.names_equal("", "")
    assert not r.names_equal(None, "Иван")
