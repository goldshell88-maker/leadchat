// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts и cardRecipe.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * ЦЕЛЬНОСТЬ ОФОРМЛЕНИЯ МЕЖДУ РАЗДЕЛАМИ (аудит 06.09).
 *
 * Четыре расхождения, каждое найдено по файлу и строке:
 *
 *   1. Обвязка страницы. У каждого раздела был свой внешний отступ — чаты 24,
 *      настройки 16/24, статистика 16/24/32, каналы 24, лента 24, журнал 16,
 *      «что нового» 32/24/64. Общая шапка при переходе прыгала по вертикали
 *      на 8–16 px. Теперь пара токенов `--lc-page-pad-*` и класс `.lc-page`.
 *   2. Карточки на разных ступенях: `--lc-bg-1` без тени у профиля и потолка
 *      раздачи, `--lc-surface` у остальных, причём две с `--lc-shadow-1`.
 *      Теперь один рецепт `.lc-card`, а причина исключения (поле ввода
 *      сливается с карточкой) снята там же — полю на карточке дана своя ступень.
 *   3. Шапки таблиц: вес 400 в «Диалогах бота», 500 в журнале, у менеджеров и
 *      быстрых ответов, 600 у Mantine-таблиц. Теперь `.lc-table th` рядом с
 *      `.mantine-Table-th`, частные веса убраны.
 *   4. Экраны входа, приглашения, восстановления и отказа набирали кегль
 *      числом (24, 22, 16) и ширину числом (520) мимо шкалы.
 *
 * ⚠ ПОЧЕМУ ПО ИСХОДНИКАМ. В vitest стоит `css: false`: в jsdom стилей нет,
 * ни отступ, ни тень померить нечем. Проверяется то, что проверяемо, — какой
 * токен и какой класс взял себе каждый носитель.
 *
 * ⚠ КОММЕНТАРИИ СРЕЗАЮТСЯ ДО РАЗБОРА ПРАВИЛ. Внутри комментариев встречаются
 * `{…}` и `padding: 24px` словами; иначе `[^}]*` обрезается на фигурной скобке
 * из объяснения, и сторож судит не то правило (грабли surfaceLadder).
 *
 * ЧЕГО СТОРОЖ НЕ ЗАКРЫВАЕТ — вслух, чтобы не считали закрытым:
 *   - `.dist__cap` (distribution.css) остаётся на `--lc-bg-1`: его держит
 *     сторож DistributionAdaptive0509 и разметка DistributionTab.tsx, которую
 *     правят соседи. Причина исключения уже снята в `.lc-card`; перевод —
 *     следующим шагом вместе с тем сторожем.
 *   - решётка «люди × каналы» (`.op-grid__table`) — матрица с прилипшими
 *     осями, а не таблица данных; её шапка живёт в operators-grid.css.
 *   - «Чаты»: сетка колонок с `--lc-gutter`, шапки страницы там нет — прыгать
 *     нечему.
 */

const БАЗА = "src/app/lc-base.css";
const ТОКЕНЫ = "src/app/lc-vars.css";

function безКомментариев(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ");
}

function css(path: string): string {
  return безКомментариев(readFileSync(path, "utf-8") as string);
}

/** Код без комментариев: в шапках файлов классы называют словами. */
function tsx(path: string): string {
  return (readFileSync(path, "utf-8") as string)
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, " ")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/^\s*\/\/.*$/gm, "");
}

/**
 * Все тела правил, среди селекторов которых есть ровно `selector` — и внутри
 * `@media` тоже: мобильный отступ у корня раздела такой же корень.
 */
function правила(text: string, selector: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const селекторы = m[1].split(",").map((s) => s.trim());
    if (селекторы.includes(selector)) out.push(m[2]);
  }
  return out;
}

function правило(path: string, selector: string): string {
  const тела = правила(css(path), selector);
  expect(тела.length, `${path}: правило ${selector} не найдено`).toBeGreaterThan(0);
  return тела[0];
}

/** Строки разметки, где класс надет целым словом (не `prof-card--ref`). */
function строкиСКлассом(path: string, cls: string): string[] {
  const слово = new RegExp(`(?<![A-Za-z0-9_-])${cls}(?![A-Za-z0-9_-])`);
  return tsx(path)
    .split("\n")
    .filter((s) => /className=/.test(s) && слово.test(s));
}

/* ───────────────────────────────── 1. обвязка страницы ───────────────── */

