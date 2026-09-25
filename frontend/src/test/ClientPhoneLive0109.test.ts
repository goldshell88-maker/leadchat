import { beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { qk } from "@/shared/api/queryKeys";

vi.mock("@/shared/realtime/notify", () => ({
  notifyNewMessage: vi.fn(),
  notifyAssignedToMe: vi.fn(),
}));

/**
 * ТЕЛЕФОН ПОЯВЛЯЕТСЯ В КАРТОЧКЕ СРАЗУ, А НЕ «КОГДА-НИБУДЬ».
 *
 * ⚠ РАЗБОР ЗАПИСИ ЭКРАНА ОТ ВЛАДЕЛЬЦА 01.09: «не сразу добавился номер».
 *
 * На записи видно ровно это: клиент прислал номер, внизу карточки он появился
 * («Ещё номера этого человека: +7 914 …»), а основное поле осталось с кнопкой
 * «указать телефон». Через несколько секунд номер встал на место сам.
 *
 * ЧТО БЫЛО. Кадр `client:updated` существовал, публиковался и разбирался — но
 * сбрасывал ТОЛЬКО семейство `clients`. А телефон в карточке приезжает не
 * оттуда: он часть ДЕТАЛИ ДИАЛОГА (`conversation.client.phone`). Из семейства
 * `clients` берётся лишь строка «ещё номера» — потому она и обновилась, а поле
 * рядом нет. Половина карточки жила по кадру, половина ждала случая.
 *
 * Замер по базе в тот же час: сервер записал телефон В ТУ ЖЕ СЕКУНДУ, что
 * пришло сообщение. То есть данные были верны с самого начала — молчал экран.
 */
describe("Карточка клиента обновляется по кадру целиком", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  it("⚠ кадр про клиента сбрасывает и деталь диалога, где живёт телефон", () => {
    const convId = "11111111-1111-1111-1111-111111111111";
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

    applyWsEvent({
      type: "client:updated",
      ts: new Date().toISOString(),
      data: { client_id: "c-1", conversation_id: convId, reason: "phone_captured" },
    } as never);

    const ключи = invalidate.mock.calls.map((c) =>
      JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey ?? []),
    );
    expect(
      ключи.some((k) => k === JSON.stringify(qk.conversations.detail(convId))),
      "деталь диалога не перезапрошена — телефон появится в карточке не сразу, а когда повезёт",
    ).toBe(true);
    expect(
      ключи.some((k) => k.includes("clients")),
      "семейство клиентов не перезапрошено — пропадёт строка «ещё номера этого человека»",
    ).toBe(true);

    invalidate.mockRestore();
  });
});
