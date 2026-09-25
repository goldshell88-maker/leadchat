// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в DistributionAdaptive0509.test.tsx и surfaceLadder.test.ts: сторож читает
// стиль файлом, потому что в vitest стоит `css: false` и в jsdom стилей нет.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { queryClient } from "@/app/queryClient";
import { LeadbotTab } from "@/features/settings/leadbot/LeadbotTab";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * РАЗДЕЛ «ЛИД-БОТ» ПОСЛЕ ПЕРЕРАБОТКИ 05.09 — свойства раскладки и правдивости.
 *
 * ЧТО БЫЛО. Пять одинаковых серых блоков в одну колонку. Экран отвечал на
 * вопрос «как это настроить» и ни одной строкой — на вопрос, с которым его
 * открывают: «работает ли он сейчас и как хорошо». Состояние лежало бейджем
 * внутри третьего блока (замер: ~1400 px от верха при окне 900), число ведомых
 * диалогов — в четвёртом, исходы — в пятом; человек собирал состояние системы
 * по кускам сам, прокручивая три экрана.
 *
 * Стерегутся семь вещей, и каждая ломается МОЛЧА:
 *
 *   1. Состояние и выключатель стоят ВВЕРХУ, до настроек и журнала. Уедут
 *      обратно вниз — экран снова перестанет отвечать на главный вопрос, и ни
 *      один тест этого не заметит: кнопка на месте, подпись та же.
 *   2. Плитки не выдают отказ журнала за ноль. «Пошло не так: 0» на упавшей
 *      ручке — это ложь ровно в тот час, когда экран открывают из-за поломки.
 *   3. Плитка беды и цвет строки считают ПО ОДНОМУ словарю исходов. Разойдутся
 *      — в шапке ноль, в списке две красных строки, и верить перестанут обоим.
 *   4. Нажатие на плитку включает отбор в журнале. Иначе увидев «7», человек
 *      не имеет пути к этим семи.
 *   5. «Включён, но каналов нет» сказано вслух. Прежний бейдж «Работает» это
 *      состояние скрывал целиком.
 *   6. Колонок одна / две / три по ширине, и базовое правило — сетка, иначе
 *      медиазапрос правит механизм, которого нет (ловушка этого проекта).
 *   7. У журнала и списка каналов СВОЯ прокрутка, а цель нажатия у флажков —
 *      не мельче 24.
 *
 * ⚠ КАЖДЫЙ СТОРОЖ ПРОВЕРЕН ДИВЕРСИЕЙ — ломали ровно то, что он стережёт, и
 * смотрели, что краснеет именно он (12 диверсий, 05.09):
 *    1. переставить шапку состояния под колонки — как было до 05.09;
 *    2. заменить `свод ? свод.тревог : "—"` на `свод?.тревог ?? 0`;
 *    3. в `сводЖурнала` считать `outcome === "unavailable"` вместо `toneOf`;
 *    4. обезвредить `onClick` плитки беды;
 *    5. убрать ветку «включён, но не подключён ни к одному каналу»;
 *    6. и наоборот — показывать её всегда;
 *    7. заменить в базовом `.lb-cols` сетку на `display: flex`, оставив
 *       `grid-template-areas` в медиазапросах мёртвыми (ловушка проекта);
 *    8. убрать `lb-col--log` из разметки, оставив область в CSS;
 *    9. убрать `max-height` у `.lb-journal__scroll`;
 *   10. переименовать обёртку журнала в разметке, НЕ трогая CSS: правило есть,
 *       разметка до него не доехала — сторож обязан краснеть и на это;
 *   11. увести селектор цели нажатия мимо разметки (`-labelX`);
 *   12. набрать показатель кеглем тела.
 */

const CSS_PATH = "src/features/settings/leadbot/leadbot.css";

/** Стиль без комментариев: в них разобрано, ПОЧЕМУ было иначе, и наивный
    поиск краснел бы на самом объяснении. */
function css(): string {
  return (readFileSync(CSS_PATH, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
}

/**
 * Всё, что объявлено селектору, — одной строкой.
 *
 * Селектор ищется как ЦЕЛЫЙ элемент списка, а не подстрокой: стоит свойству
 * переехать в правило с общим списком (`.lb-card__lead, .lb-part__lead {`), и
 * наивный поиск нашёл бы чужое тело.
 */
function rule(scope: string, selector: string): string {
  const bodies: string[] = [];
  for (const m of scope.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].split(",").map((s) => s.trim()).includes(selector)) bodies.push(m[2]);
  }
  expect(bodies.length, `${CSS_PATH}: правило ${selector} не найдено`).toBeGreaterThan(0);
  return bodies.join("\n");
}

