import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import type { ConversationDetailDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

/**
 * Предложение передачи с двух сторон (требование заказчика от 7 августа).
 *
 * Проверяется не «рисуются ли кнопки». Проверяется то, ради чего двухфазность
 * заводилась: обе стороны должны ПОНИМАТЬ, у кого сейчас диалог. Получатель —
 * что от него ждут решения (диалога нет ни в его «Моих», ни в очереди, узнать
 * больше неоткуда). Передающий — что диалог всё ещё за ним и отвечать клиенту
 * пока ему. Пропусти мы вторую половину, получилось бы ровно то, от чего
 * уходили: человек считает, что отдал, и перестаёт следить.
 */

const GIVER = { id: "u-giver", full_name: "Анна Отдающая" };
const TAKER = { id: "u-taker", full_name: "Борис Принимающий" };

function withOffer(): ConversationDetailDto {
  return makeConversation({
    assignee: GIVER,
    transfer: {
      to: TAKER,
      by: GIVER,
      at: "2026-08-07T09:00:00Z",
      comment: "Клиент из Балашихи, это твой район",
    },
  });
}

describe("Предложение передачи", () => {
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

  const as = (user: { id: string; full_name: string }) =>
    resetSessionStore({
      user: { ...fakeUser, id: user.id, full_name: user.full_name },
      permissions: ["messages:send", "conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });

  it("получателю показывает, кто передаёт, зачем и две кнопки", () => {
    as(TAKER);
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={withOffer()} />);

    expect(screen.getByText(/Анна Отдающая передаёт вам диалог/)).toBeInTheDocument();
    // «Почему передаю» нужнее самого факта: «это твой район» объясняет
    // предложение, а голое «вам передан диалог» — нет.
    expect(screen.getByText(/это твой район/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять диалог" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отказаться" })).toBeInTheDocument();
  });

  it("передающему напоминает, что диалог ВСЁ ЕЩЁ ЗА НИМ", () => {
    // Половина требования. Не видя этой строки, человек считает, что передал,
    // перестаёт следить за диалогом — а диалог по-прежнему его, и клиент ждёт
    // ответа именно от него.
    as(GIVER);
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={withOffer()} />);

    expect(screen.getByText(/Ждёт подтверждения: Борис Принимающий/)).toBeInTheDocument();
    expect(screen.getByText(/Диалог пока за вами/)).toBeInTheDocument();
    // Кнопок у него нет: решение не его.
    expect(screen.queryByRole("button", { name: "Принять диалог" })).not.toBeInTheDocument();
  });

  it("у передающего остаётся поле ввода", () => {
    // Пока предложение висит, диалог за ним — значит он обязан иметь чем
    // ответить клиенту. Отними мы композер, вышло бы ровно то, чего требование
    // избегает: диалог формально его, а написать нечем.
    as(GIVER);
    const { container } = renderWithProviders(
      <ThreadFooter convId="conv-1" conversation={withOffer()} />,
    );
    expect(container.querySelector("textarea")).toBeInTheDocument();
  });

  it("«Принять диалог» уходит на сервер", async () => {
    as(TAKER);
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={withOffer()} />);

    await userEvent.click(screen.getByRole("button", { name: "Принять диалог" }));
    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/transfer/accept"))).toBe(true),
    );
  });

  it("«Отказаться» уходит на сервер", async () => {
    as(TAKER);
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={withOffer()} />);

    await userEvent.click(screen.getByRole("button", { name: "Отказаться" }));
    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/transfer/decline"))).toBe(true),
    );
  });

  it("без предложения полосы нет вовсе", () => {
    as(GIVER);
    renderWithProviders(
      <ThreadFooter convId="conv-1" conversation={makeConversation({ assignee: GIVER })} />,
    );
    expect(screen.queryByText(/Ждёт подтверждения/)).not.toBeInTheDocument();
  });

  it("постороннему видно, что происходит, но кнопок нет", () => {
    // Руководитель смотрит чужой диалог: ему важно понимать, почему диалог
    // числится за одним, а обсуждают его двое.
    as({ id: "u-head", full_name: "Руководитель" });
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={withOffer()} />);

    expect(
      screen.getByText(/Анна Отдающая → Борис Принимающий: ждёт подтверждения/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять диалог" })).not.toBeInTheDocument();
  });

  it("ДИАЛОГ В ОЧЕРЕДИ БЕРЁТСЯ, даже если на нём висит предложение", async () => {
    /*
     * ⚠ БОЕВОЙ ТУПИК 28.08, слова владельца: «я возвращал его во входящие, и он
     * его вернул так, при этом его нельзя было принять и пришлось закрывать».
     *
     * Возврат в очередь снимал ответственного, а предложение оставлял: диалог
     * оказывался разом ничьим и обещанным конкретному человеку. В подвале
     * ветка предложения стояла ВЫШЕ ветки очереди и выигрывала — кнопки
     * «Принять» не было вовсе, и единственным выходом оставалось закрыть
     * диалог руками и искать заново.
     *
     * Корень починен на сервере (`inbox.return_to_queue` снимает предложение),
     * это второй замок: строки, застрявшие до выкатки, живут в базе.
     */
    as(TAKER);
    const застрявший = { ...withOffer(), in_inbox: true };
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={застрявший} />);

    expect(
      await screen.findByRole("button", { name: "Принять диалог" }),
      "диалог в очереди снова неберущийся — придётся закрывать руками",
    ).toBeInTheDocument();
    // Плашка остаётся объяснением, откуда взялось предложение, — но БЕЗ кнопок:
    // принимать предложение не у кого, диалог ничей, а две кнопки с одной
    // подписью рядом означали бы, что человек нажимает наугад.
    expect(
      screen.getByText(/Анна Отдающая → Борис Принимающий: ждёт подтверждения/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отказаться" })).not.toBeInTheDocument();
  });

  it("вне очереди предложение по-прежнему оставляет поле ввода", () => {
    /*
     * Обратная граница. Пока предложение висит, диалог остаётся за передающим,
     * и он обязан иметь возможность ответить клиенту: отними мы у него
     * композер — диалог формально его, а написать нечем.
     */
    as(GIVER);
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={withOffer()} />);
    expect(screen.getByRole("textbox", { name: /Текст сообщения/ })).toBeInTheDocument();
  });
});
