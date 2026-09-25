/**
 * ЦИФРЫ СТАТИСТИКИ, ПРАВКИ 8 СЕНТЯБРЯ (пункт 2 порядка работ по двум аудитам).
 *
 * Общее у всех четырёх находок одно: числа были ВЕРНЫ, а читались как ошибка
 * счёта. Поэтому и сторожа здесь не про арифметику, а про то, отличает ли
 * человек одно число от другого и есть ли у него выход из тупика.
 *
 *   1. ПРОЦЕНТ ВНЕ ШКАЛЫ ЗАМЕНЯЕТСЯ ОСНОВАНИЕМ. «+190 775 %» — не сведение, а
 *      шум: прошлый период попал во время, когда системы не было.
 *   2. ДВА «ОТВЕЧЕНО» НАЗЫВАЮТСЯ РАЗНО. Карточка считает любой ответ оператора,
 *      колонка — только тот, у кого известен автор.
 *   3. ОГОВОРКА ПРО ИТОГ СТОИТ У ОБЕИХ КОЛОНОК, где сумма законно больше итога.
 *   4. У ОТКАЗА ПО СОРТИРОВКЕ ЕСТЬ РАБОЧИЙ ВЫХОД, а не кнопка «Повторить»,
 *      которая заведомо вернёт тот же отказ.
 *
 * Каждый проверен диверсией — сломан ровно тот код, который он стережёт.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]);
// тот же приём, что в labelsFit.test.ts: сторож читает исходник соседнего слоя.
import { readFileSync } from "node:fs";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { StatCard } from "@/features/stats/components/StatCard";
import { SummaryCards } from "@/features/stats/components/SummaryCards";
import { ManagersTable } from "@/features/stats/components/ManagersTable";
import { MetricChart, type MetricChartProps } from "@/features/stats/components/MetricChart";
import { TablePage } from "@/features/table/TablePage";
import type { ManagersResponse } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/* ───────────────────────── 1. процент вне шкалы ─────────────────────────── */

describe("Процент вне шкалы уступает место основанию (M-02)", () => {
  it("при +190 775 % показывает прошлое значение, а не процент", () => {
    renderWithProviders(
      <StatCard
        metricId="conversations_new"
        title="Новые диалоги"
        value="15 278"
        deltaPct={190_775}
        prevLabel="8"
      />,
    );
    expect(screen.getByText("было 8"), "основание не показано").toBeTruthy();
    /*
     * Проверяем отсутствие ЛЮБОГО процента, а не конкретной строки: формат
     * числа зависит от локали, и точное «190 775 %» разошлось бы с продуктом
     * от одной правки форматирования.
     */
    expect(document.body.textContent).not.toMatch(/%/);
  });

  it("обычную дельту не трогает", () => {
    renderWithProviders(
      <StatCard
        metricId="conversations_new"
        title="Новые диалоги"
        value="312"
        deltaPct={11.4}
        prevLabel="280"
      />,
    );
    expect(document.body.textContent).toMatch(/%/);
    expect(screen.queryByText("было 280"), "основание вытеснило живую дельту").toBeNull();
  });

  it("процентные пункты не режет — они и не могут выйти за шкалу", () => {
    /*
     * ⚠ ЭТОТ СТОРОЖ ПРО ГРАНИЦУ ПРИЁМА, А НЕ ПРО ЧИСЛО. Дельта процентной
     * метрики считается в пунктах и по построению не выходит за сотню; если
     * потолок однажды напишут без проверки единицы, он начнёт срабатывать там,
     * где повода нет, — и «доля закрытых ботом» потеряет свою дельту.
     */
    renderWithProviders(
      <StatCard
        metricId="bot_closed"
        title="Закрыто ботом"
        value="42,0 %"
        deltaPct={1500}
        deltaUnit="пп"
        prevLabel="1"
      />,
    );
    expect(screen.queryByText("было 1"), "пункты обрезаны потолком процентов").toBeNull();
  });

  it("сводка ПОДКЛЮЧЕНА к потолку: на настоящих карточках он срабатывает", () => {
    /*
     * ⚠ БЕЗ ЭТОГО СТОРОЖА ВСЕ ОСТАЛЬНЫЕ ЗЕЛЕНЕЮТ ВПУСТУЮ. Они рисуют
     * `StatCard` напрямую и передают `prevLabel` руками. Убери `prevLabel` из
     * `SummaryCards` — потолок в бою не сработает ни разу, а прогон останется
     * зелёным: класс «написано, но не подключено», от которого этот проект уже
     * страдал (см. историю уведомлений 07.09).
     */
    renderWithProviders(
      <SummaryCards
        summary={
          {
            period: { date_from: "2026-01-01", date_to: "2026-09-08", tz: "Europe/Moscow" },
            prev_period: { date_from: "2025-04-25", date_to: "2025-12-31" },
            refreshed_at: "2026-09-08T06:05:15Z",
            period_live: false,
            work_hours: { start_hour: 9, end_hour: 21 },
            cards: {
              conversations_new: { value: 33_403, prev: 17, delta_pct: 196_388.2 },
            },
          } as never
        }
      />,
    );
    expect(
      screen.getByText("было 17"),
      "на настоящей сводке потолок не сработал — `prevLabel` до карточки не доходит",
    ).toBeTruthy();
  });

  it("без прошлого значения оставляет процент — прятать всё нельзя", () => {
    /*
     * Прошлого значения может не быть (метрика его не отдаёт). Показать
     * «было —» бессмысленно, а спрятать дельту целиком значит отнять у
     * человека единственный признак направления.
     */
    renderWithProviders(
      <StatCard metricId="conversations_new" title="Новые диалоги" value="15 278" deltaPct={190_775} />,
    );
    expect(document.body.textContent).toMatch(/%/);
  });
});

