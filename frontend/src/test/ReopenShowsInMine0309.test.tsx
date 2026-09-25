import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { useChangeStatus } from "@/features/chats/hooks/useConversationActions";
import { refetchListsNow } from "@/shared/realtime/listRefetch";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/realtime/listRefetch", async (importOriginal) => {
  const настоящий = await importOriginal<typeof import("@/shared/realtime/listRefetch")>();
  return { ...настоящий, refetchListsNow: vi.fn() };
});

/**
 * ВЕРНУЛ ИЗ «ЗАКРЫТ» — СТРОКА ПОЯВЛЯЕТСЯ В «МОИХ» СРАЗУ.
 *
 * ⚠ ВТОРАЯ ЖАЛОБА ВЛАДЕЛЬЦА 03.09: «взял в работу и снова так же в „Мои"
 * диалог не появился». Первая её половина серверная — переоткрытие теперь
 * отдаёт диалог нажавшему. Но одного сервера мало: `applyConversationPatch`
 * правит строку ТАМ, ГДЕ ОНА УЖЕ ЕСТЬ, а в «Моих» закрытого диалога нет —
 * вкладка их исключает. Патчить нечего, а создать строку патч не умеет.
 * Обычный перезапрос схлопывается (полсекунды, потолок три), и человек читает
 * это как «опять не появился».
 */
describe("Возврат из «Закрыт» и вкладка «Мои»", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.mocked(refetchListsNow).mockClear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /*
   * Кнопка, меняющая статус тем же хуком, что и композер, — без закрытия.
   * Имя латиницей: правило react-hooks/rules-of-hooks узнаёт компонент по
   * заглавной ЛАТИНСКОЙ букве и на кириллическом имени ругается.
   */
  function StatusProbe() {
    const m = useChangeStatus(CONV_ID);
    return (
      <>
        <button
          type="button"
          onClick={() => m.mutate({ status: "waiting_client", прежний: "in_progress" })}
        >
          сменить статус
        </button>
        {m.isSuccess && <span>готово</span>}
      </>
    );
  }

  function стенд(статус: "closed" | "waiting_client") {
    const диалог = makeConversation({ status: статус });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PATCH" && String(input).includes("/status")) {
          return jsonResponse(200, { ...диалог, status: "in_progress", assignee: fakeUser });
        }
        return jsonResponse(200, { items: [], page: { limit: 200, offset: 0, total: 0 } });
      }),
    );
    renderWithProviders(
      статус === "closed" ? (
        <Composer convId={CONV_ID} conversation={диалог} />
      ) : (
        <StatusProbe />
      ),
    );
    return userEvent.setup();
  }

  it("перезапрашивает списки СРАЗУ, а не в хвосте чужой пачки", async () => {
    const user = стенд("closed");
    await user.click(await screen.findByRole("button", { name: "Вернуть в работу" }));
    await waitFor(() => expect(vi.mocked(refetchListsNow)).toHaveBeenCalled());
  });

  it("а на обычной смене статуса живого диалога — не перезапрашивает", async () => {
    /*
     * ⚠ ОТРИЦАТЕЛЬНАЯ ПОЛОВИНА ОБЯЗАНА УМЕТЬ ПОКРАСНЕТЬ, И ПЕРВАЯ РЕДАКЦИЯ НЕ
     * УМЕЛА. Она набирала текст в поле — то есть статус не меняла вовсе, и
     * перезапроса не было бы при любом коде. Диверсия «перезапрашивать всегда»
     * проходила насквозь. Теперь смена статуса настоящая: живой диалог уходит
     * в «Ждёт клиента» тем же хуком.
     *
     * Что стережём: схлопывание перезапросов. Полети оно на каждую смену —
     * списки дёргались бы на чужих событиях, ради чего схлопывание и заводили.
     */
    const user = стенд("waiting_client");
    await user.click(await screen.findByRole("button", { name: "сменить статус" }));
    await screen.findByText("готово");
    expect(vi.mocked(refetchListsNow)).not.toHaveBeenCalled();
  });
});
