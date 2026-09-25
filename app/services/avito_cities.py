"""Города Авито: слаг из ссылки → русское название.

ОТКУДА ЭТИ ДАННЫЕ. Словарь собран в проекте лид-бота по живому корпусу
(13 852 диалога Jivo) и скопирован оттуда — `brain/avito_city.py`. Это ВНЕШНИЙ
факт площадки, а не наша логика: как Авито называет города в ссылках, мы не
решаем и повлиять на это не можем.

ПОЧЕМУ КОПИЯ, А НЕ ОБЩИЙ ИСТОЧНИК. Системы живут на разных серверах и в разных
репозиториях; тянуть справочник по сети ради статической таблицы значило бы
поставить создание заявки в зависимость от доступности чужой службы. Цена копии
известна и ограничена: разойтись они могут только новым городом, и такой город
здесь не теряется молча — лид с нераспознанным городом ПРИДЕРЖИВАЕТСЯ и виден в
списке отложенных с причиной (`app/services/leads.py`).

ЗАЧЕМ ЭТО ВООБЩЕ НУЖНО. Заявка в лид-центре без города не создаётся, а у нас в
диалоге лежит слаг (`conversations.item_city_slug`): `abakan`, `bryansk`,
`gatchina`. Расширение «Автозаявки» переводит НАЗВАНИЕ в свой внутренний id по
справочнику живой формы, и название обязано быть русским.
"""

import re

