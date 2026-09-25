import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConnectionIndicator } from "@/app/ConnectionIndicator";
import {
  useConnectionStore,
  отметитьВозвратСвязи,
  ОКНО_ОБРЫВОВ_МС,
} from "@/shared/realtime/connectionStore";

/**
 * ТОЧКА СВЯЗИ ЗНАЕТ ПРО ЖИВОЙ КАНАЛ, А НЕ ТОЛЬКО ПРО HTTP.
 *
 * ⚠ РАЗБОР 03.09. Сотрудник сообщил: «сайт работал в пять раз медленнее».
 * Сервер в ту минуту отвечал за 25 мс по медиане, а у него самого сокет
 * оборвался и вернулся шесть раз за час. Узнать это по экрану он не мог:
 * состояние точки бралось из опроса `/api/health`, который проходил успешно,
 * — точка светилась зелёным «На связи». Полоса «нет соединения» гаснет через
 * две секунды после возврата, так что при обрыве в одну секунду человек видит
 * короткую вспышку; шесть вспышек за час читаются как «сайт тормозит».
 */
function стенд() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        ({
          ok: true,
          status: 200,
          json: async () => ({ status: "ok", db: true, redis: true, version: "test" }),
        }) as unknown as Response,
    ),
  );
  render(
    <QueryClientProvider client={qc}>
      <MantineProvider>
        <ConnectionIndicator />
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("Индикатор связи и обрывы живого канала", () => {
  beforeEach(() => {
    useConnectionStore.setState({ status: "open", lastEventAt: null, reconnects: [] });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("сервер отвечает и канал ровный — «На связи»", async () => {
    стенд();
    expect(await screen.findByText("На связи")).toBeInTheDocument();
  });

  it("одиночный обрыв тревоги не поднимает", async () => {
    /*
     * Одиночный обрыв бывает у всех и лечится сам за секунду. Писать о нём —
     * пугать без повода, а напуганный человек звонит администратору.
     */
    стенд();
    await screen.findByText("На связи");
    отметитьВозвратСвязи();
    отметитьВозвратСвязи();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("На связи")).toBeInTheDocument();
  });

  it("три возврата за десять минут — «Связь нестабильна», хотя сервер отвечает", async () => {
    стенд();
    await screen.findByText("На связи");
    отметитьВозвратСвязи();
    отметитьВозвратСвязи();
    отметитьВозвратСвязи();
    expect(await screen.findByText("Связь нестабильна")).toBeInTheDocument();
  });

  it("старые обрывы забываются: вчерашние вспышки сегодня не считаются", async () => {
    const давно = Date.now() - ОКНО_ОБРЫВОВ_МС - 1000;
    стенд();
    await screen.findByText("На связи");
    отметитьВозвратСвязи(давно);
    отметитьВозвратСвязи(давно);
    отметитьВозвратСвязи();
    /*
     * ⚠ ЖДЁМ ПЕРЕРИСОВКИ И ПРОВЕРЯЕМ ОТСУТСТВИЕ ТРЕВОГИ, А НЕ НАЛИЧИЕ ПОКОЯ.
     * Первая редакция стояла на `waitFor(() => getByText("На связи"))` — и
     * проходила С ПЕРВОЙ ЖЕ ПОПЫТКИ, до перерисовки: `waitFor` возвращается,
     * как только условие выполнено, а в тот момент на экране ещё стоял старый
     * текст. Диверсия «читатель не отсекает старые метки» проходила насквозь.
     */
    await new Promise((r) => setTimeout(r, 0));
    expect(
      screen.queryByText("Связь нестабильна"),
      "вчерашние обрывы посчитаны как сегодняшние",
    ).toBeNull();
  });
});
