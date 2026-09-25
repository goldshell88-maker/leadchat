import { describe, expect, it } from "vitest";
import { toConversationDto } from "@/platform/tauri/offline";

/**
 * ОФЛАЙН-СНИМОК ЧИТАЛ ПОЛЯ, КОТОРЫХ В ОТВЕТЕ RUST НЕТ.
 *
 * `cache_load_dialogs` отдаёт `DialogView` с ПЛОСКИМИ колонками — `account_id`,
 * `account_title`, `assignee_name`, `unread`, `client_name`, `item_title`, — а
 * разбор читал `d.account`, `d.client`, `d.assignee`, `d.item`,
 * `d.unread_count`. Ни одного из этих имён в ответе нет, поэтому срабатывали
 * ВСЕ умолчания: на всё время загрузки списка на экране стояли строки «Клиент»
 * без канала, без объявления, без превью и с нулём непрочитанных.
 *
 * Компилятор молчал: тип был объявлен как `Partial<ConversationDto>`, где все
 * поля необязательные, — расхождение всей формы прошло мимо него. Тестов на
 * этот шов не было ни с одной стороны: Rust проверял свою половину
 * (`upsert_and_read_back_decrypted`), фронт мокал мост уже готовыми DTO.
 *
 * Хуже того, у роли «менеджер» офлайн-список не появлялся ВООБЩЕ: у него
 * вкладка по умолчанию «Мои», отбор требует `row.assignee?.id === userId`, а
 * после нормализации `assignee` всегда был `null` — отсеивались все строки, и
 * прогрев молча возвращал «нечего показать».
 *
 * ЧТО ЛОМАЛИ: вернули чтение `d.account`/`d.client`/`d.unread_count` — краснеют
 * первые два теста.
 */

/** Ровно то, что отдаёт `cache::store::DialogView` (сериализация без rename). */
function строкаRust(over: Record<string, unknown> = {}) {
  return {
    id: "conv-1",
    account_id: "acc-7",
    account_title: "LP-Москва",
    status: "in_progress" as const,
    assignee_id: "u-3",
    assignee_name: "Анна Смирнова",
    unread: 4,
    last_message_at: "2026-08-28T10:00:00Z",
    updated_at: "2026-08-28T10:00:00Z",
    client_name: "Иван Петров",
    client_phone: "+79151112233",
    item_title: "Ремонт iPhone 13",
    card: null,
    ...over,
  };
}

describe("Офлайн-снимок списка", () => {
  it("плоские колонки Rust доезжают до элемента списка", () => {
    const dto = toConversationDto(строкаRust());

    expect(dto.account).toEqual({ id: "acc-7", title: "LP-Москва" });
    expect(dto.client.name).toBe("Иван Петров");
    expect(dto.client.phone).toBe("+79151112233");
    expect(dto.item?.title).toBe("Ремонт iPhone 13");
    expect(dto.unread_count).toBe(4);
    expect(dto.status).toBe("in_progress");
  });

  it("ответственный доезжает — иначе у менеджера вкладка «Мои» пуста всегда", () => {
    /*
     * Отбор вкладки «Мои» требует `row.assignee?.id === userId`. Пока
     * `assignee` был всегда `null`, отсеивались ВСЕ строки, и прогрев из кэша у
     * основной рабочей роли не работал ни разу — молча, без ошибки.
     */
    const dto = toConversationDto(строкаRust());
    expect(dto.assignee).toEqual({ id: "u-3", full_name: "Анна Смирнова" });
  });

  it("готовый серверный элемент из `card` побеждает плоские колонки", () => {
    /*
     * `card` — полный ответ `GET /conversations`, сохранённый рядом со строкой
     * ровно для того, «чтобы UI рисовал офлайн-снапшот своим обычным
     * рендерером». Его не читал никто.
     */
    const dto = toConversationDto(
      строкаRust({
        card: {
          id: "conv-1",
          status: "closed",
          channel: "avito",
          account: { id: "acc-7", title: "LP-Москва" },
          client: { id: "cl-1", name: "Иван Петров", phone: null, avito_rating: 4.9 },
          assignee: null,
          item: { title: "Ремонт iPhone 13", url: "https://avito.ru/x", price: "от 1500 ₽" },
          last_message: null,
          unread_count: 0,
          bot_active: false,
          tags: ["негатив"],
          transferred_to_me: false,
          last_message_at: "2026-08-28T10:00:00Z",
        },
      }),
    );

    expect(dto.status).toBe("closed");
    expect(dto.client.avito_rating).toBe(4.9);
    expect(dto.item?.url).toBe("https://avito.ru/x");
    expect(dto.tags).toEqual(["негатив"]);
  });

  it("строка старой версии без `card` и без половины колонок не роняет список", () => {
    const dto = toConversationDto({ id: "conv-2" });
    expect(dto.id).toBe("conv-2");
    expect(dto.status).toBe("new");
    expect(dto.client.name).toBe("Клиент");
    expect(dto.unread_count).toBe(0);
  });
});
