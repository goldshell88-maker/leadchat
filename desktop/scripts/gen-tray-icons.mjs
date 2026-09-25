#!/usr/bin/env node
/**
 * Генератор иконок LeadChat для Windows-сборки (04-DESKTOP §1.1, §3.2).
 *
 * ЧИСТЫЙ NODE, БЕЗ ЗАВИСИМОСТЕЙ И ВНЕШНИХ БИНАРЕЙ: внутри — минимальный
 * растеризатор (4x суперсэмплинг), кодировщик PNG (zlib из stdlib) и кодировщик
 * ICO (BMP/DIB-записи, максимально совместимые с rc.exe/Windows).
 * Никакого sharp, ImageMagick, canvas — скрипт запускается на любой машине
 * с Node 18+ и на CI без установки чего-либо.
 *
 *   node scripts/gen-tray-icons.mjs            # перегенерировать всё
 *   npm run icons                              # то же самое
 *
 * Что генерируется (все пути от desktop/src-tauri/):
 *   icons/icon.ico                — иконка приложения (16/24/32/48/64/256, DIB)
 *   icons/32x32.png               — bundle.icon (04 §1.3)
 *   icons/128x128.png             — bundle.icon
 *   icons/128x128@2x.png          — 256px, для установщика/крупных плиток
 *   icons/tray/tray_0.png         — трей без бейджа (app.trayIcon.iconPath)
 *   icons/tray/tray_1..9.png      — трей с числом непрочитанных
 *   icons/tray/tray_9plus.png     — трей с «9+»
 *   icons/tray/badge_1..9.png     — оверлей на кнопке таскбара, 16x16 (04 §3.2)
 *   icons/tray/badge_9plus.png
 *   icons/tray/badge_*@2x.png     — те же оверлеи 32x32 (мониторы 150/200 %)
 *   icons/src/leadchat.svg        — исходная геометрия знака (дизайн-источник)
 *
 * Иконки КОММИТЯТСЯ в репозиторий: рантайм только выбирает готовый файл,
 * растеризация шрифтов в рантайме не нужна (04 §3.2).
 *
 * Фирменные цвета — 10-UX-DESIGN-SYSTEM §1.2 (зелёный #3cc13b, чернила #0c2e0c);
 * красный бейджа — 04 §3.2 (#e03131).
 */

import { deflateSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ICONS = resolve(HERE, "..", "src-tauri", "icons");
const TRAY = join(ICONS, "tray");
const SRC = join(ICONS, "src");

// --- палитра ---------------------------------------------------------------

const GREEN = [0x3c, 0xc1, 0x3b, 255]; // --lc-green-500, знак Lead Partner
const INK = [0x0c, 0x2e, 0x0c, 255]; // --lc-primary-ink, монограмма на зелёном
const RED = [0xe0, 0x31, 0x31, 255]; // бейдж непрочитанных (04 §3.2)
const WHITE = [0xff, 0xff, 0xff, 255];

// --- пиксельные шрифты -----------------------------------------------------
// Растеризовать TTF без зависимостей невозможно, поэтому цифры — собственный
// пиксельный шрифт: на 16–32 px он читается лучше сглаженного контура.

const FONT_5x7 = {
  "0": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
  "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
  "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
  "3": [".###.", "#...#", "....#", "..##.", "....#", "#...#", ".###."],
  "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
  "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
  "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
  "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
  "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
  "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
  "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
  L: ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
  C: [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
  P: ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
};

const FONT_3x5 = {
  "0": ["###", "#.#", "#.#", "#.#", "###"],
  "1": [".#.", "##.", ".#.", ".#.", "###"],
  "2": ["###", "..#", "###", "#..", "###"],
  "3": ["###", "..#", "###", "..#", "###"],
  "4": ["#.#", "#.#", "###", "..#", "..#"],
  "5": ["###", "#..", "###", "..#", "###"],
  "6": ["###", "#..", "###", "#.#", "###"],
  "7": ["###", "..#", "..#", "..#", "..#"],
  "8": ["###", "#.#", "###", "#.#", "###"],
  "9": ["###", "#.#", "###", "..#", "###"],
  "+": ["...", ".#.", "###", ".#.", "..."],
};

// --- растеризатор ----------------------------------------------------------
// Хранит премультиплицированную RGBA в float, рендерит в SS раз крупнее и
// усредняет — простое, но достаточное сглаживание для 16–256 px.

const SS = 4;

class Canvas {
  constructor(size) {
    this.size = size;
    this.w = size * SS;
    this.h = size * SS;
    this.buf = new Float64Array(this.w * this.h * 4); // premultiplied RGBA, 0..1
  }

  /** Композит «source-over» одного сэмпла. */
  #blend(i, [r, g, b, a]) {
    const sa = a / 255;
    const sr = (r / 255) * sa;
    const sg = (g / 255) * sa;
    const sb = (b / 255) * sa;
    const inv = 1 - sa;
    this.buf[i] = sr + this.buf[i] * inv;
    this.buf[i + 1] = sg + this.buf[i + 1] * inv;
    this.buf[i + 2] = sb + this.buf[i + 2] * inv;
    this.buf[i + 3] = sa + this.buf[i + 3] * inv;
  }

  /** Залить область по предикату «точка внутри» (координаты — в единицах size). */
  fill(color, inside) {
    for (let py = 0; py < this.h; py++) {
      const y = (py + 0.5) / SS;
      for (let px = 0; px < this.w; px++) {
        const x = (px + 0.5) / SS;
        if (inside(x, y)) this.#blend((py * this.w + px) * 4, color);
      }
    }
  }

  roundRect(x, y, w, h, r, color) {
    this.fill(color, (px, py) => {
      if (px < x || py < y || px > x + w || py > y + h) return false;
      const cx = Math.min(Math.max(px, x + r), x + w - r);
      const cy = Math.min(Math.max(py, y + r), y + h - r);
      const dx = px - cx;
      const dy = py - cy;
      return dx * dx + dy * dy <= r * r;
    });
  }

  circle(cx, cy, r, color) {
    this.fill(color, (px, py) => {
      const dx = px - cx;
      const dy = py - cy;
      return dx * dx + dy * dy <= r * r;
    });
  }

  /** Прямоугольник по целым координатам — попадает ровно в пиксели (без размытия). */
  rect(x, y, w, h, color) {
    this.fill(color, (px, py) => px >= x && px < x + w && py >= y && py < y + h);
  }

  /** Пиксельный глиф: каждая ячейка — квадрат scale×scale. */
  glyph(font, ch, x0, y0, scale, color) {
    const rows = font[ch];
    if (!rows) throw new Error(`нет глифа «${ch}» в шрифте`);
    rows.forEach((row, ry) => {
      [...row].forEach((cell, rx) => {
        if (cell === "#") {
          this.rect(x0 + rx * scale, y0 + ry * scale, scale, scale, color);
        }
      });
    });
  }

  /** Ширина строки в пикселях итогового изображения (gap — межбуквенный зазор в ячейках). */
  static textWidth(font, text, scale, gap = 1) {
    const gw = font[text[0]][0].length;
    return text.length * gw * scale + (text.length - 1) * gap * scale;
  }

  text(font, str, x0, y0, scale, color, gap = 1) {
    const gw = font[str[0]][0].length;
    let x = x0;
    for (const ch of str) {
      this.glyph(font, ch, x, y0, scale, color);
      x += (gw + gap) * scale;
    }
  }

  /** Даунсэмплинг в 8-битную straight-alpha RGBA. */
  toRGBA() {
    const out = Buffer.alloc(this.size * this.size * 4);
    const n = SS * SS;
    for (let y = 0; y < this.size; y++) {
      for (let x = 0; x < this.size; x++) {
        let r = 0;
        let g = 0;
        let b = 0;
        let a = 0;
        for (let sy = 0; sy < SS; sy++) {
          for (let sx = 0; sx < SS; sx++) {
            const i = ((y * SS + sy) * this.w + (x * SS + sx)) * 4;
            r += this.buf[i];
            g += this.buf[i + 1];
            b += this.buf[i + 2];
            a += this.buf[i + 3];
          }
        }
        r /= n;
        g /= n;
        b /= n;
        a /= n;
        const o = (y * this.size + x) * 4;
        if (a > 0) {
          out[o] = Math.round(Math.min(1, r / a) * 255);
          out[o + 1] = Math.round(Math.min(1, g / a) * 255);
          out[o + 2] = Math.round(Math.min(1, b / a) * 255);
          out[o + 3] = Math.round(a * 255);
        }
      }
    }
    return out;
  }
}

// --- PNG -------------------------------------------------------------------

const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = -1;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function pngChunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function encodePNG(size, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  ihdr[10] = 0; // deflate
  ihdr[11] = 0; // adaptive filtering
  ihdr[12] = 0; // no interlace

  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (stride + 1)] = 0; // filter: None — картинки крошечные, сжатие и так ок
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk("IHDR", ihdr),
    pngChunk("IDAT", deflateSync(raw, { level: 9 })),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

// --- ICO (DIB-записи: понимает и rc.exe, и проводник) ----------------------

function icoImage(size, rgba) {
  const header = Buffer.alloc(40);
  header.writeUInt32LE(40, 0); // biSize
  header.writeInt32LE(size, 4); // biWidth
  header.writeInt32LE(size * 2, 8); // biHeight = XOR + AND
  header.writeUInt16LE(1, 12); // biPlanes
  header.writeUInt16LE(32, 14); // biBitCount
  header.writeUInt32LE(0, 16); // BI_RGB

  const xor = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    const srcRow = size - 1 - y; // DIB хранится снизу вверх
    for (let x = 0; x < size; x++) {
      const s = (srcRow * size + x) * 4;
      const d = (y * size + x) * 4;
      xor[d] = rgba[s + 2]; // B
      xor[d + 1] = rgba[s + 1]; // G
      xor[d + 2] = rgba[s]; // R
      xor[d + 3] = rgba[s + 3]; // A
    }
  }

  const maskStride = Math.ceil(size / 32) * 4; // 1bpp, выравнивание 4 байта
  const and = Buffer.alloc(maskStride * size, 0); // прозрачность берётся из альфы
  header.writeUInt32LE(xor.length + and.length, 20); // biSizeImage
  return Buffer.concat([header, xor, and]);
}