/* ─────────────────── 2 и 3. подписи колонок таблицы людей ───────────────── */

const МЕНЕДЖЕРЫ: ManagersResponse = {
  period: { date_from: "2026-08-05", date_to: "2026-09-04", tz: "Europe/Moscow" },
  refreshed_at: "2026-09-04T11:05:12Z",
  rows: [
    {
      manager_id: "m-1",
      full_name: "Анна Смирнова",
      is_active: true,
      taken: 34,
      answered: 31,
      closed: 28,
      frt_avg_sec: 210,
      frt_median_sec: 74,
      frt_median_biz_sec: 71,
      messages_sent: 412,
    },
  ],
  totals: {
    taken: 120,
    answered: 260,
    closed: 290,
    frt_median_sec: 95,
    frt_median_biz_sec: 88,
    messages_sent: 2040,
  },
};

/** Таблица с обязательной обвязкой — сторожам интересны только подписи. */
function ТаблицаЛюдей() {
  return (
    <ManagersTable
      data={МЕНЕДЖЕРЫ}
      isPending={false}
      isError={false}
      onRetry={() => {}}
      sort="taken"
      order="desc"
      onSortChange={() => {}}
      selectedManagerIds={[]}
      onSelectManager={() => {}}
      onClearManagers={() => {}}
      onOpenChats={() => {}}
    />
  );
}

