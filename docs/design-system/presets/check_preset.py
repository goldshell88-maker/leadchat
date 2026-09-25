#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
СЧЁТНАЯ ЧАСТЬ ПРЕСЕТА: повторяет разбор и арифметику сторожей проекта.

Почему не «на глаз»: пороги WCAG глазом не проверяются (4.6 и 3.9 выглядят
одинаково), а направление контраста в темах зеркальное — ошибку видно только
арифметикой. Здесь повторены ровно те приёмы, что в
`frontend/src/test/tokenContrast.test.ts` и `colorRoles.test.ts`:

  * разбор блоков по селектору, база → пресет, светлый блок пресета помечен
    `[data-mantine-color-scheme="light"]`, тёмный — `:not(...light)`;
  * разворачивание var()-цепочек;
  * контраст WCAG 2.1 и композитинг полупрозрачных чернил;
  * ΔE·100 в OKLab для ролей.

Таблица пар НЕ переписана руками, а вычитана из самого теста: переписанная
таблица разъезжается с оригиналом молча.
"""
import re
import sys
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # корень репозитория
VARS_PATH = ROOT / "frontend/src/app/lc-vars.css"
TEST_PATH = ROOT / "frontend/src/test/tokenContrast.test.ts"
ROLES_PATH = ROOT / "frontend/src/test/colorRoles.test.ts"

TEXT = 4.5
UI = 3.0
LADDER_MIN = 1.1
EDGE_MIN = 1.8

# ─────────────────────────── арифметика WCAG 2.1 ───────────────────────────

def _chan(v):
    v = v / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

def rgb(hex_):
    h = hex_.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def luminance(hex_):
    r, g, b = rgb(hex_)
    return 0.2126 * _chan(r) + 0.7152 * _chan(g) + 0.0722 * _chan(b)

def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)

def composite(fg, alpha, bg):
    f, b = rgb(fg), rgb(bg)
    mix = [round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3)]
    return "#" + "".join(f"{c:02x}" for c in mix)

# ─────────────────────────────── OKLab / ΔE ────────────────────────────────

def oklab(hex_):
    R, G, B = rgb(hex_)
    def lin(c):
        x = c / 255.0
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = lin(R), lin(G), lin(B)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
    )

def deltaE(a, b):
    A, B = oklab(a), oklab(b)
    return math.dist(A, B) * 100

def oklch(hex_):
    L, a, b = oklab(hex_)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360

def oklch_to_hex(L, C, h_deg, clip=True):
    """OKLCH → sRGB с понижением цветности до попадания в охват."""
    for _ in range(200):
        a = C * math.cos(math.radians(h_deg))
        b = C * math.sin(math.radians(h_deg))
        l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
        r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
        g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
        bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
        def enc(u):
            if u <= 0.0031308:
                return 12.92 * u
            return 1.055 * (u ** (1 / 2.4)) - 0.055
        vals = [enc(r), enc(g), enc(bl)]
        if not clip or all(-0.0008 <= v <= 1.0008 for v in vals):
            return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in vals)
        C -= 0.002
        if C < 0:
            C = 0
    return "#000000"

# ───────────────────────── разбор CSS (как в сторожах) ─────────────────────

BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
DECL_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")

def strip_comments(css):
    return re.sub(r"/\*[\s\S]*?\*/", "", css)

def read_tokens(css, preset, scheme):
    """Карта токенов: сперва база, затем блоки пресета — как в readTokens()."""
    css = strip_comments(css)
    blocks = BLOCK_RE.findall(css)
    m = {}
    light = scheme == "light"

    def apply(pred):
        for sel, body in blocks:
            if "@media" in sel:
                continue
            if not pred(sel):
                continue
            for name, value in DECL_RE.findall(body):
                m[name] = " ".join(value.split())

    def светлый_блок(sel):
        """
        ⚠ ЛОВУШКА, НА КОТОРОЙ Я УЖЕ ОБЖЁГСЯ, И ОНА ЖЕ СТОИТ В ТЕСТЕ.
        Тёмный блок пресета обязан нести оговорку
        `:not([data-mantine-color-scheme="light"])` — иначе он перебьёт светлую
        тему. Но подстрока `color-scheme="light"` внутри `:not(...)` ЕСТЬ, и
        проверка «есть подстрока → блок светлый» относит тёмный блок к светлой
        схеме. Вырезаем `:not(...)` ДО классификации: браузер смотрит, что
        селектор отрицает, а не какие буквы в нём встречаются.
        """
        return 'color-scheme="light"' in re.sub(r":not\([^)]*\)", "", sel)

    def base(sel):
        if "data-lc-preset" in sel:
            return False
        return light if светлый_блок(sel) else (":root" in sel)

    apply(base)
    if preset:
        mine = f'data-lc-preset="{preset}"'
        def pre(sel):
            if mine not in sel:
                return False
            return светлый_блок(sel) == light
        apply(pre)
    return m

def resolve(tokens, name, depth=0):
    if depth > 12:
        raise RuntimeError(f"циклическая ссылка на {name}")
    raw = tokens.get(name)
    if raw is None:
        raise RuntimeError(f"токен {name} не объявлен")
    ref = re.fullmatch(r"var\((--[\w-]+)\)", raw)
    if ref:
        return resolve(tokens, ref.group(1), depth + 1)
    return raw

def flatten(value, backdrop, where):
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if m:
        hexv = "#" + "".join(f"{int(m.group(i)):02x}" for i in (1, 2, 3))
        alpha = 1.0 if m.group(4) is None else float(m.group(4))
        return composite(hexv, alpha, backdrop)
    raise RuntimeError(f"{where}: значение «{value}» не свести к цвету")

# ──────────────────── таблицы пар — вычитаны из теста ──────────────────────

def load_pairs():
    src = TEST_PATH.read_text()
    body = re.search(r"const PAIRS: Pair\[\] = \[([\s\S]*?)\n\];", src).group(1)
    body = re.sub(r"//[^\n]*", "", body)
    pairs = []
    for m in re.finditer(
        r'\{\s*ink:\s*"([^"]+)",\s*bg:\s*"([^"]+)",\s*min:\s*(TEXT|UI),\s*where:\s*"([^"]+)"'
        r'(?:,\s*only:\s*"(dark|light)")?', body):
        pairs.append({
            "ink": m.group(1), "bg": m.group(2),
            "min": TEXT if m.group(3) == "TEXT" else UI,
            "where": m.group(4), "only": m.group(5),
        })
    return pairs

def load_ladder():
    src = TEST_PATH.read_text()
    body = re.search(r"const LADDER: Array<[^>]*> = \[([\s\S]*?)\n\];", src).group(1)
    return [{"upper": a, "lower": b, "where": c} for a, b, c in re.findall(
        r'\{\s*upper:\s*"([^"]+)",\s*lower:\s*"([^"]+)",\s*where:\s*"([^"]+)"', body)]

PAIRS = load_pairs()
LADDER = load_ladder()

# ───────────────────────────── прогон проверок ─────────────────────────────

def measure(tokens, pair):
    bg_raw = pair["bg"] if pair["bg"].startswith("#") else resolve(tokens, pair["bg"])
    bg = flatten(bg_raw, "#000000", pair["where"] + " (фон)")
    ink_raw = pair["ink"] if pair["ink"].startswith("#") else resolve(tokens, pair["ink"])
    ink = flatten(ink_raw, bg, pair["where"] + " (чернила)")
    return contrast(ink, bg), ink, bg

def run(css, preset, scheme):
    t = read_tokens(css, preset, scheme)
    rows, failures = [], []
    for p in PAIRS:
        if p["only"] and p["only"] != scheme:
            continue
        ratio, ink, bg = measure(t, p)
        rows.append((ratio, p, ink, bg))
        if ratio < p["min"] - 1e-9:
            failures.append(
                f'{p["where"]}: {p["ink"]} ({ink}) на {p["bg"]} ({bg}) = '
                f'{ratio:.2f}:1 при норме {p["min"]:g}')
    # лестница поверхностей
    ladder = []
    for s in LADDER:
        r = contrast(resolve(t, s["upper"]), resolve(t, s["lower"]))
        ladder.append((r, s))
        if r < LADDER_MIN:
            failures.append(f'ЛЕСТНИЦА {s["where"]}: {s["upper"]} × {s["lower"]} = {r:.3f}')
    # рама отделима
    by_fill = contrast(resolve(t, "--lc-bg-1"), resolve(t, "--lc-bg-0"))
    by_line = contrast(resolve(t, "--lc-border"), resolve(t, "--lc-bg-1"))
    if max(by_fill, by_line) < 1.12:
        failures.append(f"РАМА не отделима: заливка {by_fill:.3f}, линия {by_line:.3f}")
    # два имени — не один цвет
    for a, b in (("--lc-selected", "--lc-surface-hover"), ("--lc-bubble-in", "--lc-bg-1"),
                 ("--lc-surface", "--lc-bg-1"), ("--lc-surface-hover", "--lc-bg-1")):
        if resolve(t, a).lower() == resolve(t, b).lower():
            failures.append(f"ОДИН ЦВЕТ: {a} и {b}")
    # фокус внутри исходящего пузыря
    bubble = resolve(t, "--lc-bubble-out")
    border = contrast(resolve(t, "--lc-focus-border"), bubble)
    halo = contrast(flatten(resolve(t, "--lc-focus-halo"), bubble, "ореол"), bubble)
    if max(border, halo) < UI:
        failures.append(f"ФОКУС в пузыре: обводка {border:.2f}, ореол {halo:.2f}")
    # выключенная кнопка
    edge = resolve(t, "--lc-disabled-edge")
    for bgname, where in (("--lc-bg-0", "на полотне"), ("--lc-surface", "на карточке")):
        r = contrast(edge, resolve(t, bgname))
        if r < EDGE_MIN:
            failures.append(f"ВЫКЛ.КНОПКА рамка {where} = {r:.2f} при норме {EDGE_MIN}")
    label = contrast(resolve(t, "--lc-disabled-text"), resolve(t, "--lc-disabled-bg"))
    edge_on_bg0 = contrast(edge, resolve(t, "--lc-bg-0"))
    if edge_on_bg0 >= label:
        failures.append(f"ВЫКЛ.КНОПКА рамка {edge_on_bg0:.2f} громче подписи {label:.2f}")
    return t, rows, ladder, failures

# ─────────────────────────────── роли (ΔE) ─────────────────────────────────

# Роли, которые видны В ОДНОМ КАДРЕ с акцентом. `--lc-text-1` и `--lc-conn-ok`
# в задании не названы, но это тот же вопрос: если «текст акцентом» сравнялся с
# обычным текстом, а точка присутствия — с точкой связи, акцент перестал значить.
ROLE_TOKENS = ["--lc-link", "--lc-info", "--lc-accent", "--lc-warning", "--lc-danger",
               "--lc-text-1", "--lc-conn-ok"]
ACCENT_TOKENS = ["--lc-primary", "--lc-primary-text", "--lc-primary-solid",
                 "--lc-brand", "--lc-unread-bg"]
РАЗЛИЧИМО = float(re.search(r"const РАЗЛИЧИМО = (\d+)", ROLES_PATH.read_text()).group(1))

def roles(tokens):
    out = []
    for a in ACCENT_TOKENS:
        av = resolve(tokens, a)
        for r in ROLE_TOKENS:
            rv = resolve(tokens, r)
            out.append((deltaE(av, rv), a, av, r, rv))
    return out

def roles_pairs_test(tokens):
    """Пары самого colorRoles.test.ts — они тоже обязаны остаться в силе."""
    src = ROLES_PATH.read_text()
    body = re.search(r"const ПАРЫ:[^=]*= \[([\s\S]*?)\n\];", src).group(1)
    res = []
    for m in re.finditer(r'\{\s*a:\s*"([^"]+)",\s*b:\s*"([^"]+)"', body):
        a, b = m.group(1), m.group(2)
        res.append((deltaE(resolve(tokens, a), resolve(tokens, b)), a, b))
    return res

def report(preset_css_path=None, preset_key=None, verbose=False):
    css = VARS_PATH.read_text()
    if preset_css_path:
        css = css + "\n" + Path(preset_css_path).read_text()
    total_checked = 0
    total_failures = 0
    tightest = []
    for scheme in ("dark", "light"):
        t, rows, ladder, failures = run(css, preset_key, scheme)
        total_checked += len(rows)
        total_failures += len(failures)
        print(f"\n═══ {preset_key or 'база'} / {scheme}: пар {len(rows)}, провалов {len(failures)}")
        for f in failures:
            print("  ✗ " + f)
        margin = sorted(rows, key=lambda r: r[0] / r[1]["min"])
        print("  узкие места (запас к порогу):")
        for ratio, p, ink, bg in margin[:6]:
            print(f'    {ratio:5.2f} / {p["min"]:g}  {p["where"]}  [{p["ink"]}={ink} на {p["bg"]}={bg}]')
            tightest.append((ratio / p["min"], scheme, p["where"], ratio, p["min"]))
        print("  лестница:", ", ".join(f'{s["where"]} {r:.3f}' for r, s in ladder))
        print("  ΔE ролей:")
        for d, a, av, r, rv in sorted(roles(t))[:10]:
            mark = "ok " if d >= РАЗЛИЧИМО else "НИЖЕ"
            print(f"    {mark} ΔE {d:5.1f}  {a} {av} × {r} {rv}")
        print("  ΔE пар colorRoles:", ", ".join(f"{a}×{b} {d:.1f}" for d, a, b in roles_pairs_test(t)))
        if verbose:
            for ratio, p, ink, bg in margin:
                print(f'      {ratio:6.2f}/{p["min"]:g} {p["where"]}')
    print(f"\nИТОГО: проверено {total_checked} пар, провалов {total_failures}")
    return total_failures

if __name__ == "__main__":
    args = sys.argv[1:]
    if args:
        report(args[0], args[1] if len(args) > 1 else None, "-v" in args)
    else:
        report(None, None, "-v" in args)
