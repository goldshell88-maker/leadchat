import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * КАРТОЧКА КАНАЛА ПОМЕЧАЕТ ЖЁЛТЫМ И КРАСНЫМ ТОЛЬКО ТО, ЧТО ТРЕБУЕТ ДЕЙСТВИЯ.
 *
 * ЖАЛОБА ВЛАДЕЛЬЦА ОТ 12 АВГУСТА, ради которой всё переписано: «⚠️ Токен
 * истекает через 23 ч» горело жёлтым на всех каналах круглосуточно. Токен
 * Авито живёт сутки — значит остаток 23 часа из 24 это НОРМАЛЬНОЕ состояние
 * канала, а не новость. Люди читали строку как аварию и жали «Обновить токен»
 * руками каждый день, хотя перевыпуск идёт сам за два часа до срока. Цена
 * такого индикатора не в лишних нажатиях: он приучает не смотреть и туда, где
 * однажды загорится настоящая поломка.
 *
 * Вторая половина той же беды — тишина: «событий нет 16 ч» жёлтым на канале с
 * пятью обращениями за неделю, при исправной подписке. Мерилось время на
 * стене, а не обычный ритм канала.
 *
 * ЧТО ИМЕННО ТУТ ПРОВЕРЯЕТСЯ. Не пороги — их считает сервер
 * (`app/services/channel_health.py`, tests/unit по нему свои), — а то, что
 * экран РИСУЕТ ПРИСЛАННОЕ и не добавляет собственной тревоги поверх. Ровно это
 * и было сломано: сервер про 23 часа ничего плохого не говорил, жёлтый цвет
 * фронт брал из своего порога в 48 часов.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - вернули прежний расчёт по `token_expires_at` (жёлтое при остатке меньше
 *    48 часов) — падает «остаток 23 часа — это норма, а не тревога» на цвете и
 *    на значке, и «старый сервер без состояния — строка всё равно нейтральная»;
 *  - дали состоянию `ok` жёлтый цвет в таблице `LOOK` — падает та же пара;
 *  - показали значок у состояний `ok`/`quiet` — падают «остаток 23 часа» и
 *    «тихий канал не помечен тревогой»;
 *  - вернули кнопку «Обновить токен» под строку токена безусловно — падает
 *    «в норме кнопки нет, она в меню «…»»;
 *  - убрали кнопку из ветки `action` — падают «внимание» и «авария»;
 *  - сняли проверку `canManage` — падает «руководителю кнопку не показывают»;
 *  - вернули расчёт тишины по `last_event_at` — падает «тихий канал не помечен
 *    тревогой»;
 *  - убрали ранний выход по `is_service` — падает «служебная заглушка не
 *    выдаёт себя за канал».
 */

const MIN_MS = 60_000;
const HOUR_MS = 60 * MIN_MS;
const DAY_MS = 24 * HOUR_MS;

const ADMIN: Permission[] = ["accounts:read", "accounts:manage"];
/** Руководитель: состояние канала видит, управлять им не может (11 §4.1). */
const HEAD: Permission[] = ["accounts:read"];

/**
 * ШТАТНЫЙ КАНАЛ ГЛАЗАМИ СЕРВЕРА, И ЭТО ТОТ САМЫЙ СЛУЧАЙ ЖАЛОБЫ.
 *
 * До истечения токена 23 часа из 24 — состояние `ok`: автообновление ещё даже
 * не подходило (оно за два часа до срока). Строки `message` здесь не выдуманы,
 * а собраны ровно так, как их собирает `channel_health.token_health` и
 * `channel_health.webhook_health`.
 */
