import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "@/features/chats/components/composer/Composer";
import { queryClient } from "@/app/queryClient";
import { fakeUser, jsonResponse, errorEnvelope, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * «ВЕРНУТЬ В РАБОТУ» ДЕЙСТВИТЕЛЬНО ВОЗВРАЩАЕТ ВОЗМОЖНОСТЬ ПИСАТЬ.
 *
 * НАЙДЕНО ОБХОДОМ КОДА 12 августа. Плашка «Диалог закрыт» показывается по ИЛИ:
 * диалог закрыт ЛИБО последняя отправка вернула 422 `conversation_closed`.
 * Второе слагаемое — ошибка мутации, и она не сбрасывалась нигде.
 *
 * Сценарий, который от этого ломался: оператор отвечает в диалог, который в
 * эту самую секунду закрыл коллега; получает 422; жмёт «Вернуть в работу».
 * Статус меняется, диалог снова в работе — а плашка стоит, потому что ошибка
 * осталась прежней. Писать нельзя до перезагрузки страницы, и догадаться до
 * неё можно, только зная устройство.
 *
 * Тест ходит ровно этим путём: отправка → 422 → кнопка → поле ввода вернулось.
 */

const CONV_ID = "11111111-2222-3333-4444-555555555555";

function conversation(status: string) {
  return {
    id: CONV_ID,
    channel: "avito",
    status,
    unread_count: 0,
    client: { id: "c1", name: "Клиент", channel: "avito" },
    account: { id: "a1", title: "Парт-7" },
  };
}

describe("Композер после 422 «диалог закрыт»", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "conversations:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("«Вернуть в работу» снимает плашку, а не только меняет статус", async () => {
    const user = userEvent.setup();
    let статус = "in_progress";

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const method = init?.method ?? "GET";

        // Отправка — отказ «диалог уже закрыт» (закрыл коллега секунду назад).
        if (method === "POST" && url.pathname.endsWith("/messages")) {
          return jsonResponse(
            422,
            errorEnvelope("validation_error", "Диалог закрыт", {
              reason: "conversation_closed",
            }),
          );
        }
        // Возврат в работу — успешен.
        if (method === "PATCH" && url.pathname.endsWith("/status")) {
          статус = "in_progress";
          return jsonResponse(200, conversation(статус));
        }
        return jsonResponse(200, conversation(статус));
      }),
    );

    renderWithProviders(
      <Composer convId={CONV_ID} conversation={conversation("in_progress") as never} />,
    );

    const поле = await screen.findByRole("textbox");
    await user.type(поле, "Здравствуйте, мастер выехал");
    await user.click(screen.getByRole("button", { name: /Отправить/i }));

    /*
     * ⚠ ДОГОВОР ИЗМЕНЁН 03.09. Раньше отказ подменял поле ввода плашкой
     * «Диалог закрыт.» — вместе с набранным ответом, который сервер как раз
     * вернул в черновик. Теперь поле остаётся живым, а над ним встаёт строка с
     * объяснением, почему «Отправить» погасла: владелец просил, чтобы чужое
     * закрытие «никак не помешало другому человеку».
     */
    await screen.findByText(/Диалог закрыт — верните в работу/);
    expect(screen.getByRole("textbox"), "поле унесли вместе с ответом").toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Вернуть в работу" }));

    // ГЛАВНОЕ: запрет снят. Без сброса ошибки мутации строка стояла бы вечно —
    // статус уже in_progress, а условие держится на старом отказе.
    await waitFor(() =>
      expect(screen.queryByText(/Диалог закрыт — верните в работу/)).toBeNull(),
    );
    expect(await screen.findByRole("textbox")).toBeInTheDocument();
  });

  it("плашка уходит и когда диалог уже открыл кто-то другой", async () => {
    /*
     * УЗКИЙ ХВОСТ ПЕРВОЙ ПОЧИНКИ, найденный проверкой находки 12 августа.
     * Сброс висел на `onSuccess`. Но между отказом и нажатием «Вернуть в
     * работу» диалог могли открыть без нас: клиент написал, сторож переоткрыл,
     * коллега взял. Тогда ручка отвечает 422 «этот статус уже стоит», успеха
     * нет — и плашка стояла дальше при живом диалоге.
     */
    const user = userEvent.setup();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (method === "POST" && url.pathname.endsWith("/messages")) {
          return jsonResponse(
            422,
            errorEnvelope("validation_error", "Диалог закрыт", {
              reason: "conversation_closed",
            }),
          );
        }
        // Смена статуса отвергнута: диалог уже в работе стараниями коллеги.
        if (method === "PATCH" && url.pathname.endsWith("/status")) {
          return jsonResponse(
            422,
            errorEnvelope("validation_error", "Этот статус уже стоит", {
              reason: "same_status",
            }),
          );
        }
        return jsonResponse(200, conversation("in_progress"));
      }),
    );

    renderWithProviders(
      <Composer convId={CONV_ID} conversation={conversation("in_progress") as never} />,
    );

    const поле = await screen.findByRole("textbox");
    await user.type(поле, "Мастер выехал");
    await user.click(screen.getByRole("button", { name: /Отправить/i }));
    await screen.findByText(/Диалог закрыт — верните в работу/);

    await user.click(screen.getByRole("button", { name: "Вернуть в работу" }));

    // Даже при отказе ручки статуса запрет обязан уйти: диалог в работе, и
    // держала его только устаревшая ошибка отправки.
    await waitFor(() =>
      expect(screen.queryByText(/Диалог закрыт — верните в работу/)).toBeNull(),
    );
    expect(await screen.findByRole("textbox")).toBeInTheDocument();
  });
});
