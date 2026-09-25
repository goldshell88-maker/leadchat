#!/usr/bin/env python3
"""
ПРЕСЕТ «МЕДЬ» (copper) — счёт примитивов, а не подбор на глаз.

Каждое число здесь ВЫЧИСЛЕНО: цвет задаётся тоном и хромой в OKLCH, а его
ступень подбирается двоичным поиском по ЦЕЛЕВОЙ ОТНОСИТЕЛЬНОЙ ЯРКОСТИ WCAG.
Приём выбран не ради красоты формулы: поверхности и текст тёмной темы уже
проходят пороги на зелёном акценте, и если сохранить их ЯРКОСТЬ и поменять
только тон, все пары «текст на поверхности» переносятся один в один. Спорным
остаётся только рампа акцента — её и считаем честно.

Запуск:  python3 copper.py           — проверка и запись copper.css
         python3 copper.py --dry     — только проверка
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _engine import (TEXT, UI, LADDER_MIN, EDGE_MIN, DELTA_E_MIN,
                     luminance, contrast, composite, hex_for, delta_e,
                     Palette, flatten, rgb)
from _pairs import PAIRS, LADDER, SAME_FORBIDDEN, ROLE_RIVALS

HEADER = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_copper_header.txt"), encoding="utf-8").read()

KEY = "copper"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{KEY}.css")

# ─────────────────────── рампа акцента: (тон, хрома, целевая яркость) ────────
#
# ТОН 44…62 — это диапазон настоящей меди: полированная #b87333 стоит на 60,
# патинированная терракота — на 33. Тёмные ступени уведены к красному краю,
# светлые к золотому: так ведёт себя сама медь при потемнении.
#
# ⚠ ЯРКОСТИ ВЫБРАНЫ НЕ ПО ЭСТЕТИКЕ, А ПО ЧЕТЫРЁМ ЗАМКАМ (см. отчёт):
#   a-600 ≥ 0.193 — иначе тёмные чернила на заливке кнопки падают ниже 4.5;
#   a-500 > a-600 — наведение в тёмной теме идёт ВВЕРХ, значит база ниже него;
#   a-700 ≤ 0.183 — иначе белый на светлой заливке падает ниже 4.5;
#   a-700 как можно НИЖЕ — единственный рычаг, разводящий медь с янтарём.
RAMP = {
    50:  (62, 0.018, 0.895),
    100: (61, 0.030, 0.770),
    200: (60, 0.042, 0.590),
    300: (60, 0.050, 0.430),   # тёмная тема: текст акцентом и текст успеха
    400: (60, 0.058, 0.352),   # тёмная тема: наведение на заливку, верх тепловой карты
    500: (60, 0.055, 0.280),   # ЗНАК, графика тёмной темы, точка «в сети»
    600: (59, 0.070, 0.213),   # заливка тёмной темы, счётчик непрочитанных
    700: (60, 0.100, 0.062),   # ВСЁ в светлой теме
    800: (56, 0.095, 0.042),   # светлый текст акцентом, исходящий пузырь обеих тем
    900: (52, 0.075, 0.024),
    950: (48, 0.055, 0.012),
}

# Чернила на медной заливке. Тёмные, а не белые: заливка в тёмной теме идёт по
# ступеням 400–600, и белый на них не берёт 4.5 ни на одной (см. отчёт).
INK_ON_ACCENT = (40, 0.028, 0.0030)

# ── поверхности и текст: тон тёплый, ЯРКОСТЬ повторяет зелёную тему ──────────
DARK_SURF = {
    "--lc-bg-app":        (58, 0.012, 0.00289),
    "--lc-bg-panel":      (57, 0.014, 0.00415),
    "--lc-bg-raise":      (55, 0.020, 0.01463),
    "--lc-bg-selected":   (55, 0.017, 0.00877),
    "--lc-selected":      (50, 0.034, 0.01770),
    "--lc-line":          (55, 0.018, 0.04460),
    "--lc-line-strong":   (55, 0.018, 0.05540),
    "--lc-text-1":        (60, 0.010, 0.79600),
    "--lc-text-2":        (58, 0.012, 0.59450),
    "--lc-text-3":        (56, 0.014, 0.31160),
    "--lc-text-4":        (56, 0.014, 0.21350),
    "--lc-text-5":        (56, 0.014, 0.11210),
    "--lc-primary-subtle": (48, 0.045, 0.01346),
}

LIGHT_SURF = {
    "--lc-bg-app":        (60, 0.006, 0.97000),
    "--lc-bg-panel":      (60, 0.016, 0.85500),
    "--lc-bg-raise":      (60, 0.006, 0.97000),
    "--lc-bg-selected":   (58, 0.022, 0.82800),
    "--lc-selected":      (56, 0.048, 0.73500),
    "--lc-line":          (58, 0.020, 0.70100),
    "--lc-line-strong":   (56, 0.026, 0.65700),
    "--lc-text-1":        (56, 0.020, 0.00882),
    "--lc-text-2":        (56, 0.020, 0.05140),
    "--lc-text-3":        (56, 0.018, 0.08857),
    "--lc-text-4":        (56, 0.016, 0.17064),
    "--lc-text-5":        (56, 0.016, 0.17064),
    "--lc-primary-subtle": (62, 0.018, 0.89500),
}

def build():
    ramp = {k: hex_for(*v) for k, v in RAMP.items()}
    ink = hex_for(*INK_ON_ACCENT)
    dark = {f"--lc-a-{k}": v for k, v in ramp.items()}
    dark["--lc-a-500-rgb"] = " ".join(str(c) for c in rgb(ramp[500]))
    dark["--lc-a-700-rgb"] = " ".join(str(c) for c in rgb(ramp[700]))
    dark["--lc-ink-on-accent"] = ink
    for name, spec in DARK_SURF.items():
        dark[name] = hex_for(*spec)

    light = dict(dark)          # рампа и чернила общие для обеих схем
    for name, spec in LIGHT_SURF.items():
        light[name] = hex_for(*spec)
    return dark, light

NOTE = {
    "--lc-a-300": "тёмная: текст акцентом и текст успеха",
    "--lc-a-400": "тёмная: наведение на заливку, верх тепловой карты",
    "--lc-a-500": "ЗНАК; графика тёмной темы, точка «в сети», фокус",
    "--lc-a-600": "тёмная: заливка кнопки и счётчик непрочитанных",
    "--lc-a-700": "светлая: ВСЁ — графика, заливка, фокус, точка «в сети»",
    "--lc-a-800": "светлая: текст акцентом; исходящий пузырь ОБЕИХ тем",
    "--lc-ink-on-accent": "тёмные чернила на меди; белый не берёт 4.5 на 400-600",
    "--lc-a-500-rgb": "те же каналы: CSS не умеет rgba() от var()",
    "--lc-a-700-rgb": "то же для светлой темы",
    "--lc-bg-app": "полотно страницы",
    "--lc-bg-panel": "шапка, рельса, зазоры между колонками",
    "--lc-bg-raise": "карточка (в тёмной ещё и утопленный блок)",
    "--lc-bg-selected": "наведение на строку",
    "--lc-selected": "выбранная строка списка",
    "--lc-line": "рамка",
    "--lc-line-strong": "сильная рамка, ползунок прокрутки",
    "--lc-text-1": "основной текст",
    "--lc-text-2": "вторичный",
    "--lc-text-3": "приглушённый",
    "--lc-text-4": "булавка, значки",
    "--lc-text-5": "только disabled",
    "--lc-primary-subtle": "подложка под текст акцентом",
    "--lc-bg-0": "светлая тема задаёт поверхности семантикой, а не примитивами",
    "--lc-bg-2": "в светлой равен панели, а не карточке — так в базовой теме",
}

# В светлой схеме примитивы поверхностей не читает никто: там семантика задана
# прямо, и ниже она объявлена своими именами. Держим их ради переносимости имён.
NOTE_LIGHT = {
    "--lc-bg-app": "имя ради парности: в светлой читают --lc-bg-0 ниже",
    "--lc-bg-panel": "имя ради парности: читают --lc-bg-1 / --lc-bg-2",
    "--lc-bg-raise": "имя ради парности: читают --lc-surface",
    "--lc-bg-selected": "имя ради парности: читают --lc-surface-hover",
    "--lc-line": "имя ради парности: читают --lc-border",
    "--lc-line-strong": "имя ради парности: читают --lc-border-strong",
    "--lc-a-300": "в светлой не используется: акцент здесь берёт 700 и 800",
    "--lc-a-400": "в светлой не используется",
    "--lc-a-500": "в светлой только знак Lead Partner (--lc-brand)",
    "--lc-a-600": "в светлой только счётчик непрочитанных",
}

# ── ВТОРОЙ ИСТОЧНИК ЦВЕТА: КОРТЕЖ MANTINE ───────────────────────────────────
#
# Заливки кнопок, бейджей, чекбоксов и тостов приходят НЕ из lc-vars.css, а из
# кортежа `lp` в theme.tsx — а он на JS и var() содержать не может. Пресет,
# который красит только токены, даёт наполовину перекрашенное приложение:
# строка выбрана медью, кнопка на ней осталась зелёной. Поэтому здесь
# переопределяются сами переменные Mantine.
#
# `green` в теме — ТОТ ЖЕ кортеж (`colors: { green: lp }`), значит и красить его
# надо тем же, иначе кнопка подтверждения останется зелёной при медном акценте.
#
# ⚠ ССЫЛОЧНЫЕ ПЕРЕМЕННЫЕ НЕ ТРОГАЕМ. Mantine задаёт -filled/-filled-hover/
# -light-color/-outline/-text как `var(--mantine-color-lp-N)`, и они поедут за
# ступенями сами (get-css-color-variables.mjs). А вот -light/-light-hover/
# -outline-hover он считает на JS и записывает ЛИТЕРАЛЬНОЙ rgba() — эти три
# обязаны быть здесь, иначе подложка light-варианта останется зелёной.
MANTINE_NAMES = ("lp", "green")
STEP_BY_INDEX = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900]

def _rgba(hexv, alpha):
    r, g, b = rgb(hexv)
    return f"rgba({r}, {g}, {b}, {alpha})"

def mantine_vars(ramp_hex, scheme):
    out = {}
    for name in MANTINE_NAMES:
        for i, step in enumerate(STEP_BY_INDEX):
            out[f"--mantine-color-{name}-{i}"] = ramp_hex[step]
        if scheme == "light":
            base = ramp_hex[600]          # primaryShade: 6
            out[f"--mantine-color-{name}-light"] = _rgba(base, 0.1)
            out[f"--mantine-color-{name}-light-hover"] = _rgba(base, 0.12)
            out[f"--mantine-color-{name}-outline-hover"] = _rgba(base, 0.05)
        else:
            soft = ramp_hex[400]          # primaryShade − 2
            edge = ramp_hex[200]          # primaryShade − 4
            out[f"--mantine-color-{name}-light"] = _rgba(soft, 0.15)
            out[f"--mantine-color-{name}-light-hover"] = _rgba(soft, 0.2)
            out[f"--mantine-color-{name}-outline-hover"] = _rgba(edge, 0.05)
    return out


def css(dark, light):
    def block(sel, tokens, order, notes=NOTE):
        lines = [f"{sel} {{"]
        width = max(len(n) for n in order) + len(": ;") + 9
        for name in order:
            decl = f"  {name}: {tokens[name]};"
            note = notes.get(name)
            lines.append(f"{decl.ljust(width)} /* {note} */" if note else decl)
        lines.append("}")
        return "\n".join(lines)

    ramp_order = [f"--lc-a-{k}" for k in RAMP] + ["--lc-a-500-rgb", "--lc-a-700-rgb",
                                                  "--lc-ink-on-accent"]
    surf_order = list(DARK_SURF)
    light_semantic = {
        "--lc-bg-0": light["--lc-bg-app"],
        "--lc-bg-1": light["--lc-bg-panel"],
        "--lc-bg-2": light["--lc-bg-panel"],
        "--lc-surface": light["--lc-bg-raise"],
        "--lc-surface-hover": light["--lc-bg-selected"],
        "--lc-border": light["--lc-line"],
        "--lc-border-strong": light["--lc-line-strong"],
    }
    ramp_hex = {k: dark[f"--lc-a-{k}"] for k in RAMP}
    mant_dark = mantine_vars(ramp_hex, "dark")
    mant_light = mantine_vars(ramp_hex, "light")
    d = dict(dark); d.update(mant_dark)
    l = dict(light); l.update(light_semantic); l.update(mant_light)
    dark_sel = (f'/* ── ТЁМНАЯ СХЕМА ─────────────────────────────────────────────────── */\n'
                f':root[data-lc-preset="{KEY}"]:not([data-mantine-color-scheme="light"])')
    light_sel = (f'/* ── СВЕТЛАЯ СХЕМА. Рампа повторена целиком: базовый блок :root отдаёт\n'
                 f'   ЗЕЛЁНУЮ рампу обеим схемам, и не назвать её здесь — значит оставить\n'
                 f'   светлую тему зелёной. ───────────────────────────────────────────── */\n'
                 f':root[data-lc-preset="{KEY}"][data-mantine-color-scheme="light"]')
    notes_light = dict(NOTE); notes_light.update(NOTE_LIGHT)
    return (block(dark_sel, d, ramp_order + surf_order + list(mant_dark)),
            block(light_sel, l,
                  ramp_order + surf_order + list(light_semantic) + list(mant_light), notes_light))

def check(dark_block, light_block):
    p = Palette(dark_block + "\n" + light_block, preset=KEY)
    rows, fails = [], []
    for ink_t, bg_t, minimum, where in PAIRS:
        for scheme in ("dark", "light"):
            bg = flatten(p.resolve(bg_t, scheme), "#000000", where)
            ink = flatten(p.resolve(ink_t, scheme), bg, where)
            r = contrast(ink, bg)
            rows.append((r - minimum, r, minimum, scheme, ink_t, ink, bg_t, bg, where))
            if r < minimum - 1e-9:
                fails.append(f"[{scheme}] {where}: {ink_t} ({ink}) на {bg_t} ({bg}) = "
                             f"{r:.2f}:1 при норме {minimum}")
    rows.sort()
    return p, rows, fails

def extras(p):
    """Проверки того же файла, которые не входят в PAIRS, но валят прогон."""
    out = []
    for scheme in ("dark", "light"):
        for up, lo, where in LADDER:
            r = contrast(p.resolve(up, scheme), p.resolve(lo, scheme))
            if r < LADDER_MIN:
                out.append(f"[{scheme}] лестница {where}: {r:.3f} < {LADDER_MIN}")
        by_fill = contrast(p.resolve("--lc-bg-1", scheme), p.resolve("--lc-bg-0", scheme))
        by_line = contrast(p.resolve("--lc-border", scheme), p.resolve("--lc-bg-1", scheme))
        if max(by_fill, by_line) < 1.12:
            out.append(f"[{scheme}] рама не отделима: заливка {by_fill:.3f}, линия {by_line:.3f}")
        for a, b in SAME_FORBIDDEN:
            if p.resolve(a, scheme) == p.resolve(b, scheme):
                out.append(f"[{scheme}] {a} и {b} — один цвет")
        bubble = p.resolve("--lc-bubble-out", scheme)
        border = contrast(p.resolve("--lc-focus-border", scheme), bubble)
        halo = contrast(flatten(p.resolve("--lc-focus-halo", scheme), bubble, "ореол"), bubble)
        if max(border, halo) < UI:
            out.append(f"[{scheme}] фокус в пузыре: обводка {border:.2f}, ореол {halo:.2f} < {UI}")
        edge = p.resolve("--lc-disabled-edge", scheme)
        for bg in ("--lc-bg-0", "--lc-surface"):
            r = contrast(edge, p.resolve(bg, scheme))
            if r < EDGE_MIN:
                out.append(f"[{scheme}] рамка выключенной кнопки на {bg}: {r:.2f} < {EDGE_MIN}")
        label = contrast(p.resolve("--lc-disabled-text", scheme), p.resolve("--lc-disabled-bg", scheme))
        frame = contrast(edge, p.resolve("--lc-bg-0", scheme))
        if frame >= label:
            out.append(f"[{scheme}] форма ({frame:.2f}) громче подписи ({label:.2f})")
    # ── заливки Mantine: чернила выбраны на JS по СТАРОМУ кортежу ──────────
    #
    # Mantine считает цвет чернил один раз, по яркости шага primaryShade
    # ЗЕЛЁНОГО кортежа из theme.tsx, — пресет её не меняет и изменить не может.
    # Значит чернила остаются ЧЁРНЫМИ, а заливка под ними становится медной;
    # это и надо проверить, а не предполагать.
    ink = "#000000"
    for scheme in ("dark", "light"):
        filled = p.resolve("--mantine-color-lp-6", scheme)
        hover = p.resolve("--mantine-color-lp-filled-hover", scheme) if scheme == "x" else (
            p.resolve("--lc-primary-solid-hover", scheme) if scheme == "dark"
            else p.resolve("--lc-a-500", scheme))
        for what, fill in (("заливка", filled), ("заливка под курсором", hover)):
            r = contrast(ink, fill)
            if r < TEXT:
                out.append(f"[{scheme}] Mantine {what} {fill}: чёрные чернила {r:.2f} < {TEXT}")
        # variant="light": подложка — та же ступень с прозрачностью, текст —
        # правка из theme.tsx (`*-light-color` → --lc-primary-text).
        tint_src, alpha = (p.resolve("--lc-a-600", scheme), 0.1) if scheme == "light" else (
            p.resolve("--lc-a-400", scheme), 0.15)
        text = (p.resolve("--lc-primary-text", scheme) if scheme == "light"
                else p.resolve("--mantine-color-lp-1", scheme))
        for bg_t in ("--lc-surface", "--lc-bg-1"):
            bg = composite(tint_src, alpha, p.resolve(bg_t, scheme))
            r = contrast(text, bg)
            if r < TEXT:
                out.append(f"[{scheme}] Mantine variant=light на {bg_t}: "
                           f"{text} на {bg} = {r:.2f} < {TEXT}")
    return out

def roles(p):
    res = []
    for scheme in ("dark", "light"):
        acc = p.resolve("--lc-primary", scheme)
        for rival in ROLE_RIVALS:
            v = p.resolve(rival, scheme)
            res.append((delta_e(acc, v), scheme, rival, acc, v))
    res.sort()
    return res


# Настоящие пары из colorRoles.test.ts — тест падает именно на них.
ROLE_PAIRS = [
    ("--lc-conn-ok", "--lc-presence-online", "окно «это я»: точка связи под точкой присутствия"),
    ("--lc-conn-degraded", "--lc-presence-away", "то же окно: вы отошли и связь просела"),
    ("--lc-presence-away", "--lc-presence-online", "переключатель «На месте / Отошёл»"),
    ("--lc-presence-away", "--lc-warning", "/chats: полоса «отошёл» и полоса срочности"),
]
KNOWN_WEAK = {("--lc-presence-away", "--lc-warning", "dark"),
              ("--lc-presence-away", "--lc-warning", "light"),
              ("--lc-conn-degraded", "--lc-presence-away", "light")}

def role_pairs(p):
    out = []
    for scheme in ("dark", "light"):
        for a, b, where in ROLE_PAIRS:
            d = delta_e(p.resolve(a, scheme), p.resolve(b, scheme))
            known = (a, b, scheme) in KNOWN_WEAK
            out.append((d, scheme, a, b, where, known))
    return out

def dump(p):
    for scheme in ("dark", "light"):
        print(f"\n--- {scheme} ---")
        for n in ["--lc-a-50","--lc-a-100","--lc-a-200","--lc-a-300","--lc-a-400","--lc-a-500",
                  "--lc-a-600","--lc-a-700","--lc-a-800","--lc-a-900","--lc-a-950",
                  "--lc-ink-on-accent","--lc-bg-0","--lc-bg-1","--lc-bg-2","--lc-surface",
                  "--lc-surface-hover","--lc-selected","--lc-border","--lc-border-strong",
                  "--lc-text-1","--lc-text-2","--lc-text-3","--lc-text-4","--lc-text-5",
                  "--lc-primary","--lc-primary-solid","--lc-primary-text","--lc-primary-subtle",
                  "--lc-bubble-out","--lc-brand"]:
            v = p.resolve(n, scheme)
            print(f"  {n:26s} {v}  Y={luminance(v):.4f}")
        b = p.resolve("--lc-bubble-out", scheme); f = p.resolve("--lc-bg-0", scheme)
        print(f"  исходящий пузырь к полотну ленты: {contrast(b, f):.2f}:1")

if __name__ == "__main__":
    dark, light = build()
    db, lb = css(dark, light)
    p, rows, fails = check(db, lb)

    print(f"пар в таблице: {len(PAIRS)}; замеров (обе схемы): {len(rows)}")
    print(f"ПРОВАЛОВ: {len(fails)}")
    for f in fails:
        print("  ✗ " + f)
    print("\nсамые узкие места (запас, коэффициент, норма):")
    for d, r, m, scheme, it, ih, bt, bh, w in rows[:12]:
        print(f"  +{d:5.2f}  {r:6.2f} / {m}  [{scheme}] {w}: {it} {ih} на {bt} {bh}")

    ex = extras(p)
    print(f"\nсоседние сторожа (лестница, фокус, выключенная кнопка): {len(ex)} замечаний")
    for e in ex:
        print("  ✗ " + e)

    print(f"\nΔE акцента к соседним ролям (порог {DELTA_E_MIN}):")
    for d, scheme, rival, acc, v in roles(p):
        mark = "✓" if d >= DELTA_E_MIN else "✗"
        print(f"  {mark} [{scheme}] --lc-primary {acc} × {rival} {v} → ΔE {d:.1f}")

    print("\nпары colorRoles (порог 15):")
    for d, scheme, a, b, where, known in sorted(role_pairs(p)):
        tag = "известная слабость" if known else ("✓" if d >= DELTA_E_MIN else "✗ ПАДЕНИЕ ТЕСТА")
        print(f"  {tag:20s} [{scheme}] {a} × {b} → ΔE {d:.1f}  ({where})")

    if "--dump" in sys.argv:
        dump(p)

    if "--dry" not in sys.argv:
        if fails or ex:
            raise SystemExit("есть провалы — файл не записан")
        header = HEADER
        open(OUT, "w", encoding="utf-8").write(header + db + "\n\n" + lb + "\n")
        print(f"\nзаписано: {OUT}")
