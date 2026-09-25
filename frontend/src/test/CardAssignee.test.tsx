import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto } from "@/shared/api/types";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * «Ответственный» в карточке НЕ БЫВАЕТ ПУСТЫМ, если ответственный есть
 * (дефект аудита 5).
 *
 * ЧТО БЫЛО. Опции селекта строились ровно из `GET /users/assignable`, а тот
 * отдаёт только тех, КОМУ МОЖНО НАЗНАЧИТЬ. Ответственный оттуда выпадает
 * штатно: сотрудника отключили (деактивация диалоги не переназначает,
 * 01 §3.5), перевели в руководители, или диалог достался служебной учётке.
 * Значение селекта тогда не совпадало ни с одной опцией, и Mantine рисовал
 * ПУСТОЕ ПОЛЕ — при том что скрытый input хранил верный id, а шапка ленты в
 * трёхстах пикселях левее писала «· Администратор LP».
 *
 * ЦЕНА. Руководитель читает «диалог ничей» и переназначает его вслепую —
 * отбирает работу у человека, о котором экран промолчал.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - вернули старое построение опций (только `assignable.data.items`) —
 *    падает «показывает выпавшего ответственного»;
 *  - убрали `disabled: true` у призрачной опции — падает «выпавшего нельзя
 *    выбрать заново»;
 *  - сняли условие `assignable.isSuccess` с пояснения — падает «пока список
 *    не пришёл, карточка молчит о недоступности»;
 *  - показали пояснение всегда — падает «у обычного ответственного пояснения
 *    нет».
 */

/** Ответственный, которого нет и не будет в списке назначаемых. */
const GHOST = { id: "a5500000-0000-4000-8000-000000000001", full_name: "Администратор LP" };
const LIVE = { id: "a5500000-0000-4000-8000-000000000002", full_name: "Локальный админ" };

describe("Карточка клиента — селект «Ответственный»", () => {
  /** Управляемый ответ `GET /users/assignable`: тест решает, когда он придёт. */
  let assignable: { items: Array<{ id: string; full_name: string }> } | "pending";

  beforeEach(() => {
    assignable = { items: [LIVE] };
    queryClient.clear();
    resetSessionStore({
      // Свободный селект — только у admin/head (03 §5.2), у manager вместо него текст.
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "conversations:manage", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/users/assignable")) {
          // «Ещё не ответил» — вечное ожидание, а не пустой список: это разные
          // состояния, и именно на них разное поведение пояснения.
          if (assignable === "pending") return new Promise<Response>(() => {});
          return jsonResponse(200, assignable);
        }
        if (url.includes("client-history")) return jsonResponse(200, { client: {}, items: [] });
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function render(conversation: ConversationDetailDto) {
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conversation);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  /** Обёртка `.card-field` именно ответственного — рядом стоит селект статуса. */
  function assigneeField(container: HTMLElement): HTMLElement | null {
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Ответственный"]');
    return input?.closest<HTMLElement>(".card-field") ?? null;
  }

  /** Видимое поле селекта (в скрытом input лежит id, а спорили мы про глаза). */
  function selectValue(container: HTMLElement): string {
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Ответственный"]');
    return input?.value ?? "<нет селекта>";
  }

  it("показывает выпавшего из списка ответственного, а не пустоту", async () => {
    const { container } = render(makeConversation({ assignee: GHOST }));

    await waitFor(() => expect(selectValue(container)).toBe("Администратор LP"));
    // Скрытое значение и раньше было верным — беда была ровно в показе.
    //
    // Поиск сужен до СВОЕГО поля: над «Ответственным» встал селект «Статус»
    // (docs/38 §5), и общий `input[type=hidden]` по всей карточке стал
    // находить его скрытое значение — то есть тест зеленел бы, проверяя
    // соседний контрол.
    expect(assigneeField(container)?.querySelector<HTMLInputElement>('input[type="hidden"]')?.value).toBe(
      GHOST.id,
    );
  });

  it("выпавшего нельзя выбрать заново: опция выключена", async () => {
    const { container } = render(makeConversation({ assignee: GHOST }));
    await waitFor(() => expect(selectValue(container)).toBe("Администратор LP"));

    const input = container.querySelector<HTMLInputElement>('input[aria-label="Ответственный"]');
    input?.focus();
    input?.click();

    // Сервер на такое назначение отвечает 422 (assignee_inactive /
    // assignee_cannot_chat), поэтому единственным следствием выбора был бы
    // красный тост.
    const option = await screen.findByRole("option", { name: "Администратор LP" });
    expect(option).toHaveAttribute("data-combobox-disabled", "true");
  });

  it("объясняет, почему этого сотрудника не выбрать", async () => {
    render(makeConversation({ assignee: GHOST }));

    expect(
      await screen.findByText(/больше нет среди назначаемых — выбрать его заново нельзя/),
    ).toBeInTheDocument();
  });

  it("пока список не пришёл, о недоступности молчит", async () => {
    assignable = "pending";
    const { container } = render(makeConversation({ assignee: GHOST }));

    // Имя показываем сразу: оно есть в самом диалоге и ждать его незачем.
    await waitFor(() => expect(selectValue(container)).toBe("Администратор LP"));
    // А вот «его нет среди назначаемых» — это ещё не факт, а незнание.
    expect(screen.queryByText(/больше нет среди назначаемых/)).toBeNull();
  });

  it("у обычного ответственного пояснения нет", async () => {
    const { container } = render(makeConversation({ assignee: LIVE }));

    await waitFor(() => expect(selectValue(container)).toBe("Локальный админ"));
    expect(screen.queryByText(/больше нет среди назначаемых/)).toBeNull();
  });
});
