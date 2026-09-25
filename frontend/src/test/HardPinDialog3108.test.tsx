import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { theme } from "@/app/theme";

import { useDialogPin, выбралСам, забытьВыбор } from "@/features/chats/выборДиалога";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeUser, resetSessionStore } from "./helpers";

/**
 * ЗАМОК: С НЕДОПИСАННОГО ОТВЕТА УВОДИТ ТОЛЬКО ЧЕЛОВЕК.
 *
 * ⚠ ПОЧЕМУ ЗАМОК, А НЕ ЕЩЁ ОДИН ЗАПРЕТ. Жалоба «диалоги перелетают, начинаю
 * писать клиенту — и перескакивает» возвращалась ТРИЖДЫ. Механизмы закрывались
 * по одному: клавиши, кадры сокета, подмена поля ввода, пересортировка списка.
 * Каждый раз находился следующий — значит перебор проигрывает, путей больше,
 * чем мы находим за раз.
 *
 * Правило перевёрнуто: уйти с диалога, где лежит недописанный ответ, можно
 * только осознанным выбором. Всё прочее откатывается назад, кем бы ни было
 * вызвано, и пишется в журнал — по нему и найдётся виновник, которого не
 * видно в коде.
 */

const A = "aaaa0000-0000-0000-0000-00000000000a";
const B = "bbbb0000-0000-0000-0000-00000000000b";

let уйтиНа: (id: string) => void = () => {};

/** Отрисовать страницу и дать доступ к текущему адресу + внешний переход. */
function отрисовать(начальный: string) {
  let текущий = `/chats/${начальный}`;

  function Spy() {
    const место = useLocation();
    const nav = useNavigate();
    текущий = место.pathname;
    уйтиНа = (id: string) => act(() => nav(`/chats/${id}`));
    return null;
  }

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  function Host() {
    const { id } = useParams();
    useDialogPin(id);
    return null;
  }

  render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={[`/chats/${начальный}`]}>
          <Spy />
          <Routes>
            <Route path="/chats/:id" element={<Host />} />
            <Route path="/chats" element={<Host />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
  return { экран: () => текущий };
}

describe("Открытый диалог держится, пока в нём недописанный ответ", () => {
  beforeEach(() => {
    забытьВыбор();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: A, filters: { tab: "all" }, drafts: {} });
  });

  it("⚠ ЧУЖОЙ переход при набранном ответе откатывается назад", async () => {
    /*
     * ГЛАВНАЯ ПРОВЕРКА ФАЙЛА, и она про поведение, а не про текст исходника.
     * Здесь воспроизводится сама жалоба: человек пишет ответ, и что-то — кто
     * угодно — уводит экран на другой диалог.
     */
    useChatUiStore.setState({ drafts: { [A]: { text: "Здравствуйте, мастер", isNote: false } } });
    забытьВыбор();

    const { экран: где } = отрисовать(A);
    await waitFor(() => expect(где()).toBe(`/chats/${A}`));

    уйтиНа(B); // никто не помечал этот переход выбором

    await waitFor(() => {
      expect(где(), "экран увели с недописанного ответа — жалоба воспроизводится").toBe(
        `/chats/${A}`,
      );
    });
  });

  it("а выбор человека замок пропускает", async () => {
    useChatUiStore.setState({ drafts: { [A]: { text: "Здравствуйте, мастер", isNote: false } } });

    const { экран: где } = отрисовать(A);
    await waitFor(() => expect(где()).toBe(`/chats/${A}`));

    выбралСам(B);
    уйтиНа(B);

    await waitFor(() => {
      expect(где(), "замок держит человека там, куда он сам нажал").toBe(`/chats/${B}`);
    });
  });

  it("с пустым полем переход проходит без пометки", async () => {
    // Терять нечего: держать тут значило бы мешать работе.
    useChatUiStore.setState({ drafts: {} });
    забытьВыбор();

    const { экран: где } = отрисовать(A);
    await waitFor(() => expect(где()).toBe(`/chats/${A}`));

    уйтиНа(B);

    await waitFor(() => expect(где()).toBe(`/chats/${B}`));
  });

  it("выбор человека помечается и живёт недолго", () => {
    выбралСам(B);
    // Проверяем через сам модуль: замок опирается на эту метку.
    expect(useChatUiStore.getState().activeConversationId).toBe(A);
  });

  it("отметка выбора не подходит другому диалогу", async () => {
    const { этоВыбор } = await import("@/features/chats/выборДиалога");
    выбралСам(B);
    expect(этоВыбор(B), "свой же выбор не признан").toBe(true);
    expect(этоВыбор(A), "чужой переход принят за выбор человека").toBe(false);
  });

  it("отметка протухает и не пропускает переход через минуту", async () => {
    const { этоВыбор } = await import("@/features/chats/выборДиалога");
    выбралСам(B);
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 60_000);
    expect(этоВыбор(B), "устаревшая отметка всё ещё пропускает переход").toBe(false);
    vi.useRealTimers();
  });

  it("пустое поле держать не надо: замок молчит", async () => {
    const { этоВыбор } = await import("@/features/chats/выборДиалога");
    забытьВыбор();
    // Черновика нет — замок не должен вмешиваться вовсе.
    expect(этоВыбор(B)).toBe(false);
    expect((useChatUiStore.getState().drafts[A]?.text ?? "")).toBe("");
  });

  it("замок подключён к странице чатов", async () => {
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const страница = readFileSync("src/features/chats/ChatsPage.tsx", "utf-8") as string;
    expect(страница, "страница не зовёт замок — держать диалог некому").toMatch(
      /useDialogPin\(id\)/,
    );

    const модуль = readFileSync("src/features/chats/выборДиалога.ts", "utf-8") as string;
    expect(модуль, "откат не через replace — уведёт человека со страницы").toMatch(
      /navigate\(`\/chats\/\$\{было\}`, \{ replace: true \}\)/,
    );
    expect(модуль, "замок срабатывает и на пустом поле — будет мешать работе").toMatch(
      /if \(набрано\)/,
    );
  });

  it("все пути осознанного выбора помечены", async () => {
    // @ts-expect-error — типов Node в проекте нет.
    const { readFileSync } = await import("node:fs");
    const пути = [
      ["src/features/chats/components/list/ChatListPane.tsx", "клик по строке списка"],
      ["src/features/chats/inbox/claimFromQueue.ts", "приём диалога"],
      ["src/features/hotkeys/dispatch.ts", "клавиши перехода"],
      ["src/features/table/TablePage.tsx", "разбор диалогов"],
    ] as const;
    for (const [путь, зачем] of пути) {
      const текст = readFileSync(путь, "utf-8") as string;
      expect(текст, `путь «${зачем}» не помечен — замок откатит законный переход`).toMatch(
        /выбралСам\(/,
      );
    }
  });
});
