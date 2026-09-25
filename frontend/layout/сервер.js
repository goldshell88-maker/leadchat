/**
 * Статика стенда адаптива. НЕ ЧАСТЬ ПРОДУКТА.
 *
 * Отдаёт каталог `.dump` на 8742. Нужен ровно потому, что стенд подключает
 * шрифты (`./assets/inter-*.woff2`), а Chromium по `file://` их не берёт:
 * шрифт — ресурс с проверкой источника, и по файловому протоколу источника у
 * него нет. Без шрифта метрики текста другие, и мерка соврала бы в обе
 * стороны — то придумав обрезку, то не заметив настоящую.
 *
 * Поднимается автоматически (`webServer` в layout/playwright.config.ts).
 * ⚠ РАСШИРЕНИЕ `.js`, А НЕ `.mjs`: у пакета объявлен `"type": "module"`,
 * поэтому `.js` здесь и так модуль, — зато под правило линтера для файлов с
 * расширением js (глобальные имена Node) он попадает, а `.mjs` не попадал и
 * давал четыре ошибки `no-undef` на ровном месте.
 *
 * Вручную: node layout/сервер.js
 */
import { createServer } from "node:http";
import { createReadStream, readFileSync, statSync } from "node:fs";
import { extname, join, normalize, resolve } from "node:path";

const КОРЕНЬ = resolve(new URL("../.dump", import.meta.url).pathname);
const ПОРТ = Number(process.env.LAYOUT_PORT ?? 8742);

/**
 * Боевая политика возможностей — из того же файла, что уезжает на сервер.
 *
 * ⚠ ЗАЧЕМ СТЕНДУ БОЕВОЙ ЗАГОЛОВОК (08.09). Незнакомое имя возможности браузер
 * НЕ пропускает молча: на `ambient-light-sensor` Chrome пишет в консоль
 * «Unrecognized feature» при каждой загрузке — у каждого человека, весь день.
 * Это выяснилось не проверкой, а жалобой диспетчера в тот же вечер.
 *
 * Читаем из сниппета, а не переписываем сюда: копия разошлась бы с боем на
 * первой же правке, и сторож стерёг бы вчерашний список.
 */
function политикаВозможностей() {
  const путь = resolve(
    new URL("../../docker/nginx/snippets/security-headers.conf", import.meta.url).pathname,
  );
  const текст = readFileSync(путь, "utf8").replace(/^\s*#.*$/gm, " ");
  return /add_header\s+Permissions-Policy\s+"([^"]+)"/.exec(текст)?.[1] ?? "";
}

const ПОЛИТИКА = политикаВозможностей();

const ТИПЫ = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".woff2": "font/woff2",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

createServer((req, res) => {
  // Запрос приходит с `?` (кэш) — путь берём без него. `normalize` плюс
  // проверка префикса не дают выйти из `.dump` через `../`.
  const путь = decodeURIComponent(new URL(req.url ?? "/", "http://x").pathname);
  const файл = normalize(join(КОРЕНЬ, путь));
  if (!файл.startsWith(КОРЕНЬ)) {
    res.writeHead(403).end("выход за .dump");
    return;
  }
  let размер;
  try {
    размер = statSync(файл).size;
  } catch {
    // Отсутствие стенда — не мелочь: значит снималка не отработала, и молчать
    // об этом нельзя. Сторож ловит это по коду 404.
    res.writeHead(404).end(`нет файла: ${путь}`);
    return;
  }
  res.writeHead(200, {
    "content-type": ТИПЫ[extname(файл)] ?? "application/octet-stream",
    "content-length": размер,
    "cache-control": "no-store",
    ...(ПОЛИТИКА ? { "permissions-policy": ПОЛИТИКА } : {}),
  });
  createReadStream(файл).pipe(res);
}).listen(ПОРТ, () => {
  process.stdout.write(`стенд адаптива: http://127.0.0.1:${ПОРТ}/\n`);
});
