import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { StatsPage } from "@/features/stats/StatsPage";
import { ExportModal } from "@/features/stats/components/ExportModal";
import { exportErrorMessage, failureText, fileNameFromUrl } from "@/shared/export/useExportJob";
import { ApiError } from "@/shared/api/http";
import type { HeatmapCell } from "@/shared/api/types";
import { useLocation } from "react-router-dom";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const SUMMARY = {
  period: { date_from: "2026-07-07", date_to: "2026-08-05", tz: "Europe/Moscow" },
  prev_period: { date_from: "2026-06-07", date_to: "2026-07-06" },
  refreshed_at: "2026-08-05T11:05:12Z",
  // Период захватывает сегодня → карточки «за период» посчитаны живьём и
  // метка «Данные на 14:05» к ним не относится (STATS-04).
  period_live: true,
  // Часы НЕ 10–20 намеренно: подпись, печатающая зашитое окно, на них
  // разошлась бы с данными, а тест бы этого не заметил (FUNC-42).
  work_hours: { start_hour: 9, end_hour: 21 },
  cards: {
    conversations_new: { value: 312, prev: 280, delta_pct: 11.4 },
    conversations_closed: { value: 290, prev: 301, delta_pct: -3.7 },
    in_progress_now: { value: 47, prev: null, delta_pct: null, unassigned: 5 },
    // Три снимка «сейчас» — три РАЗНЫХ числа: равные пропустили бы подмену
    // одной карточки другой, а именно так и выглядел STATS-01.
    waiting_now: { value: 6, prev: null, delta_pct: null },
    queue_now: { value: 23, prev: null, delta_pct: null },
    frt_operator: {
      median_sec: 95,
      avg_sec: 340,
      median_biz_sec: 88,
      avg_biz_sec: 210,
      answered: 260,
      unanswered: 52,
      prev_median_sec: 120,
      delta_pct: -20.8,
    },
    frt_bot: { median_sec: 3, avg_sec: 4, answered: 295 },
    bot_closed: { pct: 18.6, closed_by_bot: 54, closed_total: 290, prev_pct: 15.2, delta_pct: 3.4 },
    phones_collected: { value: 78, by_source: { bot: 41, regex: 30, manual: 7 }, prev: 65, delta_pct: 20 },
    repeat_contacts: { reopened: 25, repeat_clients: 19, prev_reopened: 21 },
  },
};

const MANAGERS = {
  period: { date_from: "2026-07-07", date_to: "2026-08-05" },
  refreshed_at: "2026-08-05T11:05:12Z",
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
    {
      manager_id: "m-3",
      full_name: "Пётр Сидоров",
      // Активен, но в /users/assignable его НЕТ: снят с раздачи диалогов.
      is_active: true,
      taken: 7,
      answered: 6,
      closed: 5,
      frt_avg_sec: 120,
      frt_median_sec: 100,
      frt_median_biz_sec: 90,
      messages_sent: 44,
    },
    {
      manager_id: "m-2",
      full_name: "Олег Иванов",
      is_active: false,
      taken: 4,
      answered: 3,
      closed: 2,
      frt_avg_sec: null,
      frt_median_sec: null,
      frt_median_biz_sec: null,
      messages_sent: 12,
    },
  ],
  totals: {
    taken: 120,
    answered: 260,
    closed: 290,
    frt_median_sec: 95,
    frt_median_biz_sec: 88,
    messages_sent: 1834,
  },
};

const HEAT_CELLS: HeatmapCell[] = [];
for (let dow = 1; dow <= 7; dow += 1) {
  for (let hour = 0; hour < 24; hour += 1) HEAT_CELLS.push({ dow, hour, value: hour });
}