/** Корни разделов: файл стилей и селектор корня. */
const КОРНИ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/settings/settings.css", ".settings-section"],
  ["src/features/stats/stats.css", ".stats-page"],
  ["src/features/settings/accounts/accounts.css", ".accounts-page"],
  ["src/features/feed/feed.css", ".lc-feed"],
  ["src/features/notifications/notifications.css", ".lc-journal"],
  ["src/features/updates/updates.css", ".updates"],
];

/**
 * Корни, которые надевают `.lc-page` в разметке. Настроек здесь нет
 * намеренно: `.settings-section` носят восемь файлов, один из них
 * (DistributionTab.tsx) правят соседи, поэтому раздел читает те же токены
 * своим правилом — это проверяет первый тест.
 */
const КОРНИ_В_РАЗМЕТКЕ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/stats/StatsPage.tsx", "stats-page"],
  ["src/features/settings/accounts/AccountsPage.tsx", "accounts-page"],
  ["src/features/feed/FeedPage.tsx", "lc-feed"],
  ["src/features/notifications/NotificationsPage.tsx", "lc-journal"],
  ["src/features/updates/UpdatesPage.tsx", "updates"],
  ["src/features/bot-dialogs/BotDialogsPage.tsx", "bot-dialogs"],
];

describe("Обвязка страницы одна на все разделы", () => {
  /*
   * ⚠ ДИВЕРСИЯ: вернуть `.lc-feed` строку `padding: var(--lc-space-5)` — тест
   * краснеет и называет файл, корень и объявление. Вторая: `.settings-section`
   * снова `padding: var(--lc-space-4) var(--lc-space-5)` — красны оба первых
   * теста. Проверено 06.09.
   */
  it("корни разделов не задают отступ страницы числом — только --lc-page-pad-*", () => {
    const плохие: string[] = [];
    for (const [path, selector] of КОРНИ) {
      const тела = правила(css(path), selector);
      expect(тела.length, `${path}: правило ${selector} не найдено`).toBeGreaterThan(0);
      for (const тело of тела) {
        for (const m of тело.matchAll(/padding[a-z-]*:\s*([^;]+)/g)) {
          if (!m[1].includes("--lc-page-pad-")) плохие.push(`${path} ${selector}: ${m[0].trim()}`);
        }
      }
    }
    expect(плохие, `свой отступ страницы:\n${плохие.join("\n")}`).toEqual([]);
  });

  it("настройки читают те же токены своим правилом", () => {
    expect(правило("src/features/settings/settings.css", ".settings-section")).toMatch(
      /padding:\s*var\(--lc-page-pad-y\)\s+var\(--lc-page-pad-x\)/,
    );
  });

  it(".lc-page объявлена и читает оба токена", () => {
    const тело = правило(БАЗА, ".lc-page");
    expect(тело).toMatch(/padding:\s*var\(--lc-page-pad-y\)\s+var\(--lc-page-pad-x\)/);
    /*
     * У каждого токена две ступени: стол в `:root`, телефон в `@media`.
     * ⚠ «Токен объявлен» — мало: без настольной строки регекс находил
     * телефонную, и на столе обвязка пропадала, а сторож молчал.
     * ДИВЕРСИЯ: убрать `--lc-page-pad-x: var(--lc-space-5)` из `:root` —
     * тест краснеет («стол и телефон»). Проверено 06.09.
     */
    const токены = css(ТОКЕНЫ);
    for (const ось of ["x", "y"]) {
      const ступени = [...токены.matchAll(new RegExp(`--lc-page-pad-${ось}:\\s*var\\(--lc-space-(\\d)\\)`, "g"))].map(
        (m) => Number(m[1]),
      );
      expect(ступени, `--lc-page-pad-${ось}: стол и телефон`).toHaveLength(2);
      expect(ступени[0], `--lc-page-pad-${ось}: стол не шире телефона`).toBeGreaterThan(ступени[1]);
    }
  });

  // ⚠ ДИВЕРСИЯ: снять `lc-page` с корня StatsPage.tsx — тест краснеет (06.09).
  it("корни без своего отступа надевают .lc-page", () => {
    for (const [path, cls] of КОРНИ_В_РАЗМЕТКЕ) {
      const строки = строкиСКлассом(path, cls);
      expect(строки.length, `${path}: корень .${cls} не найден`).toBeGreaterThan(0);
      for (const s of строки) {
        expect(s, `${path}: корень .${cls} без .lc-page`).toMatch(/(?<![A-Za-z0-9_-])lc-page(?![A-Za-z0-9_-])/);
      }
    }
  });
});

