#!/usr/bin/env python3
"""
ПРЕСЕТ «АМЕТИСТ» — считает примитивы и проверяет их арифметикой, а не глазом.

ЧТО ЗДЕСЬ ПРОИСХОДИТ
  1. Рампа акцента строится в OKLCH: тон один на все одиннадцать ступеней,
     ступень задаётся ЦЕЛЕВОЙ ЯРКОСТЬЮ WCAG (Y), а не «на глаз подобранным»
     hex. Ровно из целевых Y и вытекают пороги: чернила на заливке, белый на
     700/800, текст акцентом на панели.
  2. Тон ищется перебором: аметист обязан быть максимально «фиолетовым» и при
     этом отстоять от --lc-accent (фиолетовый бот) на ΔE ≥ 15 в ОБЕИХ схемах.
     Берётся наименьший тон (ближе к чистому фиолету), у которого запас есть.
  3. Карты токенов собираются так, как их соберёт браузер: тёмный блок пресета
     помечен `:not([data-mantine-color-scheme="light"])` и в светлой схеме НЕ
     применяется — значит светлый блок обязан объявить рампу целиком сам.
  4. Прогоняются все 74 пары PAIRS в обеих схемах, лестница поверхностей,
     запрет «два имени — один цвет», кольцо фокуса в пузыре, выключенная
     кнопка, известные провалы PENDING (они обязаны остаться провалами) и
     матрица ΔE ролей.

Запуск:  python3 amethyst.py            — проверить и записать amethyst.css
         python3 amethyst.py --search   — показать перебор тона
"""
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _engine import (  # noqa: E402
    DELTA_E_MIN,
    EDGE_MIN,
    LADDER_MIN,
    TEXT,
    UI,
    VARS_PATH,
    _blocks,
    composite,
    contrast,
    delta_e,
    flatten,
    hex_for,
    luminance,
    oklab,
    oklch_hex,
    _oklab_to_srgb,
)
from _pairs import LADDER, PAIRS, ROLE_RIVALS, SAME_FORBIDDEN  # noqa: E402

KEY = "amethyst"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, f"{KEY}.css")
TEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "frontend", "src", "test", "tokenContrast.test.ts")

RAW = open(VARS_PATH, encoding="utf-8").read()

# ══════════════════════════ 1. КАРТЫ ТОКЕНОВ ПО-БРАУЗЕРНОМУ ══════════════

def base_maps():
    """Базовые карты: тёмная — блоки `:root`, светлая — они же плюс светлый."""
    dark, light = {}, {}
    for selector, body in _blocks(RAW):
        if "@media" in selector or "data-lc-preset" in selector:
            continue
        is_light = 'color-scheme="light"' in selector
        if not is_light and ":root" not in selector:
            continue
        decls = re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)
        for name, value in decls:
            if is_light:
                light[name] = value.strip()
            else:
                dark[name] = value.strip()
                light.setdefault(name, value.strip())
    # Светлая тема ДОЛЖНА видеть тёмные умолчания: собираем заново по порядку.
    light = {}
    for selector, body in _blocks(RAW):
        if "@media" in selector or "data-lc-preset" in selector:
            continue
        is_light = 'color-scheme="light"' in selector
        if not is_light and ":root" not in selector:
            continue
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
            light[name] = value.strip()
    return dark, light


BASE_DARK, BASE_LIGHT = base_maps()


class Maps:
    """Карта токенов варианта: база + объявления пресета своей схемы."""

    def __init__(self, preset_dark=None, preset_light=None):
        self.dark = dict(BASE_DARK)
        self.light = dict(BASE_LIGHT)
        if preset_dark:
            self.dark.update(preset_dark)
        if preset_light:
            self.light.update(preset_light)

    def raw(self, name, scheme):
        m = self.dark if scheme == "dark" else self.light
        if name not in m:
            raise SystemExit(f"токен {name} не объявлен в схеме {scheme}")
        return m[name]

    def resolve(self, name, scheme, depth=0):
        if depth > 12:
            raise SystemExit(f"циклическая ссылка на {name} ({scheme})")
        raw = self.raw(name, scheme)
        ref = re.fullmatch(r"var\((--[\w-]+)\)", raw)
        return self.resolve(ref.group(1), scheme, depth + 1) if ref else raw


