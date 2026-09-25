import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import type { Permission } from "@/shared/auth/usePermissions";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders } from "./render";

/**
 * СТОРОЖ ТРЕТЬЕГО СОСТОЯНИЯ НИЗА ПАНЕЛИ: деталь диалога НЕ ЗАГРУЗИЛАСЬ.
 *
 * Заведён 14 августа по замеру боя. Открываем `/chats/not-a-uuid`, деталь
 * падает с 400 — и внизу стоит РАБОЧЕЕ поле ввода: текст набирается, кнопка
 * «Отправить» живая, нажатие рисует пузырь сообщения, сервер отвечает отказом,
 * текст возвращается в поле. Ни одного слова о том, что писать некуда.
 *
 * Корень был в том, что «данных ещё нет» и «данных уже не будет» выглядели
 * одинаково — `conversation === undefined`, — и низ панели читал это как
 * загрузку. Шапка ленты ту же развилку делала честно.
 *
 * ⚠ ПОЧЕМУ СТОРОЖ НУЖЕН ИМЕННО ЗДЕСЬ. Deep-link на битый uuid теперь уводит к
 * списку сам (`ChatThreadPane`, 400/404/410), и этим сценарием до композера
 * больше не добраться. Но 403 и 500 никуда не делись, а починка, которую никто
 * не проверяет, снимается первой же перекройкой файла.
 */
const ALL: Permission[] = ["conversations:read", "messages:send", "notes:write", "templates:own"];

describe("Низ панели при недоступной детали диалога", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    resetSessionStore({ user: fakeUser, permissions: ALL, accessToken: "t", bootstrapped: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
    );
  });

  it("деталь не загрузилась — вместо композера плашка, писать нечем", () => {
    const { container } = renderWithProviders(
      <ThreadFooter convId={CONV_ID} conversation={undefined} detailFailed />,
    );

    expect(screen.getByText("Диалог недоступен — писать некуда.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Текст сообщения")).not.toBeInTheDocument();
    expect(container.querySelector("textarea")).toBeNull();
    expect(screen.queryByRole("button", { name: "Отправить" })).not.toBeInTheDocument();
  });

  it("деталь ещё грузится — это НЕ отказ, и плашки быть не должно", () => {
    // Разница между двумя состояниями и есть вся починка: покажи мы плашку на
    // время загрузки — оператор при каждом открытии диалога видел бы «писать
    // некуда» на долю секунды и учился бы этой надписи не верить.
    renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={undefined} />);

    expect(screen.queryByText("Диалог недоступен — писать некуда.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Текст сообщения")).toBeInTheDocument();
  });
});