CITY_BY_SLUG: dict[str, str] = {
    "abakan": "Абакан",
    "achinsk": "Ачинск",
    "amurskaya_oblast_blagoveschensk": "Благовещенск",
    "anapa": "Анапа",
    "angarsk": "Ангарск",
    "aprelevka": "Апрелевка",
    "armavir": "Армавир",
    "astrahan": "Астрахань",
    "balashiha": "Балашиха",
    "barnaul": "Барнаул",
    "belgorod": "Белгород",
    "berdsk": "Бердск",
    "berezniki": "Березники",
    "biysk": "Бийск",
    "bryansk": "Брянск",
    "chelyabinsk": "Челябинск",
    "cherepovets": "Череповец",
    "chita": "Чита",
    "dmitrov": "Дмитров",
    "dolgoprudnyy": "Долгопрудный",
    "domodedovo": "Домодедово",
    "ekaterinburg": "Екатеринбург",
    "elektrostal": "Электросталь",
    "elets": "Елец",
    "engels": "Энгельс",
    "feodosiya": "Феодосия",
    "gatchina": "Гатчина",
    "gelendzhik": "Геленджик",
    "gubkin": "Губкин",
    "habarovsk": "Хабаровск",
    "himki": "Химки",
    "irkutsk": "Иркутск",
    "ivanovo": "Иваново",
    "izhevsk": "Ижевск",
    "kaliningrad": "Калининград",
    "kaluga": "Калуга",
    "kazan": "Казань",
    "kemerovo": "Кемерово",
    "kerch": "Керчь",
    "kirovskaya_oblast_kirov": "Киров",
    "kislovodsk": "Кисловодск",
    "klin": "Клин",
    "kommunarka": "Коммунарка",
    "korolev": "Королёв",
    "kostroma": "Кострома",
    "kovrov": "Ковров",
    "krasnodar": "Краснодар",
    "krasnokamsk": "Краснокамск",
    "krasnoyarsk": "Красноярск",
    "krasnoyarskiy_kray_sosnovoborsk": "Сосновоборск",
    "krymsk": "Крымск",
    "kursk": "Курск",
    "leninsk-kuznetskiy": "Ленинск-Кузнецкий",
    "lipetsk": "Липецк",
    "magnitogorsk": "Магнитогорск",
    "maykop": "Майкоп",
    "moskovskaya_oblast_chehov": "Чехов",
    "moskovskaya_oblast_krasnogorsk": "Красногорск",
    "moskva": "Москва",
    "moskva_zelenograd": "Зеленоград",
    "murino": "Мурино",
    "murmansk": "Мурманск",
    "mytischi": "Мытищи",
    "naberezhnye_chelny": "Набережные Челны",
    "nefteyugansk": "Нефтеюганск",
    "nevinnomyssk": "Невинномысск",
    "nizhnekamsk": "Нижнекамск",
    "nizhnevartovsk": "Нижневартовск",
    "nizhniy_novgorod": "Нижний Новгород",
    "nizhniy_tagil": "Нижний Тагил",
    "norilsk": "Норильск",
    "novocherkassk": "Новочеркасск",
    "novokuybyshevsk": "Новокуйбышевск",
    "novokuznetsk": "Новокузнецк",
    "novorossiysk": "Новороссийск",
    "novosibirsk": "Новосибирск",
    "odintsovo": "Одинцово",
    "omsk": "Омск",
    "orehovo-zuevo": "Орехово-Зуево",
    "orel": "Орёл",
    "orenburg": "Оренбург",
    "orsk": "Орск",
    "perm": "Пермь",
    "petropavlovsk-kamchatskiy": "Петропавловск-Камчатский",
    "podolsk": "Подольск",
    "prokopevsk": "Прокопьевск",
    "pskov": "Псков",
    "pyatigorsk": "Пятигорск",
    "ramenskoe": "Раменское",
    "reutov": "Реутов",
    "rostov-na-donu": "Ростов-на-Дону",
    "ryazan": "Рязань",
    "rybinsk": "Рыбинск",
    "salavat": "Салават",
    "samara": "Самара",
    "sankt-peterburg": "Санкт-Петербург",
    "sankt-peterburg_kolpino": "Колпино",
    "sankt-peterburg_krasnoye_selo": "Красное Село",
    "sankt-peterburg_kronstadt": "Кронштадт",
    "saransk": "Саранск",
    "saratov": "Саратов",
    "sergiev_posad": "Сергиев Посад",
    "serpuhov": "Серпухов",
    "sertolovo": "Сертолово",
    "sevastopol": "Севастополь",
    "shahty": "Шахты",
    "simferopol": "Симферополь",
    "slavyansk-na-kubani": "Славянск-на-Кубани",
    "smolensk": "Смоленск",
    "sochi": "Сочи",
    "solnechnogorsk": "Солнечногорск",
    "staryy_oskol": "Старый Оскол",
    "stavropol": "Ставрополь",
    "sterlitamak": "Стерлитамак",
    "surgut": "Сургут",
    "syktyvkar": "Сыктывкар",
    "syzran": "Сызрань",
    "taganrog": "Таганрог",
    "tambov": "Тамбов",
    "tolyatti": "Тольятти",
    "tomsk": "Томск",
    "tuapse": "Туапсе",
    "tula": "Тула",
    "tver": "Тверь",
    "tyumen": "Тюмень",
    "ufa": "Уфа",
    "ulan-ude": "Улан-Удэ",
    "ulyanovsk": "Ульяновск",
    "velikiy_novgorod": "Великий Новгород",
    "verhnyaya_pyshma": "Верхняя Пышма",
    "vidnoe": "Видное",
    "vladimir": "Владимир",
    "vladivostok": "Владивосток",
    "volgograd": "Волгоград",
    "volgogradskaya_oblast_volzhskiy": "Волжский",
    "vologda": "Вологда",
    "voronezh": "Воронеж",
    "yalta": "Ялта",
    "yaroslavl": "Ярославль",
    "yuzhno-sahalinsk": "Южно-Сахалинск",
    "zvenigorod": "Звенигород",
    # ── ДОБРАНО СКАНОМ КОРПУСА 01.08.2026 (TERR-6/TERR-8) ────────────────────────────
    # Город не опознан → бот молча считает клиента «в черте города» и берёт РЕГИОНАЛЬНЫЙ
    # столбец прайса. По корпусу таких было 347 диалогов на 58 слагах, и среди них
    # ОСНОВНЫЕ ФИЛИАЛЫ: Чебоксары, Люберцы, Курган, Якутск, Братск, Йошкар-Ола, Волгодонск.
    # ⚠ Подмосковные и питерские пригороды дают ещё и МОСКОВСКИЙ/питерский столбец —
    # ошибка здесь стоит клиенту денег, а нам спора на месте.
    "kolomna": "Коломна",
    "obninsk": "Обнинск",
    "novoaltaysk": "Новоалтайск",
    "kurgan": "Курган",
    "schelkovo": "Щёлково",
    "cheboksary": "Чебоксары",
    "votkinsk": "Воткинск",
    "bratsk": "Братск",
    "yakutsk": "Якутск",
    "noginsk": "Ногинск",
    "dzerzhinsk": "Дзержинск",
    "lyubertsy": "Люберцы",
    "evpatoriya": "Евпатория",
    "volgodonsk": "Волгодонск",
    "bashkortostan_oktyabrskiy": "Октябрьский",
    "istra": "Истра",
    "naro-fominsk": "Наро-Фоминск",
    "fryazino": "Фрязино",
    "aleksandrov": "Александров",
    "yoshkar-ola": "Йошкар-Ола",
    "komsomolsk-na-amure": "Комсомольск-на-Амуре",
    "gorno-altaysk": "Горно-Алтайск",
    "rubtsovsk": "Рубцовск",
    "kubinka": "Кубинка",
    "vsevolozhsk": "Всеволожск",
    "kurchatov": "Курчатов",
    "voskresensk": "Воскресенск",
    "kurskaya_oblast_zheleznogorsk": "Железногорск",
    "zelenodolsk": "Зеленодольск",
    "tuchkovo": "Тучково",
    "balakovo": "Балаково",
    "pushkino": "Пушкино",
    "novomoskovsk": "Новомосковск",
    "dedovsk": "Дедовск",
    "novotroitsk": "Новотроицк",
    "elista": "Элиста",
    "lobnya": "Лобня",
    "elektrogorsk": "Электрогорск",
    "moskovskaya_oblast_troitsk": "Троицк",
    "elabuga": "Елабуга",
    "krasnoarmeysk": "Красноармейск",
    "pavlovskiy_posad": "Павловский Посад",
    "hotkovo": "Хотьково",
    "moskovskaya_oblast_ivanteevka": "Ивантеевка",
    "kotelniki": "Котельники",
    "monino": "Монино",
    "vyborg": "Выборг",
    "tomilino": "Томилино",
    "solikamsk": "Соликамск",
    "zhukovskiy": "Жуковский",
    # пригороды Санкт-Петербурга (в ссылке идут с префиксом города)
    "sankt-peterburg_peterhof": "Петергоф",
    "sankt-peterburg_sestroretsk": "Сестрорецк",
    "sankt-peterburg_pushkin": "Пушкин",
    "sankt-peterburg_lomonosov": "Ломоносов",
    "bugry": "Бугры",
    "kudrovo": "Кудрово",
    "novoe_devyatkino": "Новое Девяткино",
    "yanino-1": "Янино-1",
}