function encodeICO(images) {
  const dir = Buffer.alloc(6);
  dir.writeUInt16LE(0, 0);
  dir.writeUInt16LE(1, 2); // 1 = icon
  dir.writeUInt16LE(images.length, 4);

  const entries = [];
  const blobs = [];
  let offset = 6 + images.length * 16;
  for (const { size, data } of images) {
    const e = Buffer.alloc(16);
    e[0] = size >= 256 ? 0 : size;
    e[1] = size >= 256 ? 0 : size;
    e[2] = 0; // палитра не используется
    e[3] = 0;
    e.writeUInt16LE(1, 4); // planes
    e.writeUInt16LE(32, 6); // bpp
    e.writeUInt32LE(data.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += data.length;
    entries.push(e);
    blobs.push(data);
  }
  return Buffer.concat([dir, ...entries, ...blobs]);
}

// --- подбор размера цифры под круг бейджа ----------------------------------
// Цифра не должна вылезать за круг: проверяем углы КАЖДОЙ закрашенной ячейки
// (у «1» и «7» углы пустые, им достаётся больший кегль). Из двух шрифтов
// выбираем тот, что даёт более высокую надпись при том же радиусе.

function glyphFits(font, text, scale, r, gap) {
  const gw = font[text[0]][0].length;
  const gh = font[text[0]].length;
  const width = Canvas.textWidth(font, text, scale, gap);
  const y0 = -(gh * scale) / 2;
  let x = -width / 2;
  for (const ch of text) {
    const rows = font[ch];
    for (let ry = 0; ry < rows.length; ry++) {
      for (let rx = 0; rx < rows[ry].length; rx++) {
        if (rows[ry][rx] !== "#") continue;
        const cx = x + rx * scale;
        const cy = y0 + ry * scale;
        for (const [px, py] of [
          [cx, cy],
          [cx + scale, cy],
          [cx, cy + scale],
          [cx + scale, cy + scale],
        ]) {
          if (Math.hypot(px, py) > r) return false;
        }
      }
    }
    x += (gw + gap) * scale;
  }
  return true;
}

/** Максимальный кегль, при котором ВСЕ подписи набора влезают в круг радиуса r. */
function fitText(labels, r, gap = 1) {
  let best = null;
  for (const font of [FONT_5x7, FONT_3x5]) {
    for (let scale = 8; scale >= 1; scale--) {
      if (labels.every((t) => glyphFits(font, t, scale, r, gap))) {
        const height = font[labels[0][0]].length * scale;
        if (!best || height > best.height) best = { font, scale, height, gap };
        break;
      }
    }
  }
  if (!best) throw new Error(`цифра не влезает в круг r=${r}`);
  return best;
}

/** Нарисовать подпись по центру круга выбранным кеглем. */
function centeredText(canvas, fit, text, cx, cy, color) {
  const width = Canvas.textWidth(fit.font, text, fit.scale, fit.gap);
  const height = fit.font[text[0]].length * fit.scale;
  canvas.text(fit.font, text, cx - width / 2, cy - height / 2, fit.scale, color, fit.gap);
}

const DIGITS = [..."123456789"];

// --- знак LeadChat ---------------------------------------------------------

/** Базовый знак: зелёный скруглённый квадрат + монограмма «LC» чернилами. */
function baseMark(size) {
  const c = new Canvas(size);
  c.roundRect(0, 0, size, size, size / 4, GREEN);
  const scale = Math.max(1, Math.round(size / 16));
  const w = Canvas.textWidth(FONT_5x7, "LC", scale);
  const h = 7 * scale;
  c.text(FONT_5x7, "LC", Math.round((size - w) / 2), Math.round((size - h) / 2), scale, INK);
  return c;
}

/** Иконка трея 32x32: знак + красный бейдж в правом нижнем углу. */
// Бейдж прижат в правый нижний угол: монограмма остаётся узнаваемой, а на 16 px
// (реальный размер трея при 100 % DPI) главный сигнал — сам красный кружок.
const TRAY_BADGE = { cx: 23, cy: 23, ring: 9.8, r: 8.5 };
const TRAY_FIT = {
  digit: fitText(DIGITS, TRAY_BADGE.r),
  plus: fitText(["9+"], TRAY_BADGE.r, 0),
};

function trayIcon(label) {
  const c = baseMark(32);
  if (label !== null) {
    const { cx, cy, ring, r } = TRAY_BADGE;
    c.circle(cx, cy, ring, WHITE); // белое кольцо — отрыв бейджа от зелёного
    c.circle(cx, cy, r, RED);
    centeredText(c, label === "9+" ? TRAY_FIT.plus : TRAY_FIT.digit, label, cx, cy, WHITE);
  }
  return c;
}

/** Оверлей на кнопке таскбара: сплошной красный круг с числом. */
function badgeIcon(label, size) {
  const c = new Canvas(size);
  const ring = Math.max(1, size / 16); // белый кант — читается на тёмной панели
  const r = size / 2 - ring;
  c.circle(size / 2, size / 2, size / 2, WHITE);
  c.circle(size / 2, size / 2, r, RED);
  const fit = label === "9+" ? fitText(["9+"], r, 0) : fitText(DIGITS, r);
  centeredText(c, fit, label, size / 2, size / 2, WHITE);
  return c;
}

// --- дизайн-источник (SVG) -------------------------------------------------

const BASE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <!-- Дизайн-источник знака LeadChat (совпадает с frontend/public/favicon.svg).
       PNG/ICO из этого файла НЕ растеризуются: scripts/gen-tray-icons.mjs
       воспроизводит ту же геометрию собственным растеризатором, без зависимостей.
       Если знак меняется — правьте и SVG, и константы в скрипте. -->
  <rect width="32" height="32" rx="8" fill="#3cc13b"/>
  <text x="16" y="21.5" font-family="Inter, Roboto, sans-serif" font-size="13"
        font-weight="700" fill="#0c2e0c" text-anchor="middle">LC</text>
</svg>
`;

// --- сборка ----------------------------------------------------------------

function write(path, buf) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, buf);
  console.log(`  ${path.replace(resolve(HERE, ".."), "desktop")}  ${buf.length} B`);
}

function main() {
  console.log("LeadChat: генерация иконок (чистый Node, без зависимостей)");

  mkdirSync(TRAY, { recursive: true });
  mkdirSync(SRC, { recursive: true });
  write(join(SRC, "leadchat.svg"), Buffer.from(BASE_SVG, "utf8"));

  // иконки приложения
  for (const [size, name] of [
    [32, "32x32.png"],
    [128, "128x128.png"],
    [256, "128x128@2x.png"],
  ]) {
    write(join(ICONS, name), encodePNG(size, baseMark(size).toRGBA()));
  }

  const icoSizes = [16, 24, 32, 48, 64, 256];
  write(
    join(ICONS, "icon.ico"),
    encodeICO(icoSizes.map((size) => ({ size, data: icoImage(size, baseMark(size).toRGBA()) }))),
  );

  // трей: 0 (без бейджа), 1..9, 9plus
  write(join(TRAY, "tray_0.png"), encodePNG(32, trayIcon(null).toRGBA()));
  for (let n = 1; n <= 9; n++) {
    write(join(TRAY, `tray_${n}.png`), encodePNG(32, trayIcon(String(n)).toRGBA()));
  }
  write(join(TRAY, "tray_9plus.png"), encodePNG(32, trayIcon("9+").toRGBA()));

  // оверлеи таскбара: 16x16 (100 %) и 32x32 (@2x — 150/200 %)
  const labels = [...Array(9)].map((_, i) => [String(i + 1), String(i + 1)]);
  labels.push(["9+", "9plus"]);
  for (const [label, name] of labels) {
    write(join(TRAY, `badge_${name}.png`), encodePNG(16, badgeIcon(label, 16).toRGBA()));
    write(join(TRAY, `badge_${name}@2x.png`), encodePNG(32, badgeIcon(label, 32).toRGBA()));
  }

  console.log("Готово. Файлы коммитятся в репозиторий (04 §3.2).");
}

main();
