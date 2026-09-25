import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TransferBar } from "@/features/chats/components/thread/TransferBar";
import type { ConversationDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

/**
 * Отмена передачи тем, кто её начал (просьба владельца 04.09).
 *
 * «Нужно будет сделать, чтобы можно было отменять передачу, если я случайно
 * начал передавать не тому человеку».
 *
 * Обратная половина — «Принять диалог» и «Отказаться» у получателя — работает
 * с 7 августа. У передающего же была одна строка «Ждёт подтверждения: Имя» и
 * ни одного способа забрать ошибку назад: предложение висит пятнадцать минут,
 * и всё это время он либо ждёт отказа от постороннего человека, либо просит
 * его об этом в мессенджере. Диалог при этом числится за ним, и клиент ждёт
 * ответа именно от него.
 *
 * Проверяется не «нарисована ли кнопка», а КОМУ она видна: кнопка отмены у
 * получателя означала бы «отказаться» вторым словом, а у постороннего —
 * отмену чужого решения жестом, похожим на «убрать с глаз».
 */

const GIVER = { id: "u-giver", full_name: "Анна Отдающая" };
const TAKER = { id: "u-taker", full_name: "Борис Принимающий" };
const ОТМЕНА = "Отменить передачу";

function сПредложением(): ConversationDto {
  return makeConversation({
    assignee: GIVER,
    transfer: {
      to: TAKER,
      by: GIVER,
      at: "2026-09-04T09:00:00Z",
      comment: "Клиент из Балашихи, это твой район",
    },
  });
}

describe("Отмена передачи передающим", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const posts = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "POST")
      .map((c) => String(c[0]));

  beforeEach(() => {
    queryClient.clear();
    fetchMock = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const как = (user: { id: string; full_name: string }) =>
    resetSessionStore({
      user: { ...fakeUser, id: user.id, full_name: user.full_name },
      permissions: ["messages:send", "conversations:read", "conversations:manage"],
      accessToken: "t",
      bootstrapped: true,
    });

  it("передающему даёт чем забрать предложение назад", () => {
    // Кнопка стоит рядом с напоминанием «Диалог пока за вами»: строка
    // объясняет, почему человек всё ещё отвечает за диалог, кнопка —
    // единственный способ это закончить, не дожидаясь чужого решения.
    как(GIVER);
    renderWithProviders(<TransferBar conversation={сПредложением()} />);

    expect(screen.getByText(/Ждёт подтверждения: Борис Принимающий/)).toBeInTheDocument();
    expect(screen.getByText(/Диалог пока за вами/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: ОТМЕНА })).toBeInTheDocument();
  });

  it("нажатие уходит на сервер", async () => {
    как(GIVER);
    renderWithProviders(<TransferBar conversation={сПредложением()} />);

    await userEvent.click(screen.getByRole("button", { name: ОТМЕНА }));
    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/transfer/cancel"))).toBe(true),
    );
  });

  it("получателю кнопки отмены нет — у него свои два ответа", () => {
    /*
     * Третья кнопка у получателя означала бы «отказаться», сказанное вторым
     * словом: два разных названия для одного действия — это выбор наугад.
     * Отменяет тот, кто передавал; отказывается тот, кому предложили.
     */
    как(TAKER);
    renderWithProviders(<TransferBar conversation={сПредложением()} />);

    expect(screen.getByRole("button", { name: "Принять диалог" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отказаться" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: ОТМЕНА })).not.toBeInTheDocument();
  });

  it("постороннему — тоже нет, даже с правом на управление диалогами", () => {
    /*
     * Сервер такого человека пустит (`conversations:manage` — то же право, что
     * у передачи), и это верно: чужой затор иногда разбирают руками. Но кнопка
     * рядом с ЧУЖОЙ передачей читается как «убрать эту строку с глаз», а не
     * как «отменить чужое решение», и нажимают её не думая.
     */
    как({ id: "u-head", full_name: "Руководитель" });
    renderWithProviders(<TransferBar conversation={сПредложением()} />);

    expect(
      screen.getByText(/Анна Отдающая → Борис Принимающий: ждёт подтверждения/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: ОТМЕНА })).not.toBeInTheDocument();
  });

  it("у диалога, вернувшегося в очередь, отмены нет", () => {
    /*
     * Боевой случай 28.08: диалог вернули в очередь, а предложение на нём
     * осталось. Полоса там уже не управление, а объяснение, откуда взялось
     * предложение (`actionable={false}`), и кнопка в одном ряду с «Принять
     * диалог» полосы очереди означала бы выбор наугад.
     */
    как(GIVER);
    renderWithProviders(<TransferBar conversation={сПредложением()} actionable={false} />);

    expect(screen.getByText(/Ждёт подтверждения: Борис Принимающий/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: ОТМЕНА })).not.toBeInTheDocument();
  });

  it("без предложения полосы нет вовсе", () => {
    // Отрицательная проверка обязана падать по своей причине: диалог здесь
    // ЕСТЬ и открыт своим же хозяином — не хватает ровно предложения.
    как(GIVER);
    renderWithProviders(<TransferBar conversation={makeConversation({ assignee: GIVER })} />);

    expect(screen.queryByText(/Ждёт подтверждения/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: ОТМЕНА })).not.toBeInTheDocument();
  });
});