describe("Подписи колонок отчёта по людям (H-02, H-03)", () => {
  /*
   * ⚠ ПОДСКАЗКУ ЧИТАЕМ НАВЕДЕНИЕМ, А НЕ ИЗ ИСХОДНИКА. Соблазн взять строку
   * грепом по файлу велик и дёшев, но такой сторож зеленел бы и тогда, когда
   * подсказка написана, а к заголовку не привязана, — а это ровно тот класс
   * дефекта, который в этом проекте уже случался («написано, но не
   * подключено»). Наведение доказывает, что человек её увидит.
   */
  const подсказка = async (заголовок: string): Promise<string> => {
    const th = screen
      .getAllByRole("columnheader")
      .find((el) => (el.textContent ?? "").includes(заголовок));
    expect(th, `колонки «${заголовок}» нет`).toBeTruthy();
    const цель = th!.querySelector("button, span, div") ?? th!;
    await userEvent.hover(цель);
    /*
     * ⚠ ЖДЁМ РОВНО ОДНУ ВСПЛЫВАШКУ, И ЭТО НЕ ПЕДАНТИЗМ. Первая проверка
     * читала `findByRole("tooltip")` не убрав курсор с прошлой колонки:
     * подсказка «Принято» оставалась на экране, и сторож читал ЕЁ ЖЕ вторым
     * разом. Диверсия (снять оговорку у «Закрыто») прошла незамеченной —
     * сторож был зелёным по неверной причине.
     */
    await waitFor(() => {
      expect(document.querySelectorAll('[role="tooltip"]').length).toBe(1);
    });
    const текст = document.querySelector('[role="tooltip"]')?.textContent ?? "";
    await userEvent.unhover(цель);
    await waitFor(() => {
      expect(document.querySelectorAll('[role="tooltip"]').length).toBe(0);
    });
    return текст;
  };

  beforeEach(() => {
    queryClient.clear();
  });

  it("колонка называется «Ответил первым», а не «Отвечено»", () => {
    renderWithProviders(<ТаблицаЛюдей />);
    const заголовки = screen.getAllByRole("columnheader").map((el) => el.textContent ?? "");
    expect(
      заголовки.some((t) => t.includes("Ответил первым")),
      "колонка не переименована — на экране снова два «Отвечено» с разными числами",
    ).toBe(true);
  });

  it("обе колонки с законным расхождением объясняют итог", async () => {
    renderWithProviders(<ТаблицаЛюдей />);
    for (const колонка of ["Принято", "Закрыто"]) {
      expect(
        await подсказка(колонка),
        `у «${колонка}» нет оговорки про ИТОГО — сумма столбца снова читается как ошибка счёта`,
      ).toMatch(/ИТОГО/);
    }
  });

  it("«Ответил первым» объясняет, чем отличается от карточки наверху", async () => {
    /*
     * ⚠ ПРОВЕРЯЕТСЯ ПОДСКАЗКА, А НЕ ТОЛЬКО ИМЯ. Переименование само по себе
     * лишь разводит два слова; вопрос «почему числа разные» оно не снимает.
     * Ответ на него живёт в подсказке, и без сторожа её вправе стереть первой
     * же правкой — имя останется, объяснение исчезнет.
     */
    renderWithProviders(<ТаблицаЛюдей />);
    const текст = await подсказка("Ответил первым");
    expect(текст, "подсказка не называет источник расхождения").toMatch(/Авито/);
    expect(текст, "подсказка не упоминает карточку, с которой её сравнивают").toMatch(/Отвечено/);
  });
});

/* ──────────── 3б. экран и выгрузка называют счётчики одинаково ─────────── */

describe("Подпись счётчика на экране и в выгрузке — одна (H-03)", () => {
  /*
   * ⚠ ЭТОТ СТОРОЖ РОДИЛСЯ ИЗ ПРОПУЩЕННОЙ ПОЛОВИНЫ ПРАВКИ. Колонку
   * переименовали на экране и забыли в выгрузке — и в одном файле .xlsx снова
   * оказались лист «Сводка» со строкой «Отвечено оператором» (29 757) и лист
   * «Менеджеры» со столбцом «Отвечено» (10 006). То есть исправленное
   * противоречие переехало с экрана в файл, который уходит руководителю.
   *
   * ⚠ СВЕРЯЮТСЯ ТОЛЬКО СЧЁТЧИКИ. Подписи времени ответа у экрана и выгрузки
   * различаются НАМЕРЕННО: на экране «Первый ответ» и «В рабочие часы», в
   * файле — «FRT ср., с», «FRT мед., с», «FRT мед. (раб.), с» с единицами и в
   * трёх столбцах вместо двух. Требовать здесь совпадения значило бы ломать
   * осмысленное различие ради формального правила.
   */
  const заголовкиВыгрузки = (): string => {
    const src = readFileSync("../app/services/stats.py", "utf8");
    const блок = /MANAGER_HEADERS: tuple\[str, \.\.\.\] = \(([\s\S]*?)\)/.exec(src);
    expect(блок, "MANAGER_HEADERS пропал из app/services/stats.py").not.toBeNull();
    return блок![1];
  };

  it("«Ответил первым» стоит и в выгрузке, а старого «Отвечено» там нет", () => {
    const блок = заголовкиВыгрузки();
    expect(
      блок,
      "лист «Менеджеры» не переименован — в одном файле снова два «Отвечено» с разными числами",
    ).toMatch(/"Ответил первым"/);
    expect(
      /"Отвечено"/.test(блок),
      "в заголовках выгрузки снова появилось голое «Отвечено»",
    ).toBe(false);
  });

  it("счётчики экрана и выгрузки названы одинаково", () => {
    const блок = заголовкиВыгрузки();
    for (const подпись of ["Принято", "Ответил первым", "Закрыто"]) {
      expect(блок, `счётчик «${подпись}» в выгрузке назван иначе, чем на экране`).toContain(
        `"${подпись}"`,
      );
    }
  });
});