# ══════════════════════════ 2. СВЕРКА ТАБЛИЦЫ ПАР С ТЕСТОМ ═══════════════

def pairs_from_test():
    """Читаем PAIRS прямо из сторожа: список в _pairs.py — копия, а копия врёт."""
    src = open(TEST_PATH, encoding="utf-8").read()
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"//[^\n]*", "", src)
    body = re.search(r"const PAIRS: Pair\[\] = \[([\s\S]*?)\n\];", src)
    if not body:
        raise SystemExit("не нашёл PAIRS в тесте")
    out = []
    for m in re.finditer(
        r'\{\s*ink:\s*"([^"]+)",\s*bg:\s*"([^"]+)",\s*min:\s*(TEXT|UI),'
        r'\s*where:\s*"([^"]*)"(?:,\s*only:\s*"(\w+)")?\s*,?\s*\}',
        body.group(1),
    ):
        out.append((m.group(1), m.group(2), TEXT if m.group(3) == "TEXT" else UI, m.group(4), m.group(5)))
    return out


TEST_PAIRS = pairs_from_test()

# _pairs.py — ручной перенос; сверяем его с оригиналом, иначе проверяем не то.
_mine = [(i, b, mn) for i, b, mn, _ in PAIRS]
_theirs = [(i, b, mn) for i, b, mn, _, _ in TEST_PAIRS]
if _mine != _theirs:
    raise SystemExit(f"_pairs.py разошёлся с тестом: {len(_mine)} против {len(_theirs)}")

ONLY = {(i, b): only for i, b, _, _, only in TEST_PAIRS if only}

PENDING = [
    ("--lc-text-5", "--lc-selected", TEXT, "dark", "время последнего ответа в списке"),
    ("--lc-warn", "--lc-bg-1", UI, "light", "точка «связь просела» в шапке"),
]

# ══════════════════════════ 3. РАМПА АМЕТИСТА ════════════════════════════

STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]

# Целевая яркость WCAG каждой ступени. Числа не «красивые», а вынужденные:
#   600 — заливка ТЁМНОЙ темы под тёмными чернилами: ниже 0.20 чернила не дают 4.5;
#   700/800 — всё СВЕТЛОЙ темы под белым: выше 0.183 белый не даёт 4.5;
#   800 — ещё и исходящий пузырь, а на нём лежит #ffe4e6 «не доставлено» (0.825).
RAMP_Y = {
    50: 0.930,
    100: 0.840,
    200: 0.660,
    300: 0.520,
    400: 0.400,
    500: 0.305,
    600: 0.235,
    700: 0.085,
    800: 0.052,
    900: 0.024,
    950: 0.011,
}
RAMP_C = {
    50: 0.022,
    100: 0.050,
    200: 0.100,
    300: 0.145,
    400: 0.175,
    500: 0.200,
    600: 0.205,
    700: 0.200,
    800: 0.170,
    900: 0.120,
    950: 0.080,
}


def _lin_rgb(L, C, h):
    """OKLCH → ЛИНЕЙНЫЙ sRGB без обрезки: обрезка соврала бы про попадание в гамут."""
    a = C * math.cos(math.radians(h))
    b = C * math.sin(math.radians(h))
    return _oklab_to_srgb(L, a, b)