/* ───────────────────────────────── 2. карточки ───────────────────────── */

/** Карточки, у которых рецепт целиком в `.lc-card`: своих фона и тени нет. */
const КАРТОЧКИ_НА_КЛАССЕ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/settings/profile/profile.css", ".prof-card"],
  ["src/features/settings/leadbot/leadbot.css", ".lb-card"],
  ["src/features/settings/team/team.css", ".team-roles"],
  ["src/features/chats/components/card/client-card.css", ".card-section"],
];

/**
 * Карточки, чей рецепт повторён в собственном правиле: его держат
 * surfaceLadder.test.ts и cardRecipe.test.ts (они ищут `--lc-surface`, рамку и
 * радиус именно там). Повтор допустим, расхождение — нет: тени быть не должно,
 * а фон — только `--lc-surface`.
 */
const КАРТОЧКИ_ПРИКОЛОЧЕННЫЕ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/stats/stats.css", ".stats-panel"],
  ["src/features/updates/updates.css", ".updates__release"],
];

/** Крупные поверхности, которые в `.lc-card` не переезжают, но тень сдают. */
const БЕЗ_ТЕНИ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/stats/stats.css", ".stat-card"],
  ["src/features/settings/accounts/accounts.css", ".accounts-page__grid"],
];

/** Носители карточек: файл разметки и класс, рядом с которым обязан стоять `lc-card`. */
const НОСИТЕЛИ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/settings/profile/ProfilePage.tsx", "prof-card"],
  ["src/features/settings/profile/AboutAppBlock.tsx", "prof-card"],
  ["src/features/settings/profile/AppearanceBlock.tsx", "prof-card"],
  ["src/features/settings/profile/BrowserAlertsBlock.tsx", "prof-card"],
  ["src/features/settings/profile/ContactAdminForm.tsx", "prof-card"],
  ["src/features/settings/profile/HotkeysBlock.tsx", "prof-card"],
  ["src/features/settings/profile/MyChannelsBlock.tsx", "prof-card"],
  ["src/features/settings/profile/ToastsBlock.tsx", "prof-card"],
  ["src/features/settings/leadbot/LeadbotTab.tsx", "lb-card"],
  ["src/features/settings/team/TeamMembersTab.tsx", "team-roles"],
  ["src/features/chats/components/card/ClientCardPane.tsx", "card-section"],
  ["src/features/stats/components/ManagersTable.tsx", "stats-panel"],
  ["src/features/stats/components/MetricChart.tsx", "stats-panel"],
  ["src/features/stats/components/Heatmap.tsx", "stats-panel"],
  ["src/features/updates/UpdatesPage.tsx", "updates__release"],
];