function base() {
  const now = Date.now();
  return {
    id: "acc-1",
    title: "! Парт - 7 / Ист - В43 МНЧ !",
    avito_user_id: 111222333,
    status: "active",
    own_keys: false,
    is_service: false,
    token_expires_at: new Date(now + 23 * HOUR_MS).toISOString(),
    created_at: new Date(now - 8 * DAY_MS).toISOString(),
    token: {
      state: "ok",
      message: "Токен активен. Обновится автоматически 13.08 в 16:07",
      last_refresh_at: new Date(now - HOUR_MS).toISOString(),
      action: null,
    },
    webhook: {
      status: "ok",
      url: "https://leadchat.example/api/v1/webhooks/avito/acc-1",
      last_event_at: new Date(now - MIN_MS).toISOString(),
      state: "ok",
      message: "События приходят, последнее в 19:58. Сверка подписки 16:54, расхождений нет.",
      action: null,
    },
    backfill: { status: "idle" },
    operators: { count: 0, preview: [] },
    stats: null,
  };
}

function mount(overrides: Record<string, unknown> = {}, permissions: Permission[] = ADMIN) {
  const account = { ...base(), ...overrides };
  resetSessionStore({
    user: fakeUser,
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/avito-accounts")) {
        return jsonResponse(200, { items: [account], page: { limit: 50, offset: 0, total: 1 } });
      }
      return jsonResponse(200, {});
    }),
  );
  return renderWithProviders(<AccountsPage />);
}

/**
 * Сама строка состояния по любому узлу внутри неё.
 *
 * ⚠ ЗАЧЕМ ПОДЪЁМ К РОДИТЕЛЮ. До 14.08 текст лежал в строке голым текстовым узлом,
 * и `findByText` возвращал саму строку — значок и `role` спрашивали прямо у неё.
 * Потом текст завернули в `.account-card__health-text` (иначе он переносился ЦЕЛИКОМ
 * и оставлял значок на строке одного), и `findByText` стал возвращать этот span:
 * внутри него ни значка, ни роли. Тесты покраснели по делу — сломался прибор, а не
 * поведение.
 */
function healthLine(node: HTMLElement): HTMLElement {
  return (node.closest(".account-card__health-line") as HTMLElement | null) ?? node;
}

/** Значок внутри строки — единственное, чем строка кричит на экране. */
function hasIcon(line: HTMLElement): boolean {
  return healthLine(line).querySelector("svg") !== null;
}