def _y_of(L, C, h):
    r, g, b = _lin_rgb(L, C, h)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _L_for_y(C, h, y):
    """Светлота OKLab, дающая заданную яркость WCAG: Y растёт по L монотонно."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _y_of(mid, C, h) < y:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def step_hex(hue, step, ramp_y=None):
    """
    Ступень: заданный тон, ЦЕЛЕВАЯ яркость, максимум хромы в пределах sRGB.

    ⚠ ХРОМУ ИЩЕМ ПО ГАМУТУ, А НЕ ПО ПОЛУЧИВШЕМУСЯ HEX. Первая редакция
    сверяла хрому уже округлённого до восьми бит цвета и на 400-й и 600-й
    ступенях сваливалась в серый: округление сдвигает яркость сильнее допуска,
    проверка считала цвет негодным и урезала хрому вдвое.
    """
    y = (ramp_y or RAMP_Y)[step]
    cap = RAMP_C[step]
    lo, hi = 0.0, cap
    for _ in range(40):
        mid = (lo + hi) / 2
        L = _L_for_y(mid, hue, y)
        r, g, b = _lin_rgb(L, mid, hue)
        if all(-1e-6 <= v <= 1 + 1e-6 for v in (r, g, b)):
            lo = mid
        else:
            hi = mid
    return oklch_hex(_L_for_y(lo, hue, y), lo, hue)


def ramp(hue, ramp_y=None):
    return {s: step_hex(hue, s, ramp_y) for s in STEPS}


def tinted(hue, chroma, y):
    return hex_for(hue, chroma, y)


# Поверхности: тон чуть «холоднее» акцента (лиловый подтон, а не заливка им).
SURF_HUE_SHIFT = -14.0


def build(hue, ramp_y=None):
    """Полный набор примитивов пресета для заданного тона акцента."""
    a = ramp(hue, ramp_y)
    sh = hue + SURF_HUE_SHIFT

    dark = {
        **{f"--lc-a-{s}": a[s] for s in STEPS},
        "--lc-a-500-rgb": None,  # заполним ниже из hex
        "--lc-a-700-rgb": None,
        "--lc-ink-on-accent": tinted(hue, 0.055, 0.0045),
        "--lc-bg-app": tinted(sh, 0.016, 0.0030),
        "--lc-bg-panel": tinted(sh, 0.018, 0.0052),
        "--lc-bg-raise": tinted(sh, 0.024, 0.0155),
        "--lc-bg-selected": tinted(sh, 0.020, 0.0110),
        "--lc-line": tinted(sh, 0.026, 0.0480),
        "--lc-line-strong": tinted(sh, 0.028, 0.0600),
        "--lc-text-1": tinted(sh, 0.012, 0.7800),
        "--lc-text-2": tinted(sh, 0.016, 0.6000),
        "--lc-text-3": tinted(sh, 0.018, 0.3300),
        "--lc-text-4": tinted(sh, 0.018, 0.2300),
        "--lc-text-5": tinted(sh, 0.018, 0.1150),
        "--lc-selected": tinted(hue, 0.058, 0.0235),
        "--lc-primary-subtle": tinted(hue, 0.075, 0.0200),
    }
    for token, step in (("--lc-a-500-rgb", 500), ("--lc-a-700-rgb", 700)):
        r, g, b = (int(a[step][i : i + 2], 16) for i in (1, 3, 5))
        dark[token] = f"{r} {g} {b}"

    light = {
        **{f"--lc-a-{s}": a[s] for s in STEPS},
        "--lc-a-500-rgb": dark["--lc-a-500-rgb"],
        "--lc-a-700-rgb": dark["--lc-a-700-rgb"],
        "--lc-ink-on-accent": dark["--lc-ink-on-accent"],
        # Светлая тема ставит поверхности ПОИМЁННО, мимо --lc-bg-*: повторяем
        # её набор, иначе объявления пресета не доедут никуда.
        "--lc-bg-0": tinted(sh, 0.006, 0.9750),
        "--lc-bg-1": tinted(sh, 0.014, 0.8280),
        "--lc-bg-2": tinted(sh, 0.014, 0.8280),
        # Карточка — чистый белый, как в базе: лиловый подтон несут страница,
        # панель и наведение, а бумага под содержимым остаётся бумагой.
        "--lc-surface": "#ffffff",
        "--lc-surface-hover": tinted(sh, 0.018, 0.7950),
        "--lc-selected": tinted(hue, 0.070, 0.6800),
        "--lc-border": tinted(sh, 0.020, 0.6900),
        "--lc-border-strong": tinted(sh, 0.024, 0.5700),
        "--lc-text-1": tinted(sh, 0.030, 0.0120),
        "--lc-text-2": tinted(sh, 0.032, 0.0520),
        "--lc-text-3": tinted(sh, 0.030, 0.0880),
        "--lc-text-4": tinted(sh, 0.030, 0.1700),
        "--lc-text-5": tinted(sh, 0.030, 0.1700),
        "--lc-primary-subtle": "var(--lc-a-50)",
    }
    # bg-2 обязан отличаться от bg-1? Нет: в светлой теме это одно и то же
    # намеренно (см. комментарий сторожа), поэтому оставляем равными.
    return dark, light


# ══════════════════════════ 4. ПРОВЕРКИ ══════════════════════════════════

def measure(maps, ink, bg, scheme, where):
    bg_raw = bg if bg.startswith("#") else maps.resolve(bg, scheme)
    bg_hex = flatten(bg_raw, "#000000", f"{where} (фон)")
    ink_raw = ink if ink.startswith("#") else maps.resolve(ink, scheme)
    ink_hex = flatten(ink_raw, bg_hex, f"{where} (чернила)")
    return contrast(ink_hex, bg_hex), ink_hex, bg_hex


def check(maps, verbose=True):
    """Возвращает (провалы, замеры). Замеры — для «самых узких мест»."""
    fails, rows = [], []
    for scheme in ("dark", "light"):
        for ink, bg, mn, where in PAIRS:
            only = ONLY.get((ink, bg))
            if only and only != scheme:
                continue
            ratio, i_hex, b_hex = measure(maps, ink, bg, scheme, where)
            rows.append((ratio / mn, ratio, mn, scheme, where, ink, i_hex, bg, b_hex))
            if ratio < mn:
                fails.append(
                    f"[пара/{scheme}] {where}: {ink} ({i_hex}) на {bg} ({b_hex}) = "
                    f"{ratio:.2f} при норме {mn}"
                )

        # Лестница поверхностей.
        for upper, lower, where in LADDER:
            r = contrast(maps.resolve(upper, scheme), maps.resolve(lower, scheme))
            rows.append((r / LADDER_MIN, r, LADDER_MIN, scheme, f"лестница: {where}", upper, "", lower, ""))
            if r < LADDER_MIN:
                fails.append(f"[лестница/{scheme}] {where}: {upper} × {lower} = {r:.3f}")

        # Рама отделима от страницы: заливкой ИЛИ линией.
        by_fill = contrast(maps.resolve("--lc-bg-1", scheme), maps.resolve("--lc-bg-0", scheme))
        by_line = contrast(maps.resolve("--lc-border", scheme), maps.resolve("--lc-bg-1", scheme))
        if max(by_fill, by_line) < 1.12:
            fails.append(f"[рама/{scheme}] заливка {by_fill:.3f}, линия {by_line:.3f} — обе ниже 1.12")

        # Два имени — не один цвет.
        for x, y in SAME_FORBIDDEN:
            if maps.resolve(x, scheme) == maps.resolve(y, scheme):
                fails.append(f"[совпадение/{scheme}] {x} и {y} — один цвет")

        # Кольцо фокуса внутри исходящего пузыря.
        bubble = maps.resolve("--lc-bubble-out", scheme)
        border = contrast(maps.resolve("--lc-focus-border", scheme), bubble)
        halo_raw = maps.resolve("--lc-focus-halo", scheme)
        halo = contrast(flatten(halo_raw, bubble, "ореол"), bubble)
        rows.append((max(border, halo) / UI, max(border, halo), UI, scheme, "фокус в пузыре", "", "", "", ""))
        if max(border, halo) < UI:
            fails.append(f"[фокус/{scheme}] обводка {border:.2f}, ореол {halo:.2f} — обе ниже 3")

        # Выключенная кнопка: форму видно, но она тише подписи.
        edge = maps.resolve("--lc-disabled-edge", scheme)
        for bg_token, где in (("--lc-bg-0", "на полотне"), ("--lc-surface", "на карточке")):
            r = contrast(edge, maps.resolve(bg_token, scheme))
            if r < EDGE_MIN:
                fails.append(f"[выключенная/{scheme}] рамка {где} = {r:.2f} при норме {EDGE_MIN}")
        label = contrast(maps.resolve("--lc-disabled-text", scheme), maps.resolve("--lc-disabled-bg", scheme))
        edge_r = contrast(edge, maps.resolve("--lc-bg-0", scheme))
        if edge_r >= label:
            fails.append(f"[выключенная/{scheme}] рамка {edge_r:.2f} громче подписи {label:.2f}")

    # Известные провалы обязаны остаться провалами.
    for ink, bg, mn, scheme, where in PENDING:
        ratio, _, _ = measure(maps, ink, bg, scheme, where)
        if ratio >= mn:
            fails.append(f"[PENDING/{scheme}] {where} внезапно починился: {ratio:.2f} ≥ {mn}")

    # Каналы акцента совпадают с hex.
    for hex_token, rgb_token in (("--lc-a-500", "--lc-a-500-rgb"), ("--lc-a-700", "--lc-a-700-rgb")):
        for scheme in ("dark", "light"):
            h = maps.resolve(hex_token, scheme)
            ch = [int(x) for x in maps.raw(rgb_token, scheme).split()]
            if [int(h[i : i + 2], 16) for i in (1, 3, 5)] != ch:
                fails.append(f"[каналы/{scheme}] {rgb_token} разъехался с {hex_token}")

    return fails, rows


def role_matrix(maps):
    """ΔE акцента против ролей, которые видны с ним в одном кадре."""
    out = []
    for scheme in ("dark", "light"):
        mine = maps.resolve("--lc-primary", scheme)
        for rival in ROLE_RIVALS:
            other = maps.resolve(rival, scheme)
            out.append((delta_e(mine, other), scheme, "--lc-primary", rival, mine, other))
        # Из colorRoles.test.ts: точка присутствия берёт акцент и стоит рядом
        # с точкой связи; обе видны в окне «это я».
        for a, b, где in (
            ("--lc-presence-online", "--lc-conn-ok", "точка связи под точкой присутствия"),
            ("--lc-presence-online", "--lc-presence-away", "переключатель «На месте / Отошёл»"),
        ):
            out.append((delta_e(maps.resolve(a, scheme), maps.resolve(b, scheme)), scheme, a, b,
                        maps.resolve(a, scheme), maps.resolve(b, scheme)))
        # Подпись бота и текст акцентом лежат в одной ленте.
        out.append((delta_e(maps.resolve("--lc-primary-text", scheme), maps.resolve("--lc-accent-text", scheme)),
                    scheme, "--lc-primary-text", "--lc-accent-text",
                    maps.resolve("--lc-primary-text", scheme), maps.resolve("--lc-accent-text", scheme)))
    return out


# ══════════════════════════ 5. ПОИСК ТОНА ════════════════════════════════

def evaluate(hue, ramp_y=None):
    d, l = build(hue, ramp_y)
    maps = Maps(d, l)
    fails, rows = check(maps)
    roles = role_matrix(maps)
    worst_role = min(r[0] for r in roles)
    worst_pair = min(r[0] for r in rows)
    return fails, rows, roles, worst_role, worst_pair, maps, d, l


def search():
    print(f"{'тон':>6} {'провалов':>9} {'мин ΔE':>8} {'узкая пара':>11}")
    for hue in range(292, 346, 2):
        fails, rows, roles, wr, wp, *_ = evaluate(float(hue))
        print(f"{hue:>6} {len(fails):>9} {wr:>8.1f} {wp:>11.3f}")


# ══════════════════════════ 6. ВЫВОД CSS ═════════════════════════════════

TEMPLATE = """/*
 * ПРЕСЕТ «АМЕТИСТ» (data-lc-preset="amethyst") — вторая ось к светло/темно.
 *
 * ЧТО ЗАДАЁТ ПРЕСЕТ. Только ПРИМИТИВЫ: рампу акцента, чернила на его заливке,
 * поверхности, линии и текст. Сотня семантических имён (--lc-primary,
 * --lc-border, --lc-success-…) объявлена в lc-vars.css через var() на эти
 * примитивы и переезжает следом сама.
 *
 * ⚠ ДВА БЛОКА, ПОТОМУ ЧТО НАПРАВЛЕНИЕ КОНТРАСТА В ТЕМАХ ЗЕРКАЛЬНОЕ. В тёмной
 * графика берёт светлую ступень (500), заливка носит ТЁМНЫЕ чернила, наведение
 * идёт ВВЕРХ (400). В светлой всё берёт тёмную ступень (700), чернила БЕЛЫЕ,
 * наведение идёт ВНИЗ (800).
 *
 * ⚠ ТЁМНЫЙ БЛОК ПОМЕЧЕН `:not([data-mantine-color-scheme="light"])`, и это не
 * украшение: `:root[data-lc-preset="x"]` весит ровно столько же, сколько
 * `:root[data-mantine-color-scheme="light"]`, а лежит ниже в файле — без
 * оговорки он перекрасил бы и светлую тему. Отсюда же второе следствие:
 * светлый блок обязан объявлять рампу ЦЕЛИКОМ, потому что тёмный до него не
 * доходит.
 *
 * ⚠ ПОЧЕМУ ЧЕРНИЛА НА ЗАЛИВКЕ ТЁМНЫЕ, А НЕ БЕЛЫЕ. Посчитано, а не выбрано:
 * белый на {a500} даёт {w500:.2f}, на {a600} — {w600:.2f}, при норме 4.5.
 * Тёмные {ink} дают {i400:.2f} / {i500:.2f} / {i600:.2f} на ступенях 400/500/600.
 * В светлой теме наоборот: белый на {a700} — {w700:.2f}, на {a800} — {w800:.2f}.
 *
 * ⚠ АМЕТИСТ РАЗВЕДЁН С ФИОЛЕТОВЫМ БОТОМ. --lc-accent (#8b5cf6 / #7c3aed) — это
 * значок бота, и он виден рядом с акцентом в одном кадре. Перцептивная
 * дистанция OKLab ΔE·100: {de_dark:.1f} в тёмной и {de_light:.1f} в светлой при
 * пороге 15 из colorRoles.test.ts. Тон акцента {hue:.0f}° — не «просто
 * фиолетовый», а сдвинутый в пурпур ровно настолько, насколько нужно, чтобы
 * бот и акцент не читались одним цветом.
 *
 * ⚠ ЧЕГО ПРЕСЕТ НЕ ПЕРЕКРАШИВАЕТ, И ЭТО ВИДНО ГЛАЗУ. Подложка «в работе» и
 * подложка успеха заданы в тёмной теме ЛИТЕРАЛОМ #10240f, тепловая карта —
 * своими пятью, входящий пузырь — своим: это не примитивы, и пресет их не
 * трогает. Контраст на них сходится (текст акцентом на #10240f — 9.2), но
 * оттенок остаётся зелёным от базовой палитры. Чинится не здесь: либо эти
 * литералы переезжают на ступени рампы в lc-vars.css, либо в набор примитивов
 * пресета добавляются --lc-success-subtle, --lc-status-inprogress-bg и
 * --lc-heat-1…4. Пока не сделано — знать про это обязательно.
 *
 * ЧИСЛА В ЭТОМ ФАЙЛЕ ПОСЧИТАНЫ, А НЕ ОБЕЩАНЫ: design-system/presets/amethyst.py
 * гоняет все {npairs} пары PAIRS в обеих схемах ({nmeas} замеров), лестницу
 * поверхностей, кольцо фокуса, выключенную кнопку, известные провалы PENDING и
 * матрицу ΔE. Провалов ноль.
 */