describe("Карточка — один рецепт .lc-card", () => {
  it(".lc-card: поверхность, рамка, радиус карточки и без тени", () => {
    const тело = правило(БАЗА, ".lc-card");
    expect(тело).toMatch(/background:\s*var\(--lc-surface\)/);
    expect(тело).toMatch(/border:\s*1px solid var\(--lc-border\)/);
    expect(тело).toMatch(/border-radius:\s*var\(--lc-radius-lg\)/);
    expect(тело, "тень на карточке в потоке — признак всплывающего слоя").not.toMatch(/box-shadow/);
  });

  // ⚠ ДИВЕРСИЯ: вернуть `.prof-card` строку `background: var(--lc-bg-1)` —
  // тест краснеет (06.09).
  it("карточки на классе не носят своих фона и тени", () => {
    for (const [path, selector] of КАРТОЧКИ_НА_КЛАССЕ) {
      const тело = правило(path, selector);
      expect(тело, `${selector}: свой фон мимо .lc-card`).not.toMatch(/background/);
      expect(тело, `${selector}: своя тень мимо .lc-card`).not.toMatch(/box-shadow/);
    }
  });

  // ⚠ ДИВЕРСИЯ: вернуть `.stats-panel` строку `box-shadow: var(--lc-shadow-1)`
  // — тест краснеет (06.09).
  it("приколоченные соседними сторожами карточки повторяют рецепт, а не расходятся с ним", () => {
    for (const [path, selector] of КАРТОЧКИ_ПРИКОЛОЧЕННЫЕ) {
      const тело = правило(path, selector);
      const фоны = [...тело.matchAll(/background(?:-color)?:\s*([^;]+)/g)].map((m) => m[1].trim());
      expect(фоны, `${selector}: фон не --lc-surface`).toEqual(["var(--lc-surface)"]);
      expect(тело, `${selector}: тень мимо .lc-card`).not.toMatch(/box-shadow/);
    }
  });

  it("остальные крупные поверхности тень сдали", () => {
    for (const [path, selector] of БЕЗ_ТЕНИ) {
      expect(правило(path, selector), `${selector}: тень осталась`).not.toMatch(/box-shadow/);
    }
  });

  /*
   * ⚠ ДИВЕРСИЯ: снять `lc-card` с `lb-card` в LeadbotTab.tsx — тест краснеет
   * (06.09). Этот же тест при первом прогоне нашёл два носителя, которых в
   * списке аудита не было, — пустые состояния карточки клиента в
   * ClientCardPane.tsx (`card-section client-card__empty-card`).
   */
  it("каждый носитель карточки надевает .lc-card", () => {
    for (const [path, cls] of НОСИТЕЛИ) {
      const строки = строкиСКлассом(path, cls);
      expect(строки.length, `${path}: носитель .${cls} не найден`).toBeGreaterThan(0);
      for (const s of строки) {
        expect(s, `${path}: .${cls} без .lc-card`).toMatch(/(?<![A-Za-z0-9_-])lc-card(?![A-Za-z0-9_-])/);
      }
    }
  });

  it("поле ввода на карточке стоит на своей ступени — и включённое, и выключенное", () => {
    /*
     * Ради этого и снимается исключение профиля и потолка раздачи: в светлой
     * теме поле `--lc-bg-0` на белой карточке давало 1.00, в тёмной выключенное
     * `--lc-bg-2` на `--lc-surface` — тот же 1.00 (оба — `--lc-bg-raise`).
     *
     * ⚠ ДИВЕРСИЯ: выключенному полю вернуть `var(--lc-bg-2)` — тест краснеет
     * (06.09).
     */
    const поле = правило(БАЗА, ".lc-card .mantine-Input-input");
    expect(поле).toMatch(/background:\s*var\(--lc-bg-1\)/);
    /*
     * ⚠ Именно `--lc-surface-hover`, а не «что угодно, кроме трёх»: чёрный
     * список пропускал `--lc-bg-0`, а в светлой теме это #ffffff — сама
     * карточка, тот же 1.00. Довод за ступень «наведение» — у правила в
     * lc-base.css. ДИВЕРСИЯ: выключенному полю дать `var(--lc-bg-0)` — тест
     * краснеет. Проверено 06.09.
     */
    const выключено = правило(БАЗА, ".lc-card .mantine-Input-input:disabled");
    expect(выключено, "выключенное поле на карточке — на ступени --lc-surface-hover").toMatch(
      /background:\s*var\(--lc-surface-hover\)/,
    );
  });
});

/* ───────────────────────────────── 3. таблицы ────────────────────────── */

/** Сырые `<table>` продукта: стили и разметка. */
const СЫРЫЕ_ТАБЛИЦЫ_CSS = [
  "src/features/bot-dialogs/bot-dialogs.css",
  "src/features/notifications/notifications.css",
  "src/features/templates/templates.css",
  "src/features/settings/team/team.css",
  "src/features/stats/stats.css",
];

const СЫРЫЕ_ТАБЛИЦЫ_TSX = [
  "src/features/bot-dialogs/BotDialogsPage.tsx",
  "src/features/notifications/NotificationsPage.tsx",
  "src/features/templates/TemplatesManager.tsx",
  "src/features/settings/team/AuditLogTab.tsx",
  "src/features/settings/team/TeamMembersTab.tsx",
  "src/features/stats/components/ManagersTable.tsx",
];

