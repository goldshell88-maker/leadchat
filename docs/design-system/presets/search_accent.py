#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Совместная оптимизация обеих рамп. Сравниваем НЕ только «опорную» ступень:
в один кадр с ролями попадают и заливка кнопки, и знак, и счётчик, и текст
акцентом. Первый черновик это и подвёл — заливка главной кнопки в тёмной
схеме встала на 3.6 ΔE от фиолетового бота, то есть буквально его цветом.
"""
import itertools
from check_preset import oklch_to_hex, deltaE, contrast, luminance

INK_D = "#070a1c"; INK_L = "#070a1c"
TXT_D = "#e7eaf2"; TXT_L = "#0f172a"
PANEL_D = "#0b0e15"; SURF_D = "#1a2130"; SEL_D = "#1c2440"
BG1_L = "#eceff7"; SEL_L = "#d7ddfa"
ROLES_D = {"link": "#60a5fa", "info": "#3b82f6", "bot": "#8b5cf6"}
ROLES_L = {"link": "#1d4ed8", "info": "#2563eb", "bot": "#7c3aed"}

def dark(h, L3, L4, L5, L6, L8, C):
    a = {k: oklch_to_hex(v, C, h) for k, v in
         {300: L3, 400: L4, 500: L5, 600: L6, 800: L8}.items()}
    if min(contrast(INK_D, a[s]) for s in (400, 500, 600)) < 4.55: return None
    if contrast(a[500], PANEL_D) < 3.05 or contrast(a[500], SEL_D) < 3.05: return None
    if min(contrast(a[300], SURF_D), contrast(a[300], SEL_D)) < 4.55: return None
    if contrast(a[400], SURF_D) < 3.05: return None
    if contrast("#ffffff", a[800]) < 4.55: return None
    if contrast(a[500], a[800]) < 3.05: return None          # фокус внутри пузыря
    if contrast(a[400], a[600]) < 1.30: return None          # наведение обязано быть видно
    ds = {f"a{s}/{r}": deltaE(a[s], v) for s in (500, 600, 400) for r, v in ROLES_D.items()}
    ds["a300/текст"] = deltaE(a[300], TXT_D)
    ds["a300/link"] = deltaE(a[300], ROLES_D["link"])
    return a, ds

def light(h, L5, L6, L7, L8, L9, C):
    a = {k: oklch_to_hex(v, C, h) for k, v in
         {500: L5, 600: L6, 700: L7, 800: L8, 900: L9}.items()}
    if min(contrast("#ffffff", a[s]) for s in (700, 800, 900)) < 4.55: return None
    if min(contrast(INK_L, a[s]) for s in (500, 600)) < 4.55: return None
    if contrast(a[700], BG1_L) < 3.05 or contrast(a[700], SEL_L) < 3.05: return None
    if contrast(a[800], BG1_L) < 4.55: return None
    ds = {f"a{s}/{r}": deltaE(a[s], v) for s in (700, 800, 500, 600) for r, v in ROLES_L.items()}
    ds["a800/текст"] = deltaE(a[800], TXT_L)
    return a, ds

print("ТЁМНАЯ")
res = []
for h in range(276, 297, 2):
    for L6 in [x/100 for x in range(60, 76)]:
        for st in (0.035, 0.045, 0.055):
            L5, L4, L3 = L6+st, L6+2*st, L6+3*st
            if L3 > 0.90: continue
            for L8 in (0.36, 0.40, 0.44):
                for C in (0.14, 0.17, 0.20, 0.24):
                    r = dark(h, L3, L4, L5, L6, L8, C)
                    if r: res.append((min(r[1].values()), h, L6, st, L8, C, r))
res.sort(reverse=True)
for m, h, L6, st, L8, C, (a, ds) in res[:6]:
    worst = sorted(ds.items(), key=lambda x: x[1])[:4]
    print(f" min {m:5.1f} h{h} L600={L6:.2f} шаг{st} L800={L8} C{C} | a600 {a[600]} a500 {a[500]} "
          f"a400 {a[400]} a300 {a[300]} a800 {a[800]} | " + " ".join(f"{k} {v:.1f}" for k, v in worst))

print("\nСВЕТЛАЯ")
res = []
for h in range(276, 297, 2):
    for L7 in [x/100 for x in range(30, 50)]:
        for L6 in [x/100 for x in range(58, 72)]:
            L8, L9 = L7-0.06, L7-0.12
            L5 = L6 + 0.05
            for C in (0.14, 0.17, 0.20, 0.24):
                r = light(h, L5, L6, L7, L8, L9, C)
                if r: res.append((min(r[1].values()), h, L7, L6, C, r))
res.sort(reverse=True)
for m, h, L7, L6, C, (a, ds) in res[:6]:
    worst = sorted(ds.items(), key=lambda x: x[1])[:4]
    print(f" min {m:5.1f} h{h} L700={L7:.2f} L600={L6:.2f} C{C} | a700 {a[700]} a800 {a[800]} "
          f"a900 {a[900]} a500 {a[500]} a600 {a[600]} | " + " ".join(f"{k} {v:.1f}" for k, v in worst))