#: Слаги, которые городом не являются (разделы Авито). Встретив такой,
#: города мы НЕ знаем — и это честнее, чем подставить первый попавшийся.
NON_CITY: frozenset[str] = frozenset({"profile", "brands", "user", "items", "web"})


def city_by_slug(slug: str | None) -> str | None:
    """Русское название города по слагу Авито. `None` — не распознали.

    ЗАПАСНОЙ РАЗБОР ДЛЯ СОСТАВНЫХ СЛАГОВ. У городов, чьи имена повторяются в
    разных регионах, Авито ставит префикс: `amurskaya_oblast_blagoveschensk`,
    `moskva_zelenograd`. Берём последний сегмент — он и есть город.

    `None`, а не пустая строка и не «Москва по умолчанию»: незнакомый город
    обязан остановить заявку, а не увести мастера в другой конец страны.
    """
    key = (slug or "").strip().lower()
    if not key or key in NON_CITY:
        return None
    known = CITY_BY_SLUG.get(key)
    if known:
        return known
    tail = re.split(r"_(?:oblast|kray|respublika|ao|kraj)_", key)[-1]
    if tail in CITY_BY_SLUG:
        return CITY_BY_SLUG[tail]
    tail = key.rsplit("_", 1)[-1]
    return CITY_BY_SLUG.get(tail)