/** Стиль без медиазапросов: базовые правила и только они. */
function base(): string {
  let text = css();
  for (;;) {
    const at = text.indexOf("@media");
    if (at === -1) return text;
    let depth = 0;
    let i = text.indexOf("{", at);
    for (; i < text.length; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}" && --depth === 0) break;
    }
    text = text.slice(0, at) + text.slice(i + 1);
  }
}

/** Тело @media-блока целиком — по счётчику скобок, а не по первой скобке. */
function media(query: string, contains: string): string {
  const text = css();
  for (let at = text.indexOf(query); at > -1; at = text.indexOf(query, at + 1)) {
    let depth = 0;
    let i = text.indexOf("{", at);
    const from = i;
    for (; i < text.length; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}" && --depth === 0) break;
    }
    const body = text.slice(from, i);
    if (body.includes(contains)) return body;
  }
  expect.fail(`${CSS_PATH}: нет медиазапроса ${query} с правилом ${contains}`);
}

/** Селекторы правил, в теле которых стоит `property: value`. */
function selectorsWith(scope: string, declaration: string): string[] {
  const экран = declaration.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const found: string[] = [];
  for (const m of scope.matchAll(new RegExp(`([^{}]*)\\{[^{}]*${экран}`, "g"))) {
    for (const s of m[1].split(",")) if (s.trim() && !s.includes("@")) found.push(s.trim());
  }
  return found;
}

const OVERVIEW = {
  connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
  is_ready: true,
  enabled: true,
  mode: "suggest" as const,
  context_messages: 30,
  account_ids: ["acc-1"],
  accounts: [
    { id: "acc-1", title: "Дамир", lead_origin: "В95", busy_with_other_bot: false },
    { id: "acc-2", title: "Служебный", lead_origin: null, busy_with_other_bot: false },
  ],
};

const SILENCE = {
  channels: [{ id: "acc-1", title: "Дамир", bot_attached: true, status: "connected" }],
  in_progress: [
    { conversation_id: "aaaabbbbcccc", status: "in_progress", last_message_at: null },
    { conversation_id: "ddddeeeeffff", status: "in_progress", last_message_at: null },
  ],
  not_taken: [
    {
      conversation_id: "111122223333",
      status: "new",
      last_message_at: null,
      reason: "operator_answered",
      reason_label: "оператор уже отвечал",
    },
  ],
};

/** Четыре исхода: штатный, «не ответил», «отброшен» и собранная заявка. */
function call(id: string, outcome: string, lead_ready = false) {
  return {
    id,
    at: "2026-09-05T10:00:00Z",
    conversation_id: null,
    account_id: null,
    request_id: null,
    question: "Ремонтируете?",
    reply: outcome === "unavailable" ? null : "Да",
    layer: "роутер",
    flag: null,
    confidence: 1,
    needs_operator: false,
    outcome,
    outcome_label: outcome,
    escalation: null,
    lead_ready,
    warnings: [],
    ms: 4,
    error: null,
  };
}

const CALLS = [
  call("1", "sent"),
  call("2", "unavailable"),
  call("3", "low_confidence"),
  call("4", "sent", true),
];

type Ответ = { статус: number; тело: unknown };

function mount({
  overview = OVERVIEW,
  calls = CALLS as unknown[],
  callsStatus = 200,
}: { overview?: unknown; calls?: unknown[]; callsStatus?: number } = {}) {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ["bots:manage"],
    accessToken: "t",
    bootstrapped: true,
  });
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requests.push(url);
      // ⚠ ПОРЯДОК ВЕТОК: «/leadbot/calls» содержит «/leadbot», и общая ветка,
      // стоя первой, съела бы обе частные.
      const { статус, тело }: Ответ = url.includes("/leadbot/calls")
        ? { статус: callsStatus, тело: callsStatus === 200 ? { items: calls } : { error: { code: "server_error" } } }
        : url.includes("/leadbot/silence")
          ? { статус: 200, тело: SILENCE }
          : { статус: 200, тело: overview };
      return new Response(JSON.stringify(тело), {
        status: статус,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });
  return requests;
}

/**
 * Число на плитке с такой подписью.
 *
 * Ищем среди подписей ПЛИТОК, а не по всему экрану: «Ведёт сейчас» стоит и на
 * плитке, и заголовком списка в карточке «Что бот ведёт» — одно и то же по
 * смыслу, и это правильно, но `getByText` находит обе.
 */
function плитка(подпись: string): string {
  const caps = [...document.querySelectorAll(".lb-kpi__cap")];
  const cap = caps.find((c) => c.textContent!.trim() === подпись);
  expect(cap, `плитки «${подпись}» нет: ${caps.map((c) => c.textContent).join(" | ")}`).toBeTruthy();
  return cap!.parentElement!.querySelector(".lb-kpi__num")!.textContent!.trim();
}

