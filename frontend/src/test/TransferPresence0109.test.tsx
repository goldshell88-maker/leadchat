import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { TransferDialog } from "@/features/chats/components/card/TransferDialog";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/realtime/notify", () => ({
  notifyNewMessage: vi.fn(),
  notifyAssignedToMe: vi.fn(),
}));

/**
 * СПИСОК «КОМУ ПЕРЕДАТЬ» ДОЛЖЕН ОТЛИЧАТЬ РАБОТАЮЩЕГО ОТ ОТОШЕДШЕГО.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 01.09 со скриншотом этой самой модалки: «показывается, что
 * люди онлайн в работе, а у кого-то просто вкладка открыта, у кого-то просто ПК
 * включён».
 *
 * ПРИЧИНА БЫЛА ЗДЕСЬ, А НЕ В МЕХАНИЗМЕ. Точка красилась по `is_online`, а это
 * поле на сервере означает «ключ присутствия существует», то есть «приложение
 * открыто и отвечает на пинги», — у ОТОШЕДШЕГО оно тоже true. Авто-«отошёл»
 * исправно писал статус: замер на бою 01.09 показал троих `away` и двоих
 * `online`, а в модалке все пятеро были зелёные.
 *
 * ⚠ ОТОШЕДШЕГО НЕ ПРЯЧЕМ. Он за столом: видит диалоги, отвечает, ему можно
 * передать — и сервер такую передачу принимает (`test_away_counts_as_present`).
 * Убрать его из списка значило бы соврать в другую сторону. Врёт не строка, а
 * её цвет, и лечится это подписью, а не фильтром.
 */

const СПИСОК = {
  items: [
    { id: "u-rab", full_name: "Пётр Ковалёв", role: "manager", is_online: true, presence: "online" },
    { id: "u-obed", full_name: "Анна Петрова", role: "manager", is_online: true, presence: "away" },
  ],
};

describe("«Передать диалог»: отошедший не выглядит работающим", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/users/assignable")) return jsonResponse(200, СПИСОК);
        return jsonResponse(200, {});
      }),
    );
  });

  it("⚠ отошедший подписан «отошёл», а работающий — нет", async () => {
    renderWithProviders(<TransferDialog convId={CONV_ID} opened onClose={() => {}} />);

    const обедающий = await screen.findByText("Анна Петрова");
    const строкаОбедающего = обедающий.closest("button");
    expect(
      строкаОбедающего?.textContent,
      "отошедший в списке неотличим от работающего — руководитель отдаёт диалог тому, кого нет за столом",
    ).toContain("отошёл");

    const работающий = await screen.findByText("Пётр Ковалёв");
    expect(
      работающий.closest("button")?.textContent,
      "работающему приписали «отошёл» — теперь врём в другую сторону",
    ).not.toContain("отошёл");
  });

  it("⚠ точка отошедшего другого цвета, а не та же зелёная", async () => {
    /*
     * Подписи мало: цвет читается первым и именно он вводил в заблуждение.
     * Проверяем состояние точки, а не наличие слова.
     */
    renderWithProviders(<TransferDialog convId={CONV_ID} opened onClose={() => {}} />);

    const обедающий = await screen.findByText("Анна Петрова");
    const точкаОбедающего = состояниеТочки(обедающий);
    const работающий = await screen.findByText("Пётр Ковалёв");
    const точкаРаботающего = состояниеТочки(работающий);

    expect(точкаРаботающего, "работающий перестал быть зелёным").toBe("online");
    expect(
      точкаОбедающего,
      "отошедший и работающий одного цвета — ровно то, на что жаловался владелец",
    ).toBe("away");
  });

  it("отошедший ОСТАЁТСЯ в списке: ему можно передать", async () => {
    renderWithProviders(<TransferDialog convId={CONV_ID} opened onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Анна Петрова")).toBeInTheDocument());
  });
});

/** Состояние точки в строке: у неё `data-state`. */
function состояниеТочки(имя: HTMLElement): string | null {
  const строка = имя.closest("button");
  return строка?.querySelector(".transfer-row__dot")?.getAttribute("data-state") ?? null;
}
