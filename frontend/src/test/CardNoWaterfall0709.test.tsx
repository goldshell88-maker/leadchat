/**
 * КАРТОЧКА КЛИЕНТА НЕ ЖДЁТ ДЕТАЛЬ ДИАЛОГА — НИ ЧТОБЫ РИСОВАТЬ, НИ ЧТОБЫ СПРАШИВАТЬ.
 *
 * ⚠ ЧТО БЫЛО (аудит 06.09). Правая колонка целиком висела на
 * `GET /conversations/{id}`, и из него же брался `client.id`, по которому
 * спрашивается личность клиента. Получался водопад из двух кругов подряд:
 * сначала деталь, потом `GET /clients/{id}/identity` — 5 047 вызовов за восемь
 * часов боя, p50 21 мс сервера при RTT офиса около 120 мс. Всё это время
 * человек смотрел на скелет из трёх полос, хотя строка списка, по которой он
 * только что щёлкнул, несёт и имя, и телефон, и `client.id`.
 *
 * СТОРОЖИМ ТРИ ВЕЩИ:
 *  1) карточка рисует клиента из строки списка, пока деталь ещё едет;
 *  2) личность спрашивается в тот же миг, а не вторым кругом;
 *  3) того, чего в строке нет (счётчик обращений, позванные), карточка НЕ
 *     выдумывает — эти места ждут детали и молчат.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { предзагрузитьДиалог } from "@/features/chats/предзагрузка";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const CONV = "conv-77";
const CLIENT = "client-77";
const ИМЯ = "Ольга Никитина";

function строка(): ConversationDto {
  return {
    id: CONV,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: CLIENT, name: ИМЯ, phone: "+79141234567", avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт телевизора Samsung", url: null, price: null },
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-07T10:00:00Z",
  } as ConversationDto;
}

let запросы: string[] = [];

async function подождать(мс: number): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, мс));
  });
}

/** Строка списка в кэше — то, что человек уже видел глазами до щелчка. */
function засеятьСтроку(): void {
  queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
    pages: [{ items: [строка()], page: { limit: 50, offset: 0, total: 1 } }],
    pageParams: [0],
  });
}

function войти(permissions: Permission[]): void {
  resetSessionStore({ user: fakeUser, permissions, accessToken: "t", bootstrapped: true });
}

describe("карточка клиента без водопада (07.09)", () => {
  beforeEach(() => {
    queryClient.clear();
    запросы = [];
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, activeConversationId: null });
    войти(fakeMe.permissions as Permission[]);
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        const адрес = String(url);
        запросы.push(адрес);
        /*
         * ⚠ ДЕТАЛЬ НЕ ПРИЕЗЖАЕТ НИКОГДА, И ЭТО СУТЬ ПРОВЕРКИ. Ответь она хоть
         * через миллисекунду — карточка дорисовалась бы по ней, и сторож
         * зеленел бы даже у прежнего кода, ждущего деталь.
         */
        if (/\/conversations\/[^/?]+$/.test(адрес)) return new Promise<Response>(() => {});
        const тело = адрес.includes("/identity")
          ? {
              id: CLIENT,
              name: ИМЯ,
              phone: "+79141234567",
              phone_manual: false,
              phone_source: "dialog",
              phone_candidates: [],
              external_id: "u2i-77",
              phones: [],
              avito_ids: [],
              merged_from: [],
              merged_into: null,
            }
          : { items: [] };
        return Promise.resolve(
          new Response(JSON.stringify(тело), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  /**
   * ДИВЕРСИЯ: в `ClientCardPane` вернуть `const head = conversation;` (то есть
   * снять запасной путь через `cachedConversationRow`) — проверка краснеет: на
   * экране скелет `.card-skeleton`, имени клиента нет.
   */
  it("рисует клиента из строки списка, пока деталь ещё едет", async () => {
    засеятьСтроку();
    const { container } = renderWithProviders(<ClientCardPane convId={CONV} />);
    await подождать(30);

    expect(container.querySelector(".card-skeleton")).toBeNull();
    expect(screen.getByText(ИМЯ)).toBeTruthy();
    // Объявление тоже приезжает в строке — блок «Заявка» ждать деталь не должен.
    expect(screen.getByText("Ремонт телевизора Samsung")).toBeTruthy();
  });

  /**
   * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА: без строки в кэше карточка ОБЯЗАНА показать
   * скелет. Иначе проверка выше зеленела бы и от карточки, которая скелет не
   * рисует вовсе.
   */
  it("без строки в кэше скелет на месте — проверка выше не зеленеет впустую", async () => {
    const { container } = renderWithProviders(<ClientCardPane convId={CONV} />);
    await подождать(30);

    expect(container.querySelector(".card-skeleton")).not.toBeNull();
    expect(screen.queryByText(ИМЯ)).toBeNull();
  });

  /**
   * ВОДОПАД СНЯТ: `client.id` берётся из строки, и личность уходит на сервер, не
   * дожидаясь детали, которая тут вообще не приедет.
   *
   * ДИВЕРСИЯ: в `ClientCardPane` вернуть `const clientId = conversation?.client.id;`
   * — проверка краснеет: за личностью никто не идёт, пока деталь не приехала.
   */
  it("личность клиента спрашивается, не дожидаясь детали", async () => {
    засеятьСтроку();
    renderWithProviders(<ClientCardPane convId={CONV} />);
    await подождать(30);

    expect(запросы.filter((u) => u.includes(`/clients/${CLIENT}/identity`)).length).toBe(1);
  });

  /**
   * ЧЕГО В СТРОКЕ НЕТ — ТОГО КАРТОЧКА НЕ ВЫДУМЫВАЕТ. Счётчик прошлых обращений
   * живёт только в детали, и чип «Впервые у нас» до её приезда был бы неправдой
   * в половине случаев (дефект SCEN-15: клиент вернулся в тот же чат).
   */
  it("«Впервые у нас» не говорится по одной строке списка", async () => {
    засеятьСтроку();
    renderWithProviders(<ClientCardPane convId={CONV} />);
    await подождать(30);

    expect(screen.queryByText("Впервые у нас")).toBeNull();
  });

  /**
   * ПРЕДЗАГРУЗКА ЛИЧНОСТИ СПРАШИВАЕТ ТО ЖЕ ПРАВО, ЧТО И КАРТОЧКА. Ручка требует
   * `conversations:manage` на сервере: наблюдателю она вернёт 403, а увидеть он
   * там всё равно нечего.
   *
   * ДИВЕРСИЯ: снять в `предзагрузитьЛичность` проверку прав — проверка
   * краснеет: наблюдатель шлёт запрос за личностью на каждое наведение.
   */
  it("наблюдателю личность не предзагружается", async () => {
    засеятьСтроку();
    войти(["conversations:read", "notes:read"]);

    предзагрузитьДиалог(CONV);
    await подождать(30);

    expect(запросы.filter((u) => u.includes("/identity"))).toEqual([]);
  });

  /**
   * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА К ПРЕДЫДУЩЕЙ: с правом та же предзагрузка обязана
   * сходить за личностью — иначе «наблюдателю не шлём» зеленело бы и от кода,
   * который не шлёт никому.
   */
  it("с правом предзагрузка личность спрашивает", async () => {
    засеятьСтроку();

    предзагрузитьДиалог(CONV);
    await подождать(30);

    expect(запросы.filter((u) => u.includes(`/clients/${CLIENT}/identity`)).length).toBe(1);
  });
});
