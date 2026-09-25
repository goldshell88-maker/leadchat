#!/usr/bin/env node
/**
 * Генератор манифеста автообновления `latest.json` (04-DESKTOP §6.1, §7).
 *
 * Читает версию из `tauri.conf.json`, находит в каталоге NSIS-сборки
 * `*-setup.exe` и его minisign-подпись `*-setup.exe.sig` (их кладёт
 * `createUpdaterArtifacts: true`) и пишет манифест формата Tauri 2 updater:
 *
 *   { version, notes, pub_date, platforms: { "windows-x86_64": { signature, url } } }
 *
 * Чистый Node без зависимостей — в CI на windows-latest ставить нечего.
 *
 * Использование:
 *   node desktop/scripts/make-latest-json.mjs
 *   node desktop/scripts/make-latest-json.mjs --notes "Быстрый ответ из тоста" --dry-run
 *
 * Опции (у каждой есть переменная окружения — так удобнее в GitHub Actions):
 *   --conf <path>        tauri.conf.json                (TAURI_CONF)
 *   --bundle <dir>       каталог NSIS-артефактов        (NSIS_BUNDLE_DIR)
 *   --out <path>         куда писать манифест           (LATEST_JSON_OUT)
 *   --base-url <url>     база ссылок на /download/      (DOWNLOAD_BASE_URL)
 *   --notes <text>       строка «что нового»            (RELEASE_NOTES)
 *   --notes-file <path>  то же, но из файла             (RELEASE_NOTES_FILE)
 *   --version <x.y.z>    переопределить версию          (RELEASE_VERSION)
 *   --pub-date <iso8601> зафиксировать дату             (RELEASE_PUB_DATE)
 *   --dry-run            только показать результат
 *   --help
 *
 * Коды выхода: 0 — успех, 1 — ошибка входных данных (не собранный бандл,
 * отсутствующая подпись, рассинхрон версий).
 */

import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
/** desktop/scripts → desktop */
const DESKTOP_DIR = resolve(SCRIPT_DIR, "..");
/** Раздача с VPS: 04 §6.1 + 05-DEPLOY §3. */
const DEFAULT_BASE_URL = "https://chat.partner-lead-centre.ru/download/";
/** Единственная поддерживаемая цель: обновление ездит на NSIS x64 (04 §6.1). */
const PLATFORM_KEY = "windows-x86_64";
const SEMVER_RE = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

class UserError extends Error {}

function parseArgs(argv) {
  const flags = new Set(["--dry-run", "--help", "-h"]);
  const options = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--") && arg !== "-h") {
      throw new UserError(`неизвестный аргумент: ${arg}`);
    }
    if (flags.has(arg)) {
      options[arg.replace(/^-+/, "")] = true;
      continue;
    }
    const value = argv[i + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new UserError(`у опции ${arg} нет значения`);
    }
    options[arg.slice(2)] = value;
    i += 1;
  }
  return options;
}

function pick(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).length > 0) return String(value);
  }
  return undefined;
}

function toPath(value, fallback) {
  const raw = value ?? fallback;
  return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
}

function readJson(path) {
  if (!existsSync(path)) throw new UserError(`не найден файл конфигурации: ${path}`);
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new UserError(`не разобрать JSON ${path}: ${error.message}`);
  }
}

/**
 * Ищет ровно один `*-setup.exe`. Если сборок несколько (остались артефакты
 * прошлых версий) — берём совпадающую по версии, иначе честно падаем: тихо
 * выбрать не тот инсталлятор хуже, чем упасть в CI.
 */
function findSetup(bundleDir, version) {
  if (!existsSync(bundleDir)) {
    throw new UserError(
      `каталог NSIS-сборки не найден: ${bundleDir}\n` +
        "сначала соберите бандл: npm run --prefix desktop tauri build",
    );
  }
  const candidates = readdirSync(bundleDir).filter((name) => name.toLowerCase().endsWith("-setup.exe"));
  if (candidates.length === 0) {
    throw new UserError(`в ${bundleDir} нет *-setup.exe`);
  }
  if (candidates.length === 1) return candidates[0];

  const matching = candidates.filter((name) => name.includes(version));
  if (matching.length === 1) return matching[0];
  throw new UserError(
    `в ${bundleDir} несколько инсталляторов, неясно какой брать:\n  ${candidates.join("\n  ")}\n` +
      "почистите каталог сборки или укажите --version",
  );
}

function readSignature(bundleDir, setupName) {
  const sigPath = join(bundleDir, `${setupName}.sig`);
  if (!existsSync(sigPath)) {
    throw new UserError(
      `нет подписи ${sigPath}\n` +
        "проверьте createUpdaterArtifacts: true в tauri.conf.json и секреты " +
        "TAURI_SIGNING_PRIVATE_KEY / TAURI_SIGNING_PRIVATE_KEY_PASSWORD в CI",
    );
  }
  const signature = readFileSync(sigPath, "utf8").trim();
  if (signature.length === 0) throw new UserError(`подпись пуста: ${sigPath}`);
  if (!/^[A-Za-z0-9+/=\s]+$/.test(signature)) {
    throw new UserError(`подпись не похожа на base64: ${sigPath}`);
  }
  if (signature.length < 40) {
    throw new UserError(`подпись подозрительно короткая (${signature.length} символов): ${sigPath}`);
  }
  return signature;
}