/* ───────────────── 4. выход из тупика на экране отказа ──────────────────── */

describe("Отказ по дорогой сортировке даёт рабочий выход (H-01)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  const отдать = (ответТаблицы: Response) => {
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const u = new URL(String(input), "http://localhost");
      if (u.pathname.endsWith("/conversations/table")) return ответТаблицы;
      if (u.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      if (u.pathname.endsWith("/avito-accounts")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      return jsonResponse(200, { items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
  };

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "stats:all", "accounts:read"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("предлагает сбросить сортировку и не предлагает бесполезный повтор", async () => {
    отдать(
      jsonResponse(
        400,
        errorEnvelope(
          "validation_error",
          "Сортировка по этой колонке считается по всей выборке, а в ней 50955 диалогов — сузьте период или фильтры",
          { max_rows: 20000, total: 50955, sort: "first_response_sec" },
        ),
      ),
    );
    renderWithProviders(<TablePage />, {
      /*
       * ⚠ ФИЛЬТР В АДРЕСЕ ОБЯЗАТЕЛЕН. Без него проверка не отличила бы
       * «Сбросить сортировку» от прежнего «Сбросить фильтры»: сбрасывать было
       * бы нечего, и обе кнопки выглядели бы одинаково правильными.
       */
      route: "/dialogs?sort=first_response_sec&dir=desc&status=in_progress",
    });

    const кнопка = await screen.findByRole("button", { name: "Сбросить сортировку" });
    expect(
      screen.queryByRole("button", { name: "Повторить" }),
      "«Повторить» вернёт ровно тот же отказ — эту кнопку показывать нельзя",
    ).toBeNull();

    await userEvent.click(кнопка);
    /*
     * Проверяем не адрес, а ПОСЛЕДСТВИЕ: после сброса запрос уходит без
     * дорогой сортировки. Иначе сторож зеленел бы и на кнопке, которая правит
     * адрес, но не перезапрашивает.
     */
    await waitFor(() => {
      const последний = fetchMock.mock.calls
        .map((c) => new URL(String(c[0]), "http://localhost"))
        .filter((u) => u.pathname.endsWith("/conversations/table"))
        .at(-1)!;
      expect(последний.searchParams.get("sort")).not.toBe("first_response_sec");
      expect(
        последний.searchParams.get("status"),
        "сброс сортировки унёс с собой фильтр человека — это работа кнопки «Сбросить фильтры», а не этой",
      ).toBe("in_progress");
    });
  });

  it("у отказа по глубине страницы свой выход — «В начало списка»", async () => {
    /*
     * ⚠ ВТОРОЙ НЕПОВТОРИМЫЙ ОТКАЗ, И ОН ЧУТЬ НЕ ОСТАЛСЯ В ТУПИКЕ. Сервер режет
     * глубину пагинации (`offset > MAX_OFFSET`, потолок 5000) тем же кодом
     * `validation_error` и тем же статусом 400, но кладёт в `details`
     * `max_offset`, а не `sort`. Признак, написанный только под сортировку,
     * оставлял бы здесь «Повторить» — то есть тот же адрес с той же глубиной
     * и тот же отказ.
     */
    отдать(
      jsonResponse(
        400,
        errorEnvelope("validation_error", "Слишком глубокая страница — уточните фильтр", {
          max_offset: 5000,
        }),
      ),
    );
    renderWithProviders(<TablePage />, { route: "/dialogs?offset=9000&status=in_progress" });

    const кнопка = await screen.findByRole("button", { name: "В начало списка" });
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();

    await userEvent.click(кнопка);
    await waitFor(() => {
      const последний = fetchMock.mock.calls
        .map((c) => new URL(String(c[0]), "http://localhost"))
        .filter((u) => u.pathname.endsWith("/conversations/table"))
        .at(-1)!;
      expect(последний.searchParams.get("offset")).not.toBe("9000");
      expect(
        последний.searchParams.get("status"),
        "выход из глубины унёс фильтр человека",
      ).toBe("in_progress");
    });
  });

  it("у обычного отказа «Повторить» остаётся", async () => {
    /*
     * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ЗДЕСЬ ОБЯЗАТЕЛЬНА. Признак отказа узнаётся по
     * `details.sort`; напиши его слишком широко — и «Повторить» исчезнет у
     * всех отказов, включая обрыв связи, где повтор как раз и лечит.
     */
    отдать(jsonResponse(500, errorEnvelope("internal_error", "Что-то пошло не так")));
    renderWithProviders(<TablePage />, { route: "/dialogs" });

    expect(await screen.findByRole("button", { name: "Повторить" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Сбросить сортировку" })).toBeNull();
  });
});

/* ─────────── 5. одно слово — одно число (найдено разбором 08.09) ────────── */

describe("Одно слово не стоит над двумя числами", () => {
  /*
   * ⚠ КЛАСС ТОТ ЖЕ, ЧТО У H-03, И ОН ПОВТОРЯЕТСЯ. На экране «Диалоги бота»
   * живая полоса печатала «N реплик» — реплики БОТА, — а колонка таблицы под
   * заголовком «Реплик» показывала `messages_count`, то есть ВСЮ переписку
   * вместе с сообщениями клиента и оператора. Числа расходятся в разы на
   * одном экране, и читается это как ошибка счёта.
   */
  it("на экране диалогов бота колонка называется «Сообщений», а не «Реплик»", () => {
    const src = readFileSync("src/features/bot-dialogs/BotDialogsPage.tsx", "utf8");
    /* Ищем в РАЗМЕТКЕ, а не во всём файле: слово «реплик» законно живёт в
       комментариях и в подписи живой полосы, где оно верно. */
    const шапка = /<thead>([\s\S]*?)<\/thead>/.exec(src)?.[1] ?? "";
    expect(шапка, "шапки таблицы не нашлось — проверка потеряла предмет").not.toBe("");
    expect(
      /Реплик/.test(шапка),
      "колонка снова названа «Реплик» — тем же словом, что и полоса выше, при другом числе",
    ).toBe(false);
    expect(шапка).toContain("Сообщений");
  });
});


/* ────── 6. расхождения объяснены там, где их видят (разбор 08.09) ───────── */

describe("Законные расхождения названы своими словами", () => {
  /*
   * ⚠ ВСЕ ТРИ РАСХОЖДЕНИЯ ТАБЛИЦЫ ИМЕЮТ ОДИН КОРЕНЬ, и объяснение стоит ОДНО.
   * «Сообщений» против графика «Исходящие», «Первый ответ» против одноимённой
   * карточки, «Ответил первым» против «Отвечено» — всюду дело в том, что
   * строка бывает только у того, чьё имя записано, а ответ из приложения Авито
   * автора не имеет. Три подсказки об одном читатель складывает в «тут всё
   * не сходится»; одна строка под заголовком отвечает сразу.
   */
  it("под заголовком таблицы сказано, почему её числа меньше карточек", () => {
    renderWithProviders(<ТаблицаЛюдей />);
    const строка = screen.getByText(/Таблица считает только то, что можно приписать человеку/);
    expect(строка.textContent, "оговорка не называет источник безымянных ответов").toMatch(/Авито/);
  });

  it("у графика «Закрыто» сказано, почему сумма столбиков больше карточки", () => {
    /*
     * Оговорка привязана к МЕТРИКЕ, а не к панели: у остальных метрик
     * расхождения нет, и лишняя строка под ними была бы шумом.
     */
    const общее: MetricChartProps = {
      metric: "conversations_closed",
      group: "day",
      onMetricChange: () => {},
      onGroupChange: () => {},
      isPending: false,
      isError: false,
      onRetry: () => {},
      data: { metric: "conversations_closed", group: "day", points: [], refreshed_at: null },
      hourAllowed: true,
    };
    const { unmount } = renderWithProviders(<MetricChart {...общее} />);
    expect(screen.getByText(/сумма столбиков бывает больше/)).toBeInTheDocument();
    unmount();

    renderWithProviders(<MetricChart {...общее} metric="conversations_new" />);
    expect(
      screen.queryByText(/сумма столбиков бывает больше/),
      "оговорка показана у метрики, где расхождения нет",
    ).toBeNull();
  });
});