describe("Карточка канала — предупреждения", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // --- токен -----------------------------------------------------------------

  it("остаток 23 часа — это норма, а не тревога: нейтрально, без ⚠️", async () => {
    // КРИТЕРИЙ ПРИЁМКИ. Ровно то состояние, на которое жаловался владелец:
    // сутки минус час до истечения — и раньше здесь горело жёлтое «⚠️ Токен
    // истекает через 23 ч».
    mount({});

    const line = await screen.findByText(/Токен активен/);
    expect(line).toHaveStyle({ color: "var(--lc-text-2)" });
    expect(hasIcon(line)).toBe(false);
    // Ни живой области, ни тревожной роли: сообщать не о чем.
    expect(healthLine(line)).not.toHaveAttribute("role");
    expect(screen.queryByText(/истекает через/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("в норме кнопки «Обновить токен» на виду нет — она в меню «…»", async () => {
    mount({});

    await screen.findByText(/Токен активен/);
    // Именно эта кнопка и собирала ежедневные нажатия: человек читал штатную
    // строку как аварию, а под ней стояло приглашение «почини».
    expect(screen.queryByRole("button", { name: "Обновить токен" })).toBeNull();
    expect(
      screen.getByRole("button", { name: /Обслуживание канала/ }),
    ).toBeInTheDocument();
  });

  it("норма говорит, что автоматика работает: когда обновилось в последний раз", async () => {
    mount({});

    // Ответ на вопрос, ради которого и жали кнопку: «а оно вообще само-то
    // обновляется?». Без него «обновится автоматически» — обещание без истории.
    expect(await screen.findByText(/Последний раз обновился сам \d{2}\.\d{2} в \d{2}:\d{2}/)).toBeInTheDocument();
  });

  it("внимание: жёлтая строка со знаком, причиной и кнопкой", async () => {
    mount({
      token: {
        state: "warning",
        message:
          "Последнее автообновление не сработало: Авито ответил 502. Токен действует до 13.08 в 16:07; " +
          "если следующая попытка тоже не пройдёт, ответы перестанут уходить.",
        last_refresh_at: null,
        action: "refresh_token",
      },
    });

    const line = await screen.findByText(/Последнее автообновление не сработало/);
    expect(line).toHaveStyle({ color: "var(--lc-warning-text)" });
    expect(hasIcon(line)).toBe(true);
    expect(healthLine(line)).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Обновить токен" })).toBeInTheDocument();
  });

  it("авария: красная строка называет последствие и зовёт чинить", async () => {
    mount({
      token: {
        state: "critical",
        message: "Токен истёк 13.08 в 16:07 — ответы клиентам не уходят.",
        last_refresh_at: null,
        action: "refresh_token",
      },
    });

    const line = await screen.findByRole("alert");
    expect(line).toHaveTextContent(/Токен истёк/);
    // Последствие, а не диагноз: «истёк» без «ответы не уходят» человек
    // прочитает как техническую мелочь.
    expect(line).toHaveTextContent(/ответы клиентам не уходят/);
    expect(line).toHaveStyle({ color: "var(--lc-danger-text)" });
    expect(screen.getByRole("button", { name: "Обновить токен" })).toBeInTheDocument();
  });

  it("руководителю кнопку не показывают, а говорят, кто чинит", async () => {
    mount(
      {
        token: {
          state: "critical",
          message: "Токен истёк 13.08 в 16:07 — ответы клиентам не уходят.",
          last_refresh_at: null,
          action: "refresh_token",
        },
      },
      HEAD,
    );

    /*
     * Строку он видит: канал молчит, и знать об этом ему нужно. А кнопки нет — сервер
     * откажет по праву `accounts:manage`, и без пояснения руководитель искал бы кнопку.
     *
     * ⚠ ПОЯСНЕНИЕ ПЕРЕПИСАНО 13.08, И ЭТО НЕ КОСМЕТИКА. Стояло «Это делает
     * администратор.» — и держалось на хвосте серверной фразы «Нажмите «Обновить
     * токен»»: «это» отсылало к «нажмите». Хвосты сняли (кнопка с той же подписью
     * стоит вплотную справа, и дублировать её в тексте незачем), после чего
     * местоимению стало не к чему отнестись.
     *
     * Теперь строка собирается из подписи того же действия и называет ОБА
     * недостающих: что сделать и кто это делает. Проверяется она целиком, вместе с
     * названием действия, — иначе подмена «Обновить токен» на «Обновить подписку»
     * прошла бы молча.
     */
    expect(await screen.findByText(/Токен истёк/)).toBeInTheDocument();
    expect(screen.getByText(/^Токен истёк.*не уходят\.$/), "хвост «Нажмите…» вернулся").toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Обновить токен" })).toBeNull();
    expect(screen.getByText("Обновить токен может администратор.")).toBeInTheDocument();
  });

  it("старый сервер без состояния — строка всё равно нейтральная", async () => {
    // Выкатка идёт порознь, и сборка сервера без `token` знает про токен одну
    // дату. Соблазн «посчитать самим, раз даты хватает» — это и есть отменённое
    // правило «меньше двух суток — жёлтым»: с суточным токеном оно горит всегда.
    mount({ token: undefined });

    const line = await screen.findByText(/Токен активен, до \d{2}\.\d{2}\.\d{4}$/);
    expect(line).toHaveStyle({ color: "var(--lc-text-2)" });
    expect(hasIcon(line)).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  // --- приём обращений -------------------------------------------------------

  it("тихий канал не помечен тревогой, и сверка подписки видна", async () => {
    // Канал с пятью обращениями за неделю: шестнадцать часов тишины для него
    // обычный день. Раньше здесь горело жёлтое «⚠️ событий нет 16 ч» с текстом
    // про подписку, которая могла достаться другой системе, — при исправной
    // интеграции.
    mount({
      webhook: {
        status: "ok",
        url: "https://leadchat.example/api/v1/webhooks/avito/acc-1",
        last_event_at: new Date(Date.now() - 16 * HOUR_MS).toISOString(),
        state: "quiet",
        message:
          "Событий нет 16 ч, для этого канала это в пределах нормы. " +
          "Сверка подписки 16:54, расхождений нет.",
        action: null,
      },
    });

    const line = await screen.findByText(/Событий нет 16 ч/);
    expect(line).toHaveStyle({ color: "var(--lc-text-2)" });
    expect(hasIcon(line)).toBe(false);
    // Время сверки — на карточке, а не в журналах: вопрос «смотрел ли
    // кто-нибудь на подписку вообще» задавали именно потому, что ответа не было.
    expect(line).toHaveTextContent(/Сверка подписки 16:54, расхождений нет/);
    expect(screen.queryByRole("button", { name: "Обновить подписку" })).toBeNull();
  });

  it("чужая подписка — красное, с последствием и кнопкой", async () => {
    // Авито держит на аккаунт РОВНО ОДНУ подписку: увёл её Jivo — наша молча
    // исчезла, и обращения к нам не приходят вовсе. Это факт, а не догадка по
    // тишине: он получен сверкой у Авито.
    mount({
      webhook: {
        status: "ok",
        url: "https://jivo.example/webhook",
        last_event_at: new Date(Date.now() - 3 * HOUR_MS).toISOString(),
        state: "critical",
        message:
          "Подписку на события забрала другая система: обращения от клиентов к нам не приходят " +
          "вовсе. Сверка подписки 16:54.",
        action: "rewebhook",
      },
    });

    const line = await screen.findByRole("alert");
    expect(line).toHaveTextContent(/забрала другая система/);
    expect(line).toHaveTextContent(/не приходят вовсе/);
    expect(line).toHaveStyle({ color: "var(--lc-danger-text)" });
    expect(screen.getByRole("button", { name: "Обновить подписку" })).toBeInTheDocument();
  });

  it("на экране не осталось слова «Webhook»", async () => {
    // Строку читает владелец бизнеса, а не интегратор, и сервер присылает
    // готовую русскую фразу. Латинская подпись перед ней требовала перевода.
    mount({});

    await screen.findByText(/События приходят/);
    expect(screen.queryByText(/Webhook/i)).toBeNull();
  });

  // --- служебная заглушка ----------------------------------------------------

  it("служебная заглушка не выдаёт себя за канал", async () => {
    mount({
      title: "SMOKE-ACCOUNT",
      is_service: true,
      avito_user_id: 1,
      // Ровно то, что лежало в живой системе: срок на десять лет вперёд и след
      // неизбежно провалившейся попытки подписаться строкой вместо токена.
      token_expires_at: new Date(Date.now() + 3650 * DAY_MS).toISOString(),
      token: {
        state: "ok",
        message: "Служебная заглушка регрессионного набора — токена Авито у неё нет.",
        last_refresh_at: null,
        action: null,
      },
      webhook: {
        status: "failed",
        url: null,
        last_event_at: null,
        state: "ok",
        message: "Служебная заглушка регрессионного набора — события через неё не идут.",
        action: null,
      },
    });

    const card = await screen.findByLabelText(/Служебная заглушка SMOKE-ACCOUNT/);
    expect(within(card).getByText(/Служебная заглушка проверочного набора/)).toBeInTheDocument();
    expect(screen.getByLabelText("Статус: служебная заглушка")).toBeInTheDocument();
    // Ни одной кнопки, которой сервер обязан отказать.
    expect(screen.queryByRole("button", { name: "Обновить токен" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Обслуживание канала/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Отключить/ })).toBeNull();
  });

  it("настоящий канал остаётся полноценной карточкой", async () => {
    // Охранник от чрезмерного запрета: «спрятать всё у всех» выглядело бы такой
    // же зелёной правкой, как «спрятать всё у заглушки».
    mount({});

    expect(await screen.findByText(/Токен активен/)).toBeInTheDocument();
    expect(screen.getByText(/События приходят/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Обслуживание канала/ })).toBeInTheDocument();
  });

  /*
   * ДВЕ ЧАСТИ ФРАЗЫ: ЗАГОЛОВОК ЯНТАРНЫЙ, ДЕТАЛЬ СЕРАЯ (разбор интерфейса 13.08).
   *
   * Жалоба: янтарный абзац выходит на пять-шесть строк в колонке карточки — 190
   * знаков у затянувшейся тишины. Делит СЕРВЕР и присылает обе части готовыми;
   * разбирать `message` на фронте запрещено прямо (шапка ChannelHealth.tsx).
   *
   * ⚠ ПОЧЕМУ ЭТИ ТРИ ТЕСТА ОБЯЗАТЕЛЬНЫ, ХОТЯ ПРАВКА НЕ УРОНИЛА НИ ОДНОГО СТАРОГО.
   * Все заготовки в этом файле — захардкоженные объекты в моке `fetch`: серверный
   * текст до них физически не доезжает. Значит правка сервера здесь не проверяется
   * ничем, и обратный откат — тоже: убери `headline` из разметки, и весь файл
   * останется зелёным, а на экран вернётся абзац на пять строк.
   */
  it("сервер прислал две части — заголовок янтарный, деталь серая и отдельной строкой", async () => {
    mount({
      webhook: {
        status: "ok",
        state: "warning",
        message:
          "Событий нет 16 ч — заметно дольше обычного. Обычная пауза на этом канале — 32 мин. " +
          "Сверка подписки 16:54, расхождений нет. Подписка на месте, значит дело не в ней — " +
          "проверьте приём сообщений.",
        headline: "Событий нет заметно дольше обычного для этого канала.",
        detail:
          "Без событий 16 ч. Обычная пауза на этом канале — 32 мин. Сверка подписки 16:54, " +
          "расхождений нет. Подписка на месте, значит дело не в ней — проверьте приём сообщений.",
        action: null,
      },
    });

    const строка = await screen.findByText("Событий нет заметно дольше обычного для этого канала.");
    expect(строка).toHaveStyle({ color: "var(--lc-warning-text)" });
    expect(hasIcon(строка)).toBe(true);

    // Деталь — ОТДЕЛЬНЫЙ узел, а не хвост той же строки: иначе абзац не сократился,
    // а только переехал.
    const деталь = screen.getByText(/^Без событий 16 ч\./);
    expect(деталь).not.toBe(строка);
    expect(деталь).toHaveStyle({ color: "var(--lc-text-3)" });

    // ⚠ И ГЛАВНОЕ: длинной фразы на экране больше нет вовсе.
    expect(screen.queryByText(/Событий нет 16 ч — заметно дольше обычного/)).toBeNull();
  });

  it("⚠ СТАРЫЙ СЕРВЕР БЕЗ ЗАГОЛОВКА — ФРАЗА ЦЕЛИКОМ, И НИЧЕГО НЕ ПОТЕРЯНО", async () => {
    /*
     * Сервер и фронт выкатываются порознь. В окно между выкатками `headline` не
     * приходит вовсе — и строка обязана остаться прежней и ПОЛНОЙ. Ровно ради этого
     * `message` на сервере не укорачивали: режь его — и здесь была бы половина фразы
     * без всякого способа узнать вторую.
     */
    mount({
      token: {
        state: "critical",
        message: "Токен истёк 13.08 в 16:07 — ответы клиентам не уходят.",
        last_refresh_at: null,
        action: "refresh_token",
      },
    });

    expect(
      await screen.findByText("Токен истёк 13.08 в 16:07 — ответы клиентам не уходят."),
    ).toBeInTheDocument();
    expect(document.querySelector(".account-card__health-detail")).toBeNull();
  });

  it("деталь пустая — второй строки нет, а не пустая полоса", async () => {
    // У коротких состояний делить нечего, и сервер шлёт пустую строку. Пустой узел
    // съел бы зазор колонки и читался бы как обрезанный текст.
    mount({
      token: {
        state: "ok",
        message: "Служебная заглушка регрессионного набора — токена Авито у неё нет.",
        headline: "Служебная заглушка регрессионного набора — токена Авито у неё нет.",
        detail: "",
        last_refresh_at: null,
        action: null,
      },
    });

    await screen.findByText(/Служебная заглушка/);
    expect(document.querySelector(".account-card__health-detail")).toBeNull();
  });
});