/** Экран статистики целиком (11 §6): фильтры, карточки, график, карта, таблица. */
describe("StatsPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const urls = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "stats:all", "accounts:read"],
      accessToken: "t",
      bootstrapped: true,
    });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/stats/summary")) return jsonResponse(200, SUMMARY);
      if (url.pathname.endsWith("/stats/timeseries")) {
        return jsonResponse(200, {
          metric: "conversations_new",
          group: "day",
          refreshed_at: SUMMARY.refreshed_at,
          points: [
            { ts: "2026-08-01", value: 41 },
            { ts: "2026-08-02", value: 55 },
          ],
        });
      }
      if (url.pathname.endsWith("/stats/heatmap")) {
        return jsonResponse(200, { tz: "Europe/Moscow", metric: "messages_in", cells: HEAT_CELLS });
      }
      if (url.pathname.endsWith("/stats/managers")) return jsonResponse(200, MANAGERS);
      if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      if (url.pathname.endsWith("/avito-accounts")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("показывает карточки, свежесть данных, график, карту и таблицу", async () => {
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });

    expect(await screen.findByText("312")).toBeInTheDocument(); // новые диалоги
    // FRT-медиана: 95 секунд → «1 м 35 с» (то же значение есть в строке «Итого»).
    expect(container.querySelector('.stat-card[data-metric="frt_operator"] .stat-card__value')).toHaveTextContent(
      "1 м 35 с",
    );
    expect(screen.getByText("18,6 %")).toBeInTheDocument(); // закрыто ботом
    expect(screen.getByText(/Данные на/)).toBeInTheDocument();

    await waitFor(() => expect(container.querySelectorAll(".heatmap__cell")).not.toHaveLength(0));
    expect(container.querySelectorAll(".chart-bar").length).toBeGreaterThan(0);

    // По роли, а не по тексту: с 15 августа каждый человек из таблицы попадает
    // и в опции фильтра, и голый getByText находил бы два элемента.
    expect(screen.getByRole("button", { name: "Показать статистику: Анна Смирнова" })).toBeInTheDocument();
    expect(screen.getByText("отключён")).toBeInTheDocument(); // деактивированный сотрудник
    expect(screen.getByText("ИТОГО")).toBeInTheDocument();
  });

  it("дельта FRT показана как улучшение, дельта «закрыто» — как падение", async () => {
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    const frt = container.querySelector('.stat-card[data-metric="frt_operator"] .stat-card__delta');
    expect(frt).toHaveAttribute("data-tone", "good");
    expect(frt?.textContent).toContain("быстрее");

    const closed = container.querySelector('.stat-card[data-metric="conversations_closed"] .stat-card__delta');
    expect(closed).toHaveAttribute("data-tone", "bad");
  });

  it("«Ждут ответа» и «В очереди» — разные карточки с разными числами (STATS-01)", async () => {
    // Очередь стояла под подписью «Ждут ответа»: руководитель видел
    // неразобранную очередь там, где ему обещали невыполненную работу
    // диспетчеров, и то же число дублировало бейдж вкладки «Входящие».
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    const value = (metric: string) =>
      container.querySelector(`.stat-card[data-metric="${metric}"] .stat-card__value`)?.textContent;

    expect(value("waiting_now")).toBe("6");
    expect(value("queue_now")).toBe("23");
    expect(value("waiting_now")).not.toBe(value("queue_now"));
  });

  it("подсказка «Ждут ответа» описывает то же, что посчитано (STATS-01)", async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    const card = container.querySelector('.stat-card[data-metric="waiting_now"]');
    await user.hover(card as Element);

    // Слово «в работе» здесь несущее: оно отделяет карточку от очереди.
    expect(await screen.findByText(/Диалоги в работе, где клиент написал/)).toBeInTheDocument();
    expect(screen.queryByText(/последнее сообщение — от клиента/)).toBeNull();
  });

  it("подсказка «В работе» называет диалоги без ответственного (проверка 24.09)", async () => {
    // Было «взятые кем-то из сотрудников», а в числе лежали и ничьи — на бою
    // 51 из 305. Число оставлено полным, ничьи названы рядом.
    const user = userEvent.setup();
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    await user.hover(container.querySelector('.stat-card[data-metric="in_progress_now"]') as Element);

    expect(await screen.findByText(/Из них без ответственного: 5/)).toBeInTheDocument();
    expect(screen.queryByText(/взятые кем-то из сотрудников/)).toBeNull();
  });

  it("группа «За период» подписывает свою свежесть, а не общую метку экрана (STATS-04)", async () => {
    // Метка «Данные на 14:05» одна на весь экран и относится к витрине.
    // Карточки этой группы при периоде с сегодня считаются живьём — если это
    // не написано рядом с ними, читатель относит метку и к ним тоже.
    renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    expect(screen.getByText("на момент открытия страницы")).toBeInTheDocument();
    // Прежняя подсказка обещала, что раз в час обновляется ВЕСЬ экран.
    expect(screen.queryByText("Агрегаты обновляются раз в час")).toBeNull();
  });

  it("период целиком в прошлом подписан как витринный (STATS-04)", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/stats/summary")) {
        return jsonResponse(200, { ...SUMMARY, period_live: false });
      }
      if (url.pathname.endsWith("/stats/managers")) return jsonResponse(200, MANAGERS);
      if (url.pathname.endsWith("/stats/heatmap")) {
        return jsonResponse(200, { tz: "Europe/Moscow", metric: "messages_in", cells: HEAT_CELLS });
      }
      return jsonResponse(200, { items: [], points: [] });
    });

    renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    expect(screen.getByText("по витрине статистики")).toBeInTheDocument();
    expect(screen.queryByText("на момент открытия страницы")).toBeNull();
  });

  it("подсказка FRT печатает ДЕЙСТВУЮЩИЕ рабочие часы, а не зашитые (FUNC-42)", async () => {
    // Окно настраивается (#41). Пока оно было напечатано числами в разметке,
    // цифра считалась по новым часам, а подпись обещала старые.
    const user = userEvent.setup();
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    await user.hover(container.querySelector('.stat-card[data-metric="frt_operator"]') as Element);

    expect(await screen.findByText(/в рабочие часы 09:00–21:00/)).toBeInTheDocument();
    expect(screen.queryByText(/10:00–20:00/)).toBeNull();
  });

  it("«Диалоги ↗» снимает чужие сужения и уносит аккаунт (STATS-07)", async () => {
    // Фильтры чатов переживают уход со страницы, а setFilters сливает
    // переданное с текущим. Передавался один менеджер — поиск и тег, набранные
    // в чатах час назад, оставались наложенными: руководитель приходил из
    // отчёта, где у сотрудника 34 диалога, видел два и винил отчёт.
    const { useChatUiStore } = await import("@/shared/stores/chatUiStore");
    useChatUiStore.setState({
      filters: {
        tab: "mine",
        q: "иванов",
        tag: "негатив",
        status: "new",
        accountId: "acc-old",
        // Сужения переключателями (проверка 24.09): «Без ответственного» с
        // сотрудником давало пустой список, «Ждут ответа» — только ждущих.
        unassigned: true,
        waitingOnly: true,
        withClosed: true,
      },
    });

    const user = userEvent.setup();
    renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("Анна Смирнова");

    await user.click(screen.getByRole("button", { name: "Диалоги: Анна Смирнова" }));

    const filters = useChatUiStore.getState().filters;
    expect(filters.assigneeId).toBe("m-1");
    expect(filters.q).toBeUndefined();
    expect(filters.tag).toBeUndefined();
    expect(filters.status).toBeUndefined();
    expect(filters.unassigned).toBeUndefined();
    expect(filters.waitingOnly).toBeUndefined();
    expect(filters.withClosed).toBeUndefined();
    // Аккаунт на /stats не выбран — значит и в чатах его быть не должно,
    // а не «остался с прошлого визита».
    expect(filters.accountId).toBeUndefined();
  });

  it("клик по заголовку колонки шлёт серверную сортировку и разворачивает порядок", async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("Анна Смирнова");

    await user.click(screen.getByRole("button", { name: /Принято/ }));
    await waitFor(() => {
      expect(urls().some((u) => u.includes("sort=taken") && u.includes("order=desc"))).toBe(true);
    });

    await user.click(screen.getByRole("button", { name: /Принято/ }));
    await waitFor(() => {
      expect(urls().some((u) => u.includes("sort=taken") && u.includes("order=asc"))).toBe(true);
    });
  });

  it("клик по строке менеджера фильтрует весь экран (manager_id уходит в запросы)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("Анна Смирнова");

    await user.click(screen.getByRole("button", { name: "Показать статистику: Анна Смирнова" }));

    await waitFor(() => {
      expect(urls().some((u) => u.includes("/stats/summary") && u.includes("manager_id=m-1"))).toBe(true);
    });
    // На тепловую карту фильтр по менеджеру не действует (06 §4.3).
    expect(urls().every((u) => !u.includes("/stats/heatmap") || !u.includes("manager_id"))).toBe(true);
    expect(await screen.findByText("все менеджеры")).toBeInTheDocument();
  });

  it("отключённый сотрудник из таблицы попадает в фильтр подписью, а не голым id", async () => {
    // `/users/assignable` знает только активных (01 §3.1) — в фикстуре он пуст;
    // Олег Иванов есть лишь в отчёте (06 §4.4). Без объединения списков Mantine
    // показал бы в фильтре пилюлю «m-2».
    const user = userEvent.setup();
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("Олег Иванов");

    await user.click(screen.getByRole("button", { name: "Показать статистику: Олег Иванов" }));

    await waitFor(() => {
      expect(container.querySelector(".mantine-MultiSelect-pill")).toHaveTextContent("Олег Иванов (отключён)");
    });
    expect(screen.queryByText("m-2")).toBeNull();
  });

  /**
   * ⚠ АКТИВНЫЙ, НО НЕ НАЗНАЧАЕМЫЙ — ТРЕТЬЕ МНОЖЕСТВО, И ЕГО ТЕРЯЛИ (15 августа).
   *
   * `/users/assignable` отдаёт тех, КОМУ МОЖНО ДАТЬ ДИАЛОГ; отчёт — всех, У
   * КОГО БЫЛА АКТИВНОСТЬ. Администратор, снятый с раздачи
   * (`handles_conversations = false`), в справочник не попадает никогда, а в
   * таблице стоит с ненулевыми числами. Старое накопление брало из таблицы
   * ТОЛЬКО отключённых — и такой человек не попадал в фильтр ниоткуда:
   * отчёт по нему есть, а сузить отчёт до него нельзя. В фикстуре он —
   * Пётр Сидоров: `is_active: true`, в assignable отсутствует.
   */
  /**
   * СРЕЗ ЖИВЁТ В АДРЕСЕ (п. 12 отчёта тестирования, 15 августа).
   *
   * Пока фильтры лежали в useState, руководитель настраивал период, канал и
   * сотрудника — а F5 или возврат из диалога сбрасывали всё к «30 дней, все
   * подряд», и «скинь ссылку на этот срез» было невыполнимой просьбой.
   * Проверяем через собственный пробник адреса: MemoryRouter настоящего
   * window.location не трогает.
   */
  it("клик по сотруднику кладёт сужение в адрес, повторный — убирает", async () => {
    const user = userEvent.setup();
    let search = "";
    function Probe() {
      search = useLocation().search;
      return <StatsPage />;
    }
    renderWithProviders(<Probe />, { route: "/stats" });
    await screen.findByText("Анна Смирнова");

    await user.click(screen.getByRole("button", { name: "Показать статистику: Анна Смирнова" }));
    await waitFor(() => expect(search).toContain("managers=m-1"));

    await user.click(screen.getByRole("button", { name: "Показать статистику: Анна Смирнова" }));
    await waitFor(() => expect(search).not.toContain("managers"));
  });

  it("незнакомые значения из адреса молча становятся умолчаниями, а не пустым экраном", async () => {
    // Урок `?period=-5` с «Разбора»: там неизвестный период не уходил в запрос
    // вовсе, и человек молча получал всю базу. Здесь та же ссылка из закладки
    // обязана показать отчёт за 30 дней с сортировкой по умолчанию.
    renderWithProviders(<StatsPage />, { route: "/stats?period=bogus&metric=junk&sort=hack&group=year" });

    expect(await screen.findByText("312")).toBeInTheDocument();
    const managersUrl = urls().find((u) => u.includes("/stats/managers"));
    expect(managersUrl).toContain("sort=messages_sent");
    const seriesUrl = urls().find((u) => u.includes("/stats/timeseries"));
    expect(seriesUrl).toContain("metric=conversations_new");
    expect(seriesUrl).toContain("group=day");
  });

  it("свой период без настоящих дат — это не срез: возвращаемся к 30 дням", async () => {
    renderWithProviders(<StatsPage />, { route: "/stats?period=custom&from=не-дата&to=2026-08-05" });

    expect(await screen.findByText("312")).toBeInTheDocument();
    // Запросы ушли с датами пресета last30, а не с мусором из адреса.
    expect(urls().some((u) => u.includes("не-дата"))).toBe(false);
  });

  it("активный сотрудник, которого нет в справочнике, всё равно попадает в фильтр", async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("Пётр Сидоров");

    await user.click(screen.getByRole("button", { name: "Показать статистику: Пётр Сидоров" }));

    await waitFor(() => {
      // Подпись без «(отключён)»: человек работает, он просто не в раздаче.
      expect(container.querySelector(".mantine-MultiSelect-pill")).toHaveTextContent("Пётр Сидоров");
    });
    expect(screen.queryByText("m-3")).toBeNull();
  });
});

