import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { MetricChart } from "@/features/stats/components/MetricChart";
import { LeadbotTab } from "@/features/settings/leadbot/LeadbotTab";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * ЭКРАН НЕ ИМЕЕТ ПРАВА УТВЕРЖДАТЬ ТО, ЧЕГО НЕ ЗНАЕТ.
 *
 * Две находки, механизм общий: отсутствие данных и НЕВОЗМОЖНОСТЬ их получить
 * склеены в одну ветку, и человек читает сбой как факт о мире.
 *
 * ⚠ ГРАФИК СТАТИСТИКИ. Признак наличия данных считался как «есть значение
 * больше нуля». Но сервер для счётных метрик отдаёт сплошной занулённый ряд:
 * пустых точек у «новых диалогов» и «сообщений» не бывает вовсе. Ветка «данных
 * нет» у них недостижима, а срабатывала она ровно в обратном случае — на
 * настоящих нулях. Руководитель выбирает мёртвый канал (именно это он и
 * проверяет), карточка показывает 0, а график ниже — «Нет данных за выбранный
 * период»: он читает это как сбой отчёта и уходит, не увидев, что канал молчит
 * месяц.
 *
 * ⚠ ЖУРНАЛ ЛИД-БОТА. `rows = calls.data?.items ?? []`, и единственное условие —
 * длина массива. При отказе запроса `data` остаётся undefined, и экран печатал
 * «Ничего не сломалось» — ровно в тот момент, ради которого журнал и открывают.
 */

describe("График статистики", () => {
  const общие = {
    isPending: false,
    isError: false,
    onRetry: () => {},
    metric: "conversations_new" as const,
    group: "day" as const,
    onMetricChange: () => {},
    onGroupChange: () => {},
    hourAllowed: true,
  };

  it("сплошные нули — это данные, а не их отсутствие", () => {
    renderWithProviders(
      <MetricChart
        {...общие}
        data={{
          metric: "conversations_new",
          group: "day",
          refreshed_at: null,
          points: [
            { ts: "2026-08-01", value: 0 },
            { ts: "2026-08-02", value: 0 },
            { ts: "2026-08-03", value: 0 },
          ],
        }}
      />,
    );

    expect(
      screen.queryByText("Нет данных за выбранный период"),
      "график назвал настоящие нули отсутствием данных — это разные утверждения",
    ).toBeNull();
  });

  it("ряда нет вовсе — говорим об этом прямо", () => {
    renderWithProviders(
      <MetricChart
        {...общие}
        data={{ metric: "conversations_new", group: "day", refreshed_at: null, points: [] }}
      />,
    );

    expect(screen.getByText("Нет данных за выбранный период")).toBeTruthy();
  });
});

describe("Журнал лид-бота", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["bots:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("отказ загрузки не выдаётся за «ничего не сломалось»", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/leadbot/calls")) {
          return new Response(JSON.stringify({ error: { code: "server_error" } }), { status: 500 });
        }
        return new Response(
          JSON.stringify({
            connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
            is_ready: true,
            enabled: false,
            mode: "suggest",
            context_messages: 30,
            account_ids: [],
            accounts: [],
          }),
          { status: 200 },
        );
      }),
    );

    renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });

    expect(await screen.findByText(/Не удалось загрузить журнал/)).toBeTruthy();
    expect(screen.queryByText("Ничего не сломалось")).toBeNull();
    expect(screen.queryByText("Лид-бот пока не отвечал")).toBeNull();
  });

  it("настоящая тишина по-прежнему называется тишиной", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const тело = url.includes("/leadbot/calls")
          ? { items: [] }
          : {
              connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
              is_ready: true,
              enabled: false,
              mode: "suggest",
              context_messages: 30,
              account_ids: [],
              accounts: [],
            };
        return new Response(JSON.stringify(тело), { status: 200 });
      }),
    );

    renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });

    expect(await screen.findByText("Лид-бот пока не отвечал")).toBeTruthy();
  });
});
