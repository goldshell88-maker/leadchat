/**
 * ТАБЛИЦА ЛЮДЕЙ НАЗЫВАЕТ СВОЙ ИСТОЧНИК (09.09).
 *
 * До этой правки «Ответил первым» и обе медианы FRT приходили с витрины
 * ВСЕГДА, а «Принято», «Закрыто» и «Сообщений» считались живьём — и одна
 * строка одного человека держала два разных момента времени. Источник
 * выровняли на сервере; здесь сторожится ВИДИМАЯ часть: экран обязан сказать
 * человеку, что он читает, теми же словами, что и группы карточек рядом.
 *
 * Поле необязательное: выкатка идёт по частям, и старый сервер его не
 * пришлёт. Тогда подпись обязана МОЛЧАТЬ, а не гадать — третья проверка.
 */
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { ManagersTable } from "@/features/stats/components/ManagersTable";
import type { ManagersResponse } from "@/shared/api/types";
import { renderWithProviders } from "./render";

const ОТВЕТ: ManagersResponse = {
  period: { date_from: "2026-09-09", date_to: "2026-09-09", tz: "Europe/Moscow" },
  refreshed_at: "2026-09-09T07:05:12Z",
  rows: [
    {
      manager_id: "m-1",
      full_name: "Анна Смирнова",
      is_active: true,
      taken: 4,
      answered: 4,
      closed: 2,
      frt_avg_sec: 210,
      frt_median_sec: 74,
      frt_median_biz_sec: 71,
      messages_sent: 12,
    },
  ],
  totals: {
    taken: 4,
    answered: 4,
    closed: 2,
    frt_median_sec: 74,
    frt_median_biz_sec: 71,
    messages_sent: 12,
  },
};

function таблица(data: ManagersResponse) {
  return (
    <ManagersTable
      data={data}
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

/** Заголовок панели вместе с подписью — читаем ровно то, что видит человек. */
const заголовок = (): string =>
  screen.getByRole("heading", { name: /Менеджеры/ }).textContent ?? "";

describe("Таблица людей называет свой источник", () => {
  it("период с сегодня — «на момент открытия страницы»", () => {
    renderWithProviders(таблица({ ...ОТВЕТ, period_live: true }));
    expect(заголовок()).toContain("на момент открытия страницы");
  });

  it("период в прошлом — «по витрине статистики»", () => {
    renderWithProviders(таблица({ ...ОТВЕТ, period_live: false }));
    expect(заголовок()).toContain("по витрине статистики");
  });

  it("старый сервер поля не прислал — подпись молчит", () => {
    renderWithProviders(таблица(ОТВЕТ));
    const текст = заголовок();
    expect(текст).not.toContain("на момент открытия");
    expect(текст).not.toContain("по витрине");
  });
});