describe("Экспорт статистики (11 §6.3)", () => {
  it("CSV прячет выбор листов — в CSV только «Диалоги» (06 §5.2)", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ExportModal
        opened
        onClose={() => {}}
        period={{ dateFrom: "2026-08-01", dateTo: "2026-08-04" }}
        managerIds={[]}
      />,
    );

    expect(screen.getByText("Листы")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "CSV" }));

    expect(screen.queryByText("Листы")).toBeNull();
    expect(screen.getByText(/CSV содержит только лист «Диалоги»/)).toBeInTheDocument();
  });

  it("тело запроса несёт период, формат, листы и СПИСОК менеджеров (06 §4.5)", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : {} });
        return jsonResponse(202, { job_id: "job-1" });
      }),
    );

    renderWithProviders(
      <ExportModal
        opened
        onClose={() => {}}
        period={{ dateFrom: "2026-08-01", dateTo: "2026-08-04" }}
        accountId="acc-1"
        managerIds={["m-1", "m-2"]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Выгрузить" }));

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls[0].url).toContain("/stats/export");
    expect(calls[0].body).toMatchObject({
      format: "xlsx",
      date_from: "2026-08-01",
      date_to: "2026-08-04",
      account_id: "acc-1",
      manager_id: ["m-1", "m-2"],
      sheets: ["summary", "managers", "conversations"],
    });

    vi.unstubAllGlobals();
  });

  it("409 и 429 превращаются в человеческие сообщения", () => {
    expect(exportErrorMessage(new ApiError(409, "export_already_running", "conflict"))).toBe(
      "Предыдущий экспорт ещё готовится",
    );
    expect(exportErrorMessage(new ApiError(429, "rate_limited", "too many"))).toBe(
      "Лимит выгрузок на сегодня исчерпан",
    );
    expect(exportErrorMessage(new ApiError(400, "period_too_long", "long"))).toBe(
      "Период больше 366 дней — сузьте диапазон",
    );
    expect(exportErrorMessage(new Error("boom"))).toBe("Не получилось запустить выгрузку");
    // Машинный код из статуса задачи — не текст для человека (проверка 24.09).
    expect(failureText("internal")).toBe("Не получилось собрать файл — повторите выгрузку позже");
    expect(failureText(null)).toBe("Не получилось собрать файл — повторите выгрузку позже");
    expect(failureText("Больше 100000 диалогов — сузьте период")).toBe(
      "Больше 100000 диалогов — сузьте период",
    );
  });

  it("имя файла берётся из подписанной ссылки", () => {
    expect(fileNameFromUrl("/api/v1/media/exports/leadchat-stats_2026-08-01_2026-08-04_b8c4.xlsx?sig=x&exp=1")).toBe(
      "leadchat-stats_2026-08-01_2026-08-04_b8c4.xlsx",
    );
  });
});
