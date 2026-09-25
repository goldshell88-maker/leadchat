import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import { TransferBar } from "@/features/chats/components/thread/TransferBar";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * НИЖНЯЯ ПЛАШКА — ЕДИНСТВЕННОЕ МЕСТО ПЕРВИЧНОГО ДЕЙСТВИЯ.
 *
 * ЧТО БЫЛО (разбор живого экрана владельцем, 12 августа). «Принять» диалог
 * предлагалось в трёх местах сразу: нижней плашкой, кнопкой «Взять в работу» в
 * правой карточке и селектом «Статус» там же. Три способа сделать одно и то же
 * — это не свобода, а лишний выбор перед каждым диалогом; хуже того, названия
 * у них расходились, и «Принять диалог» с «Взять в работу» выглядели как два
 * разных действия.
 *
 * Здесь сторожатся две вещи, обе про центральную колонку:
 *  1. пока диалог ждёт решения — внизу «Принять диалог» и НЕТ поля ввода;
 *     как только принят — плашка ПРЕВРАЩАЕТСЯ в поле ввода;
 *  2. слово одно и то же везде, где берут ответственность за диалог.
 */
describe("Низ панели: плашка решения превращается в поле ввода", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    useInboxStore.getState().clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, {})));
  });

  afterEach(() => {
    useInboxStore.getState().clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("диалог ждёт решения — есть «Принять диалог» и НЕТ поля ввода", () => {
    // Признак очереди — слово сервера (`in_inbox`), а не догадка по статусу:
    // диалоги, заведённые до очереди, выглядят так же и обязаны вести себя
    // как раньше — с полем ввода на месте.
    renderWithProviders(
      <ThreadFooter
        convId={CONV_ID}
        conversation={makeConversation({ in_inbox: true, assignee: null } as never)}
      />,
    );

    expect(screen.getByRole("button", { name: "Принять диалог" })).toBeInTheDocument();
    // Поля ввода нет намеренно: пока диалог не принят, писать в него нельзя.
    expect(screen.queryByLabelText("Текст сообщения")).not.toBeInTheDocument();
  });

  it("диалог принят — на том же месте поле ввода, и «Принять» больше нигде", () => {
    renderWithProviders(
      <ThreadFooter
        convId={CONV_ID}
        conversation={makeConversation({ in_inbox: false } as never)}
      />,
    );

    expect(screen.getByLabelText("Текст сообщения")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Принять/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Взять в работу/ })).not.toBeInTheDocument();
  });

  it("«Взять в работу» в центральной колонке не встречается ни разу", () => {
    /*
     * ОДНО ДЕЙСТВИЕ — ОДНО НАЗВАНИЕ. Выбрано «Принять диалог», потому что
     * этими словами отвечает сама система: запись в ленте «Диалог принят:
     * Имя», всплывающее «Диалог принят», шпаргалка «Ctrl+R — принять диалог».
     * Название кнопки обязано совпадать с тем, что человек увидит после
     * нажатия, иначе он не свяжет одно с другим.
     */
    const { container } = renderWithProviders(
      <ThreadFooter
        convId={CONV_ID}
        conversation={makeConversation({ in_inbox: true, assignee: null } as never)}
      />,
    );
    expect(container.textContent).not.toContain("Взять в работу");
  });

  it("предложение передачи называет взятие ответственности теми же словами", () => {
    // Здесь кнопка называлась просто «Принять» — третье имя одного действия.
    renderWithProviders(
      <TransferBar
        conversation={
          makeConversation({
            transfer: {
              to: { id: fakeUser.id, full_name: fakeUser.full_name },
              by: { id: "u-2", full_name: "Борис Гущин" },
              comment: null,
            },
          } as never)
        }
      />,
    );

    expect(screen.getByRole("button", { name: "Принять диалог" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять" })).not.toBeInTheDocument();
  });
});