function readNotes(options) {
  const inline = pick(options.notes, process.env.RELEASE_NOTES);
  if (inline) return inline.trim();

  const notesFile = pick(options["notes-file"], process.env.RELEASE_NOTES_FILE);
  if (notesFile) {
    const path = toPath(notesFile, notesFile);
    if (!existsSync(path)) throw new UserError(`файл с описанием релиза не найден: ${path}`);
    return readFileSync(path, "utf8").trim();
  }
  return "";
}

function normalizeBaseUrl(raw) {
  const base = raw.endsWith("/") ? raw : `${raw}/`;
  let parsed;
  try {
    parsed = new URL(base);
  } catch {
    throw new UserError(`некорректный --base-url: ${raw}`);
  }
  if (parsed.protocol !== "https:") {
    // Канал обновлений — только https: подпись защищает содержимое, TLS — сам факт запроса.
    throw new UserError(`--base-url должен быть https, получено: ${raw}`);
  }
  return base;
}

/** Тег `desktop-v1.4.2` обязан совпадать с версией в конфиге (04 §7). */
function assertTagMatches(version) {
  const ref = process.env.GITHUB_REF_NAME;
  if (!ref || !ref.startsWith("desktop-v")) return;
  const tagVersion = ref.slice("desktop-v".length);
  if (tagVersion !== version) {
    throw new UserError(`версия в tauri.conf.json (${version}) не совпадает с тегом ${ref}`);
  }
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help || options.h) {
    process.stdout.write(`${readFileSync(fileURLToPath(import.meta.url), "utf8").split("*/")[0]}*/\n`);
    return;
  }

  const confPath = toPath(
    pick(options.conf, process.env.TAURI_CONF),
    join(DESKTOP_DIR, "src-tauri", "tauri.conf.json"),
  );
  const conf = readJson(confPath);

  const version = pick(options.version, process.env.RELEASE_VERSION, conf.version);
  if (!version) throw new UserError(`в ${confPath} нет поля version`);
  if (!SEMVER_RE.test(version)) throw new UserError(`версия «${version}» не semver`);
  assertTagMatches(version);

  const bundleDir = toPath(
    pick(options.bundle, process.env.NSIS_BUNDLE_DIR),
    join(DESKTOP_DIR, "src-tauri", "target", "release", "bundle", "nsis"),
  );
  const setupName = findSetup(bundleDir, version);
  const signature = readSignature(bundleDir, setupName);

  if (!setupName.includes(version)) {
    throw new UserError(
      `имя инсталлятора «${setupName}» не содержит версию ${version} — ` +
        "похоже, бандл собран из другой версии конфига",
    );
  }

  const baseUrl = normalizeBaseUrl(
    pick(options["base-url"], process.env.DOWNLOAD_BASE_URL) ?? DEFAULT_BASE_URL,
  );
  const pubDate = pick(options["pub-date"], process.env.RELEASE_PUB_DATE) ?? new Date().toISOString();
  if (Number.isNaN(Date.parse(pubDate))) throw new UserError(`некорректная дата: ${pubDate}`);

  const manifest = {
    version,
    notes: readNotes(options),
    pub_date: pubDate,
    platforms: {
      [PLATFORM_KEY]: {
        signature,
        url: `${baseUrl}${encodeURIComponent(setupName)}`,
      },
    },
  };

  const outPath = toPath(
    pick(options.out, process.env.LATEST_JSON_OUT),
    join(DESKTOP_DIR, "..", "dist-release", "latest.json"),
  );
  const body = `${JSON.stringify(manifest, null, 2)}\n`;

  const setupSize = statSync(join(bundleDir, setupName)).size;
  process.stdout.write(
    [
      `версия:      ${version}`,
      `инсталлятор: ${setupName} (${(setupSize / 1024 / 1024).toFixed(1)} МБ)`,
      `подпись:     ${signature.length} символов base64`,
      `url:         ${manifest.platforms[PLATFORM_KEY].url}`,
      `pub_date:    ${pubDate}`,
      `notes:       ${manifest.notes || "(пусто)"}`,
      "",
    ].join("\n"),
  );

  if (options["dry-run"]) {
    process.stdout.write(`--dry-run: манифест не записан\n${body}`);
    return;
  }

  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, body, "utf8");
  process.stdout.write(`манифест записан: ${outPath}\n`);
}

try {
  main();
} catch (error) {
  if (error instanceof UserError) {
    process.stderr.write(`make-latest-json: ${error.message}\n`);
    process.exit(1);
  }
  throw error;
}