/** Селектор целится в элемент `th` (не в `.tpl-th--used` и не в `thead`). */
const ЭТО_TH = /(^|[\s>+~])th(?=$|[\s,:.[])/;

describe("Шапка таблицы одна — и у Mantine, и у сырой", () => {
  // ⚠ ДИВЕРСИЯ: дописать в notifications.css `.lc-journal__table th {
  // font-weight: 500 }` — тест краснеет и называет селектор (06.09).
  it("в css сырых таблиц у th нет своего веса", () => {
    const плохие: string[] = [];
    for (const path of СЫРЫЕ_ТАБЛИЦЫ_CSS) {
      for (const m of css(path).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const наTh = m[1].split(",").some((s) => ЭТО_TH.test(s.trim()));
        if (наTh && /font-weight/.test(m[2])) плохие.push(`${path}: ${m[1].trim()}`);
      }
    }
    expect(плохие, `свой вес шапки:\n${плохие.join("\n")}`).toEqual([]);
  });

  // ⚠ ДИВЕРСИЯ: в BotDialogsPage.tsx снова голый `<table>` — тест краснеет (06.09).
  it("каждая сырая таблица надевает .lc-table", () => {
    for (const path of СЫРЫЕ_ТАБЛИЦЫ_TSX) {
      const теги = [...tsx(path).matchAll(/<table\b[^>]*>/g)].map((m) => m[0]);
      expect(теги.length, `${path}: <table> не найден`).toBeGreaterThan(0);
      for (const тег of теги) {
        expect(тег, `${path}: таблица без .lc-table`).toMatch(
          /className=["'`][^"'`]*(?<![A-Za-z0-9_-])lc-table(?![A-Za-z0-9_-])/,
        );
      }
    }
  });

  // ⚠ ДИВЕРСИЯ: `.lc-table th` на `--lc-fw-medium` при `--lc-fw-semibold` у
  // Mantine — тест краснеет (06.09).
  it(".lc-table th повторяет .mantine-Table-th кеглем, весом и цветом", () => {
    const своя = правило(БАЗА, ".lc-table th");
    const mantine = правило(БАЗА, ".mantine-Table-th");
    for (const свойство of ["font-size", "font-weight", "color"]) {
      const взять = (тело: string) => тело.match(new RegExp(`${свойство}:\\s*([^;]+)`))?.[1].trim();
      expect(взять(своя), `${свойство} у .lc-table th не задан`).toBeTruthy();
      expect(взять(своя), `${свойство}: сырая шапка разошлась с Mantine`).toBe(взять(mantine));
    }
  });
});

/* ───────────────────────────────── 4. вход и отказ ───────────────────── */

const ВХОД: ReadonlyArray<readonly [string, string]> = [
  ["src/features/auth/LoginPage.tsx", "--lc-fz-page"],
  ["src/features/auth/InvitePage.tsx", "--lc-fz-page"],
  ["src/features/auth/ForgotPasswordForm.tsx", "--lc-fz-section"],
  ["src/app/ErrorBoundary.tsx", "--lc-fz-page"],
];

describe("Экраны входа и отказа — кегль и ширина из шкалы", () => {
  /*
   * ⚠ ДИВЕРСИЯ: в LoginPage.tsx вернуть `fz={24}` — красны «кегль не числом» и
   * «заголовок на своей ступени»; в ErrorBoundary.tsx вернуть
   * `style={{ maxWidth: 520 }}` — красны «ширина не числом» и та же вторая.
   * Проверено 06.09.
   */
  it("кегль не числом", () => {
    for (const [path] of ВХОД) {
      const код = tsx(path);
      expect(код, `${path}: fz числом мимо шкалы`).not.toMatch(/fz=\{\s*\d/);
      // Тот же обход через `style`: он перебивает `fz`, и шкала снова мимо.
      // ДИВЕРСИЯ: дописать `style={{ fontSize: 24 }}` — тест краснеет (06.09).
      expect(код, `${path}: fontSize числом в style`).not.toMatch(/fontSize:\s*\d/);
    }
  });

  it("ширина не числом", () => {
    for (const [path] of ВХОД) {
      const код = tsx(path);
      expect(код, `${path}: maxWidth числом`).not.toMatch(/maxWidth:\s*\d/);
      expect(код, `${path}: maw числом`).not.toMatch(/maw=\{\s*\d/);
    }
  });

  it("заголовок каждого экрана стоит на своей ступени", () => {
    for (const [path, ступень] of ВХОД) {
      // Каждый `<Title>` файла, а не «хоть один»: у приглашения два экрана и
      // два заголовка, и второй мог уехать со ступени незамеченным.
      // ДИВЕРСИЯ: в InvitePage.tsx перевести ОДИН из двух на `--lc-fz-body` —
      // тест краснеет. Проверено 06.09.
      const заголовки = [...tsx(path).matchAll(/<Title\b[^>]*>/g)].map((m) => m[0]);
      expect(заголовки.length, `${path}: <Title> не найден`).toBeGreaterThan(0);
      for (const тег of заголовки) {
        expect(тег, `${path}: заголовок не на ${ступень}`).toContain(`fz="var(${ступень})"`);
      }
    }
    expect(tsx("src/app/ErrorBoundary.tsx"), "текст отказа без потолка читаемой строки").toContain(
      "--lc-prose-max",
    );
  });
});