describe("Лид-бот: доска состояния вместо стопки блоков", () => {
  beforeEach(() => queryClient.clear());
  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("состояние и выключатель стоят выше настроек и журнала", async () => {
    /*
     * ГЛАВНОЕ СВОЙСТВО ПЕРЕРАБОТКИ, и сломать его можно одной перестановкой
     * разметки: кнопка останется на экране, подпись не изменится, ни один
     * прежний тест не покраснеет — а экран снова перестанет отвечать на
     * вопрос, с которым его открывают.
     */
    mount();
    const кнопка = await screen.findByRole("button", { name: "Выключить лид-бота" });
    const шапка = document.querySelector('[aria-label="Состояние лид-бота"]')!;
    expect(шапка, "шапки состояния нет вовсе").toBeTruthy();
    expect(шапка.contains(кнопка), "выключатель живёт не в шапке состояния").toBe(true);

    const колонки = document.querySelector(".lb-cols")!;
    expect(
      шапка.compareDocumentPosition(колонки) & Node.DOCUMENT_POSITION_FOLLOWING,
      "шапка состояния стоит НИЖЕ колонок с настройками",
    ).toBeTruthy();

    // Слово состояния — крупным числом рядом с кнопкой, а не бейджем в форме.
    expect(шапка.querySelector(".lb-state__word")!.textContent).toBe("Работает");
  });

  it("плитки показывают черту, а не ноль, когда журнал не прочитан", async () => {
    /*
     * ⚠ ПУСТО И «НЕ СМОГЛИ ЗАГРУЗИТЬ» — РАЗНЫЕ ВЕЩИ. Тот же разбор, что у
     * пустого состояния списка (NOTIF-01), но у чисел он опаснее: ноль
     * выглядит как настоящий показатель и не вызывает подозрений.
     */
    mount({ callsStatus: 500 });
    await screen.findByText(/Журнал не прочитан/);
    expect(плитка("Пошло не так"), "отказ журнала выдан за ноль").toBe("—");
    expect(плитка("Заявок собрано"), "отказ журнала выдан за ноль").toBe("—");
    // А то, что журналом не считается, обязано остаться числом: ручка
    // «что бот ведёт» отвечала нормально.
    expect(плитка("Ведёт сейчас")).toBe("2");
  });

  it("плитка беды и цветные строки журнала считают по одному словарю", async () => {
    /*
     * Из четырёх обращений беда в двух: «не ответил» (красное) и «ответ
     * отброшен» (жёлтое). Посчитай шапка по своему условию — и в ней окажется
     * единица при двух крашеных строках ниже.
     */
    mount();
    await screen.findByText("Пошло не так");
    await waitFor(() => expect(плитка("Пошло не так")).toBe("2"));
    expect(плитка("Заявок собрано")).toBe("1");

    const крашеные = document.querySelectorAll(".lb-log__item[data-tone]");
    expect(
      крашеные.length,
      "число на плитке разошлось с числом крашеных строк — словарей исходов стало два",
    ).toBe(2);
  });

  it("нажатие на плитку беды включает отбор в журнале", async () => {
    const requests = mount();
    await screen.findByText("Пошло не так");
    const флажок = screen.getByRole("checkbox", { name: "Только там, где что-то пошло не так" });
    expect(флажок).not.toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: /Пошло не так/ }));

    // Отбор общий: плитка и флажок обязаны показывать одно состояние, иначе
    // экран даёт два разных ответа на один вопрос.
    await waitFor(() => expect(флажок).toBeChecked());
    await waitFor(() =>
      expect(
        requests.some((u) => u.includes("/leadbot/calls") && u.includes("only_trouble=true")),
        "журнал не переспросил сервер с отбором беды",
      ).toBe(true),
    );
  });

  it("включён без единого канала — сказано вслух", async () => {
    /*
     * Состояние, которое прежний бейдж «Работает» скрывал целиком: бот жив,
     * настроен, включён — и не увидит ни одного клиента.
     */
    mount({ overview: { ...OVERVIEW, enabled: true, account_ids: [] } });
    expect(
      await screen.findByText(/не подключён ни к одному каналу/),
      "включённый бот без каналов выглядит работающим",
    ).toBeTruthy();
  });

  it("с каналами тревоги нет — иначе предупреждение перестанут читать", async () => {
    mount();
    await screen.findByText("Пошло не так");
    expect(screen.queryByText(/не подключён ни к одному каналу/)).toBeNull();
  });

  it("колонок одна на телефоне, две от 1200 и три от 1800", () => {
    /*
     * ⚠ ЛОВУШКА ЭТОГО ПРОЕКТА: медиазапрос правит механизм, которого у
     * базового правила нет. В списке каналов «Распределения» узкий экран
     * задавал `flex-direction` строке, ставшей к тому времени сеткой, — правило
     * не делало ничего, и никто этого не видел. Поэтому сначала проверяется,
     * что база — СЕТКА, и только потом её области.
     */
    const базовое = rule(base(), ".lb-cols");
    expect(базовое, "база не сетка — области в медиазапросах мертвы").toMatch(
      /display:\s*grid/,
    );
    expect(базовое, "колонки заданы уже в базе — на телефоне их быть не должно").not.toMatch(
      /grid-template-columns/,
    );

    const двe = media("min-width: 1200px", ".lb-col--control");
    expect(двe).toMatch(/grid-template-columns:\s*minmax\(340px,\s*420px\)/);
    expect(двe, "две колонки без раскладки по областям").toMatch(
      /grid-template-areas:\s*"control watch"\s*"control log"/,
    );

    const три = media("min-width: 1800px", "grid-template-areas");
    expect(три, "на большом мониторе журнал и «не взял» по-прежнему друг под другом").toMatch(
      /grid-template-areas:\s*"control watch log"/,
    );
  });

  it("колонки из медиазапросов есть в разметке", async () => {
    // Правило мимо разметки выглядит работающим: в CSS оно есть, сторож выше
    // зелёный, а на экране по-прежнему одна колонка.
    mount();
    await screen.findByText("Пошло не так");
    for (const имя of ["lb-col--control", "lb-col--watch", "lb-col--log"]) {
      expect(document.querySelector(`.${имя}`), `в разметке нет .${имя}`).toBeTruthy();
    }
  });

  it("у журнала и списка каналов своя прокрутка, а не общая простыня", async () => {
    mount();
    await screen.findByText("Пошло не так");

    const журнал = rule(base(), ".lb-journal__scroll");
    expect(журнал, "журнал прокручивается страницей").toMatch(/max-height:/);
    expect(журнал).toMatch(/overflow-y:\s*auto/);

    // И НАСТОЯЩИЙ список лежит внутри такого элемента: правило, до которого
    // разметка не доехала, чинит только тест.
    const список = document.querySelector(".lb-log")!;
    expect(список, "журнал не отрисован — проверять нечего").toBeTruthy();
    expect(
      список.parentElement!.matches(".lb-journal__scroll"),
      "лента журнала лежит вне прокручиваемой области",
    ).toBe(true);

    const каналы = rule(base(), ".lb-channels");
    expect(каналы, "два десятка флажков снова выталкивают форму за край").toMatch(
      /max-height:/,
    );
    expect(каналы).toMatch(/overflow-y:\s*auto/);
    expect(document.querySelector(".lb-channels")).toBeTruthy();
  });

  it("цель нажатия у флажков не мельче 24 пикселей", async () => {
    /*
     * Метка Mantine — 20 px, и целиться в неё пальцем нечем. Тот же размер и
     * тот же довод, что у тумблера «Без ограничения» в «Распределении».
     *
     * ⚠ СТОРОЖ ИДЁТ ОТ РАЗМЕТКИ К СТИЛЮ, А НЕ НАОБОРОТ: он требует, чтобы
     * НАСТОЯЩАЯ метка попадала под селектор с высотой, иначе правило можно
     * увести мимо разметки и оставить тест зелёным.
     */
    mount();
    await screen.findByRole("checkbox", { name: /Дамир · В95/ });
    const селекторы = selectorsWith(css(), "min-height: 24px");
    expect(селекторы.length, "высоты цели нажатия нет вовсе").toBeGreaterThan(0);

    for (const [что, где] of [
      ["канал", ".lb-channels__item .mantine-Checkbox-label"],
      ["отбор беды", ".lb-filters__flag .mantine-Checkbox-label"],
    ] as const) {
      const метка = document.querySelector(где);
      expect(метка, `метки «${что}» нет в разметке (${где})`).toBeTruthy();
      expect(
        селекторы.some((s) => метка!.matches(s)),
        `метка «${что}» не попадает ни под один селектор высоты: ${селекторы.join(" | ")}`,
      ).toBe(true);
    }
  });

  it("число на плитке крупнее подписи", () => {
    /*
     * «Числа крупно, подписи мелко» — иначе плитка перестаёт быть показателем
     * и становится ещё одной строчкой текста. На этом экране до 05.09 не было
     * ни одного числа крупнее тела.
     */
    const число = rule(base(), ".lb-kpi__num");
    const подпись = rule(base(), ".lb-kpi__cap");
    expect(число, "показатель набран не ступенью показателя").toMatch(
      /font-size:\s*var\(--lc-fz-metric\)/,
    );
    expect(подпись, "подпись плитки набрана крупнее подписи").toMatch(
      /font-size:\s*var\(--lc-fz-caption\)/,
    );
  });
});