:root[data-lc-preset="{key}"]:not([data-mantine-color-scheme="light"]) {{
{dark_body}
}}

:root[data-lc-preset="{key}"][data-mantine-color-scheme="light"] {{
{light_body}
}}
"""

DARK_ORDER = [
    ("§1 рампа акцента: 500 — графика тёмной темы, 600 — её заливка", [f"--lc-a-{s}" for s in STEPS]),
    ("тот же цвет каналами — для rgba() от var(), которого CSS не умеет", ["--lc-a-500-rgb", "--lc-a-700-rgb"]),
    ("чернила на любой заливке акцента", ["--lc-ink-on-accent"]),
    ("§2 поверхности: лиловый подтон, а не серый", ["--lc-bg-app", "--lc-bg-panel", "--lc-bg-raise", "--lc-bg-selected"]),
    ("§3 линии", ["--lc-line", "--lc-line-strong"]),
    ("§4 текст", ["--lc-text-1", "--lc-text-2", "--lc-text-3", "--lc-text-4", "--lc-text-5"]),
    ("§5 литералы, подкрашенные акцентом", ["--lc-selected", "--lc-primary-subtle"]),
]

LIGHT_ORDER = [
    ("§1 рампа — целиком: тёмный блок сюда не доходит", [f"--lc-a-{s}" for s in STEPS]),
    ("каналы и чернила заливки — общие с тёмной", ["--lc-a-500-rgb", "--lc-a-700-rgb", "--lc-ink-on-accent"]),
    ("§2 поверхности. Светлая тема ставит их ПОИМЁННО, мимо --lc-bg-*", ["--lc-bg-0", "--lc-bg-1", "--lc-bg-2", "--lc-surface", "--lc-surface-hover"]),
    ("§3 линии", ["--lc-border", "--lc-border-strong"]),
    ("§4 текст", ["--lc-text-1", "--lc-text-2", "--lc-text-3", "--lc-text-4", "--lc-text-5"]),
    ("§5 литералы, подкрашенные акцентом", ["--lc-selected", "--lc-primary-subtle"]),
]

NOTES = {}


def body(decls, order):
    lines = []
    for title, names in order:
        lines.append(f"  /* {title} */")
        for n in names:
            note = NOTES.get(n, "")
            lines.append(f"  {n}: {decls[n]};" + (f" /* {note} */" if note else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def emit(hue, maps, d, l):
    a = {s: d[f"--lc-a-{s}"] for s in STEPS}
    ink = d["--lc-ink-on-accent"]
    text = TEMPLATE.format(
        key=KEY,
        hue=hue,
        a500=a[500], a600=a[600], a700=a[700], a800=a[800], ink=ink,
        w500=contrast("#ffffff", a[500]), w600=contrast("#ffffff", a[600]),
        w700=contrast("#ffffff", a[700]), w800=contrast("#ffffff", a[800]),
        i400=contrast(ink, a[400]), i500=contrast(ink, a[500]), i600=contrast(ink, a[600]),
        de_dark=delta_e(maps.resolve("--lc-primary", "dark"), maps.resolve("--lc-accent", "dark")),
        de_light=delta_e(maps.resolve("--lc-primary", "light"), maps.resolve("--lc-accent", "light")),
        npairs=len(PAIRS),
        nmeas=sum(1 for scheme in ("dark", "light") for i, b, mn, w in PAIRS
                  if ONLY.get((i, b), scheme) == scheme),
        dark_body=body(d, DARK_ORDER),
        light_body=body(l, LIGHT_ORDER),
    )
    open(OUT, "w", encoding="utf-8").write(text)
    return text


# ══════════════════════════ 7. ОБРАТНАЯ СВЕРКА ПО ФАЙЛУ ══════════════════

def maps_from_css(path):
    """
    Собирает карты ИЗ ЗАПИСАННОГО ФАЙЛА, а не из питоновских словарей.

    ⚠ КЛАССИФИКАЦИЯ БЛОКА — ПО СЕЛЕКТОРУ БЕЗ `:not(...)`. Тёмный блок пресета
    обязан нести оговорку `:not([data-mantine-color-scheme="light"])`, то есть
    подстрока «color-scheme="light"» есть и в НЁМ. Проверка «есть подстрока →
    блок светлый» на этом ломается молча: тёмная карта остаётся базовой, и
    пресет выглядит проверенным, не будучи проверенным ни разу.
    """
    dark, light = {}, {}
    for selector, body in _blocks(open(path, encoding="utf-8").read()):
        if "data-lc-preset" not in selector:
            continue
        bare = re.sub(r":not\([^)]*\)", "", selector)
        target = light if 'color-scheme="light"' in bare else dark
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
            target[name] = value.strip()
    return dark, light


# ══════════════════════════ 8. ГЛАВНОЕ ═══════════════════════════════════

HUE = 330.0  # найдено перебором; см. --search

# ΔE, которую держать обязаны (роли из задания + пары colorRoles.test.ts).
# Пара «текст акцентом × подпись бота» сюда НЕ входит: сторож её не меряет, а
# развести две БЛЕДНЫЕ ступени фиолетового до 15 нельзя ничем, кроме ухода
# акцента в розовый. Её значение печатается отдельно и вынесено в отчёт.
def _required(roles):
    return [r for r in roles if not (r[2].endswith("-text") and r[3].endswith("-text"))]


def report(fails, rows, roles, title):
    print(f"\n── {title} ──")
    print(f"провалов: {len(fails)}")
    for f in fails:
        print("  ✗", f)
    print("самые узкие места (запас = отношение к норме):")
    for запас, ratio, mn, scheme, where, ink, i_hex, bg, b_hex in sorted(rows)[:10]:
        print(f"  {запас:5.2f}×  {ratio:6.2f} при {mn:<4} [{scheme}] {where}"
              + (f"  {ink} {i_hex} на {bg} {b_hex}" if i_hex else ""))
    print("ΔE ролей (порог 15):")
    for de, scheme, a, b, ha, hb in sorted(roles):
        обяз = not (a.endswith("-text") and b.endswith("-text"))
        mark = "✗" if (обяз and de < DELTA_E_MIN) else (" " if обяз else "·")
        print(f"  {mark} {de:5.1f}  [{scheme}] {a} {ha} × {b} {hb}")


def main():
    if "--search" in sys.argv:
        search()
        return

    fails, rows, roles, worst_role, worst_pair, maps, d, l = evaluate(HUE)
    n_pairs = sum(1 for scheme in ("dark", "light") for i, b, mn, w in PAIRS
                  if ONLY.get((i, b), scheme) == scheme)
    print(f"тон акцента: {HUE}°   пар PAIRS проверено: {n_pairs}   всего замеров: {len(rows)}")
    report(fails, rows, roles, "расчёт по примитивам")

    print("\nрампа:")
    for s in STEPS:
        h = d[f"--lc-a-{s}"]
        print(f"  --lc-a-{s:<4} {h}  Y={luminance(h):.4f}  белый {contrast('#ffffff', h):5.2f}  "
              f"чернила {contrast(d['--lc-ink-on-accent'], h):5.2f}")

    ok = not fails and min(r[0] for r in _required(roles)) >= DELTA_E_MIN
    if not ok:
        print("\nНЕ ЗАПИСАНО: есть провалы")
        raise SystemExit(1)

    emit(HUE, maps, d, l)
    print(f"\nзаписано: {OUT}")

    # Обратная сверка: читаем ФАЙЛ и считаем всё заново по нему.
    fd, fl = maps_from_css(OUT)
    back = Maps(fd, fl)
    bfails, brows = check(back)
    broles = role_matrix(back)
    report(bfails, brows, broles, "обратная сверка по записанному .css")
    if bfails or min(r[0] for r in _required(broles)) < DELTA_E_MIN:
        print("\n⚠ ФАЙЛ НЕ СХОДИТСЯ С РАСЧЁТОМ")
        raise SystemExit(1)
    if [round(r[1], 4) for r in sorted(rows)] != [round(r[1], 4) for r in sorted(brows)]:
        print("\n⚠ замеры по файлу разошлись с замерами по расчёту")
        raise SystemExit(1)
    print("\nфайл и расчёт сходятся до последнего знака.")


if __name__ == "__main__":
    main()
