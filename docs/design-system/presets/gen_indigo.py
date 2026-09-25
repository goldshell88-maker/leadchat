#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка пресета «Индиго». Числа не подбираются на глаз: рампа задаётся в OKLCH
(светлота/цветность/тон), переводится в sRGB с понижением цветности до охвата,
и каждое значение потом проверяется `check_preset.py` по таблице PAIRS.

ТОН ОДИН НА ОБЕ СХЕМЫ (288°) — это одна марка, а не два цвета. Различаются
только СТУПЕНИ: тёмная схема берёт светлый край рампы под тёмные чернила,
светлая — тёмный край под белые. Направление зеркальное, потому что зеркальны
сами темы, и путать их нельзя: светлая ступень на белом фоне не читается.

⚠ ВЕРХ РАМПЫ (50–600) У ОБЕИХ СХЕМ ОДИНАКОВ. Так знак Lead Partner
(--lc-brand = a-500) и счётчик непрочитанных (a-600) остаются одним цветом при
переключении темы — как это устроено в базовой палитре, где рампа общая.
Расходятся только ступени 700–950: в светлой они уходят заметно глубже, и
этому есть счётная причина — см. комментарий к разрыву ниже.
"""
from check_preset import oklch_to_hex, rgb

HUE = 288.0

ВЕРХ = {
    50:  (0.965, 0.020),
    100: (0.930, 0.040),
    200: (0.890, 0.065),
    300: (0.855, 0.085),
    400: (0.815, 0.115),
    500: (0.765, 0.145),
    600: (0.695, 0.175),
}
DARK_RAMP = {**ВЕРХ, 700: (0.560, 0.200), 800: (0.400, 0.170),
             900: (0.300, 0.130), 950: (0.225, 0.095)}
LIGHT_RAMP = {**ВЕРХ, 700: (0.350, 0.220), 800: (0.290, 0.185),
              900: (0.230, 0.145), 950: (0.160, 0.100)}

DARK = {
    "--lc-ink-on-accent": "#070a1c",
    "--lc-bg-app": "#080a10", "--lc-bg-panel": "#0b0e15",
    "--lc-bg-raise": "#1a2130", "--lc-bg-selected": "#141924",
    "--lc-line": "#333b4d", "--lc-line-strong": "#3a4358",
    "--lc-text-1": "#e7eaf2", "--lc-text-2": "#c6cddc", "--lc-text-3": "#949cb0",
    "--lc-text-4": "#7c8497", "--lc-text-5": "#5a6274",
    "--lc-selected": "#1c2440", "--lc-primary-subtle": "#151b3c",
}
LIGHT = {
    "--lc-ink-on-accent": "#070a1c",
    "--lc-bg-0": "#ffffff", "--lc-bg-1": "#eceff7", "--lc-bg-2": "#eceff7",
    "--lc-surface": "#ffffff", "--lc-surface-hover": "#e7ebf5",
    "--lc-border": "#d2d8e8", "--lc-border-strong": "#c6cee2",
    "--lc-text-1": "#0f172a", "--lc-text-2": "#334155", "--lc-text-3": "#475569",
    "--lc-text-4": "#64748b", "--lc-text-5": "#64748b",
    "--lc-selected": "#d7ddfa", "--lc-primary-subtle": "#eef0fe",
}

def ramp(spec):
    return {k: oklch_to_hex(L, C, HUE) for k, (L, C) in spec.items()}

def channels(h):
    return " ".join(str(c) for c in rgb(h))

DARK_HEX, LIGHT_HEX = ramp(DARK_RAMP), ramp(LIGHT_RAMP)

if __name__ == "__main__":
    import sys
    out = []
    for hexes, extra in ((DARK_HEX, DARK), (LIGHT_HEX, LIGHT)):
        sel = ':root[data-lc-preset="indigo"]:not([data-mantine-color-scheme="light"])' \
            if extra is DARK else \
            ':root[data-lc-preset="indigo"][data-mantine-color-scheme="light"]'
        out.append(sel + " {")
        for s in sorted(hexes):
            out.append(f"  --lc-a-{s}: {hexes[s]};")
        out.append(f"  --lc-a-500-rgb: {channels(hexes[500])};")
        out.append(f"  --lc-a-700-rgb: {channels(hexes[700])};")
        for n, v in extra.items():
            out.append(f"  {n}: {v};")
        out.append("}")
    sys.stdout.write("\n".join(out) + "\n")
