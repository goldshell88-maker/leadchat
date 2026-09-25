import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { act } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * ПОЛЕ ВВОДА ПОКАЗЫВАЕТ ВЕСЬ ЧЕРНОВИК, А НЕ ПЕРВУЮ СТРОКУ.
 *
 * ЧТО БЫЛО. Автовысота пересчитывалась только при изменении текста, то есть
 * при монтировании и дальше по нажатию клавиши. Всё, что меняет ПЕРЕНОСЫ, не
 * меняя текста, проходило мимо.
 *
 * Как ловилось руками: набрать две строки, не отправлять, перезагрузить
 * страницу (черновик лежит в localStorage). Замеры поля: clientHeight 28,
 * scrollHeight 48, computed overflow-y hidden — видно «…Уточните адрес,», а
 * «пожалуйста.» не видно и прокрутить к нему нельзя.
 *
 * ПРИЧИНА. Шрифт приезжает позже первого кадра: на момент монтирования строка
 * меряется системным шрифтом, а Inter шире (та же фраза — 493px против 580px),
 * и в поле шириной 516px первый вариант умещается в одну строку, второй
 * требует двух. Высота вычислялась по первому и больше не пересчитывалась.
 * Второй такой же случай — изменение ширины поля (окно, оверлей карточки,
 * плашки над полем).
 *
 * В JSDOM раскладки нет, поэтому `scrollHeight` поля отдаёт наш объект: он и
 * означает «сколько места содержимому нужно на самом деле».
 */

const CONTENT = { height: 28, width: 516 };

const ORIGINAL = {
  scrollHeight: Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "scrollHeight"),
  clientWidth: Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "clientWidth"),
};

/** Наблюдатели размеров, созданные компонентом: тест дёргает их вручную. */
const observers: Array<() => void> = [];

function mockTextareaMetrics() {
  Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
    configurable: true,
    get: () => CONTENT.height,
  });
  Object.defineProperty(HTMLTextAreaElement.prototype, "clientWidth", {
    configurable: true,
    get: () => CONTENT.width,
  });
}

function restoreTextareaMetrics() {
  for (const [name, descriptor] of Object.entries(ORIGINAL)) {
    if (descriptor) Object.defineProperty(HTMLTextAreaElement.prototype, name, descriptor);
    else delete (HTMLTextAreaElement.prototype as unknown as Record<string, unknown>)[name];
  }
}

const DRAFT = "Здравствуйте! Да, мастер сможет подъехать сегодня. Уточните адрес, пожалуйста.";

function field() {
  return screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;
}

describe("Автовысота поля ввода", () => {
  let fontsReady!: () => void;
  let originalRO: typeof ResizeObserver;

  beforeEach(() => {
    CONTENT.height = 28;
    CONTENT.width = 516;
    observers.length = 0;
    mockTextareaMetrics();

    // Шрифт «приезжает» тогда, когда мы решим.
    const ready = new Promise<void>((resolve) => {
      fontsReady = () => resolve();
    });
    Object.defineProperty(document, "fonts", { configurable: true, value: { ready } });

    // Заглушка из setup.ts не configurable — подменяем присваиванием.
    originalRO = window.ResizeObserver;
    window.ResizeObserver = class {
      constructor(private cb: () => void) {}
      observe() {
        observers.push(this.cb);
      }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;

    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
    // Черновик уже в сторе — как после перезагрузки вкладки.
    useChatUiStore.setState({
      drafts: { [CONV_ID]: { text: DRAFT, isNote: false } },
      activeConversationId: CONV_ID,
    });
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, makeConversation())));
  });

  afterEach(() => {
    restoreTextareaMetrics();
    window.ResizeObserver = originalRO;
    delete (document as unknown as Record<string, unknown>).fonts;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("дорастает, когда шрифт приезжает после первого кадра", async () => {
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    // Первый кадр: системный шрифт, текст умещается в строку.
    expect(field().style.height).toBe("28px");

    // Приехал Inter — та же фраза стала двумя строками.
    CONTENT.height = 48;
    await act(async () => {
      fontsReady();
      await Promise.resolve();
    });

    await waitFor(() => expect(field().style.height).toBe("48px"));
  });

  it("пересчитывается, когда меняется ширина поля, а не текст", async () => {
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    expect(field().style.height).toBe("28px");

    // Окно сузилось (или открылся оверлей карточки) — тот же текст стал выше.
    CONTENT.width = 300;
    CONTENT.height = 68;
    act(() => observers.forEach((cb) => cb()));

    await waitFor(() => expect(field().style.height).toBe("68px"));
  });

  it("выше шести строк не растёт, но хвост остаётся достижимым прокруткой", async () => {
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    CONTENT.width = 300;
    CONTENT.height = 400; // предел — 6 строк по 20 плюс 8 обвязки = 128
    act(() => observers.forEach((cb) => cb()));

    await waitFor(() => expect(field().style.height).toBe("128px"));
    // Обрезать хвост без возможности до него добраться — это и был дефект.
    expect(field().style.overflowY).toBe("auto");
  });
});
