"""
Цветовая арифметика и разбор lc-vars.css — ровно так же, как это делают
сторожа проекта: WCAG 2.1 из tokenContrast.test.ts и OKLab ΔE из
colorRoles.test.ts. Ничего не приближаем: каждое число считается здесь.
"""
import math, re, os

VARS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "frontend", "src", "app", "lc-vars.css")

TEXT = 4.5
UI = 3.0
LADDER_MIN = 1.10
EDGE_MIN = 1.8
DELTA_E_MIN = 15.0          # РАЗЛИЧИМО из colorRoles.test.ts

# ─────────────────────────── WCAG 2.1 ───────────────────────────

def _chan(v):
    v = v / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

def rgb(hexstr):
    c = hexstr.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

def luminance(hexstr):
    r, g, b = rgb(hexstr)
    return 0.2126 * _chan(r) + 0.7152 * _chan(g) + 0.0722 * _chan(b)

def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)

def composite(fg, alpha, bg):
    f, b = rgb(fg), rgb(bg)
    mix = [round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3)]
    return "#" + "".join(f"{c:02x}" for c in mix)

# ─────────────────────────── OKLab / OKLCH ───────────────────────────

def _lin(c):
    x = c / 255.0
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4

def oklab(hexstr):
    R, G, B = rgb(hexstr)
    r, g, b = _lin(R), _lin(G), _lin(B)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)

def delta_e(a, b):
    A, B = oklab(a), oklab(b)
    return math.dist(A, B) * 100

def _oklab_to_srgb_float(L, a, b):
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = +4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bb = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return r, g, bb

def _cube(x):
    return x ** 3 if x >= 0 else -((-x) ** 3)

def _oklab_to_srgb(L, a, b):
    l_ = _cube(L + 0.3963377774 * a + 0.2158037573 * b)
    m_ = _cube(L - 0.1055613458 * a - 0.0638541728 * b)
    s_ = _cube(L - 0.0894841775 * a - 1.2914855480 * b)
    r = +4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bb = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return r, g, bb

def _encode(x):
    x = max(0.0, min(1.0, x))
    return 12.92 * x if x <= 0.0031308 else 1.055 * (x ** (1 / 2.4)) - 0.055

def _in_gamut(L, C, h):
    a = C * math.cos(math.radians(h))
    b = C * math.sin(math.radians(h))
    r, g, bb = _oklab_to_srgb(L, a, b)
    return all(-1e-4 <= v <= 1 + 1e-4 for v in (r, g, bb))

def oklch_hex(L, C, h):
    """OKLCH → #hex, с урезанием хромы до попадания в sRGB."""
    if not _in_gamut(L, C, h):
        lo, hi = 0.0, C
        for _ in range(40):
            mid = (lo + hi) / 2
            if _in_gamut(L, mid, h):
                lo = mid
            else:
                hi = mid
        C = lo
    a = C * math.cos(math.radians(h))
    b = C * math.sin(math.radians(h))
    r, g, bb = _oklab_to_srgb(L, a, b)
    out = [round(255 * _encode(v)) for v in (r, g, bb)]
    return "#" + "".join(f"{v:02x}" for v in out)

def hex_for(h, C, y_target):
    """Цвет заданного тона и хромы с ЗАДАННОЙ относительной яркостью WCAG."""
    lo, hi = 0.0, 1.0
    best = "#000000"
    for _ in range(60):
        mid = (lo + hi) / 2
        cand = oklch_hex(mid, C, h)
        y = luminance(cand)
        best = cand
        if y < y_target:
            lo = mid
        else:
            hi = mid
    return best

# ─────────────────────────── разбор lc-vars.css ───────────────────────────

_RAW = open(VARS_PATH, encoding="utf-8").read()

def _blocks(css):
    css = re.sub(r"/\*[\s\S]*?\*/", "", css)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)

def read_tokens(scheme, extra_css="", preset=None):
    """
    ПОВТОРЯЕТ readTokens из tokenContrast.test.ts В РЕДАКЦИИ 04.09 — два прохода.

    Первый проход кладёт БАЗУ (блоки без data-lc-preset), второй — только блоки
    НУЖНОГО пресета И НУЖНОЙ СХЕМЫ. Тёмный блок пресета помечен
    `:not([data-mantine-color-scheme="light"])`, поэтому в светлой схеме он не
    применяется вовсе: светлый блок пресета перекрывает не его, а базовую
    светлую тему. Медиазапросы пропускаются.
    """
    blocks = _blocks(_RAW + "\n" + extra_css)
    m = {}

    def apply(want):
        for selector, body in blocks:
            if "@media" in selector:
                continue
            if not want(selector):
                continue
            for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
                m[name] = value.strip()

    light = scheme == "light"

    # ⚠ ОТЛИЧИЕ ОТ TS-СТОРОЖА, И ОНО НАМЕРЕННОЕ. Там блок считается светлым по
    # ПОДСТРОКЕ `color-scheme="light"`, а она есть и внутри `:not(...)` тёмного
    # блока пресета. Мы моделируем БРАУЗЕР, поэтому «светлый» — это вхождение
    # ВНЕ отрицания. Расхождение вынесено в отчёт: из-за него тёмная схема
    # пресета у TS-сторожа не проверяется вовсе.
    def is_light_selector(selector):
        return re.search(r'(?<!:not\()\[data-mantine-color-scheme="light"\]', selector) is not None

    def base(selector):
        if "data-lc-preset" in selector:
            return False
        return light if is_light_selector(selector) else ":root" in selector

    apply(base)

    if preset:
        mine = f'data-lc-preset="{preset}"'

        def own(selector):
            if mine not in selector:
                return False
            return is_light_selector(selector) == light

        apply(own)

    if len(m) < 100:
        raise SystemExit(f"разобрано подозрительно мало токенов: {len(m)}")
    return m


class Palette:
    def __init__(self, extra_css="", preset=None):
        self.t = {"dark": read_tokens("dark", extra_css, preset),
                  "light": read_tokens("light", extra_css, preset)}

    def resolve(self, name, scheme, depth=0):
        if depth > 12:
            raise SystemExit(f"циклическая ссылка на {name} ({scheme})")
        raw = self.t[scheme].get(name)
        if raw is None:
            raise SystemExit(f"токен {name} не объявлен в схеме {scheme}")
        ref = re.fullmatch(r"var\((--[\w-]+)\)", raw)
        if ref:
            return self.resolve(ref.group(1), scheme, depth + 1)
        return raw

def flatten(value, backdrop, where):
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if m:
        hexv = "#" + "".join(f"{int(m.group(i)):02x}" for i in (1, 2, 3))
        alpha = 1.0 if m.group(4) is None else float(m.group(4))
        return composite(hexv, alpha, backdrop)
    raise SystemExit(f"{where}: значение «{value}» не свести к цвету")
