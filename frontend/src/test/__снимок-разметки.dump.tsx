/**
 * НЕ СТОРОЖ. Снималка настоящей разметки рабочего места для стенда адаптива.
 *
 * ⚠ ЗАЧЕМ ОНА ВООБЩЕ ПОЯВИЛАСЬ (07.09, жалоба владельца «всё едет»). Проверить
 * «ничего не съезжает и не обрезается» тестами нельзя: jsdom раскладку не
 * считает вовсе — у него нет ни ширин, ни переносов, ни overflow. Всё, что
 * умеют сторожа проекта, — это утверждать про значения свойств, то есть про
 * НАМЕРЕНИЕ, а едет как раз результат.
 *
 * Мерить надо в браузере, а браузеру нужны две вещи: настоящие стили и
 * настоящая разметка. Стили лежат в репозитории; разметку рисует React и
 * достать её больше неоткуда — вход в боевое приложение мне недоступен.
 * Отсюда этот файл: он рендерит `ChatsPage` тем же способом, что и соседние
 * экранные тесты, и кладёт получившийся HTML на диск. Дальше стенд открывает
 * его в браузере с настоящими CSS и меряет переполнения на каждой ширине.
 *
 * Расширение `.dump.ts`, а не `.test.ts`: это инструмент, а не проверка, и
 * попадать в общий прогон он не должен. Запуск руками:
 *   npx vitest run --config vitest.dump.config.ts
 */
import { expect, it } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// ставить @types/node ради одной снималки несоразмерно; так же поступают
// соседние сторожа, читающие исходники (mantineStyles, actionBusConsumers).
import { writeFileSync } from "node:fs";
import { Route, Routes } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ChatsPage } from "@/features/chats/ChatsPage";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

/*
 * Путь фиксированный и внутри репозитория: `process.env` здесь недоступен
 * (типов Node в проекте нет, и вся сборка падает на одной этой строке —
 * проверено), а `import.meta.env` отдаёт только переменные с префиксом VITE_.
 * Каталог `.dump/` в .gitignore: снимки — расходный материал стенда.
 */
const ВЫХОД = "./.dump/markup.html";

/**
 * Строки списка повторяют боевой снимок владельца по форме и длине; имена,
 * адреса и номера в них заменены выдуманными той же длины.
 *
 * ⚠ ДЛИННЫЕ ЗНАЧЕНИЯ ВЗЯТЫ НАРОЧНО, И ЭТО СМЫСЛ ВСЕГО НАБОРА. «Бригада Петра
 * Иванова КП» — длиной с настоящее название канала, «Васильцова 7000300» — с
 * настоящее имя клиента, два «Григория Воронцова» подряд различаются только
 * каналом. Стенд, посеянный короткими именами, покажет, что всё влезает, — и
 * соврёт.
 */
const СТРОКИ: Array<{ имя: string; канал: string; текст: string; ждёт: number; непроч: number }> = [
  { имя: "Анатолий МНЧ", канал: "Анатолий МНЧ", текст: "Доброе утро! Во сколько вы сможете подъехать сегодня?", ждёт: 60, непроч: 0 },
  { имя: "Пользователь", канал: "Марк БТ/МНЧ", текст: "1-аяКленовская 12, телефон 8 (900) 000-00-00", ждёт: 1, непроч: 3 },
  { имя: "Марина", канал: "Новый КП", текст: "Первомайская27/2", ждёт: 1, непроч: 2 },
  { имя: "Yan", канал: "Анатолий МНЧ", текст: "Просто что там нужно менять, я не понял", ждёт: 2, непроч: 3 },
  { имя: "Григорий Воронцов", канал: "Никита КП", текст: "ДП Луговское 17", ждёт: 3, непроч: 0 },
  { имя: "Григорий Воронцов", канал: "Артур КП", текст: "Мой телефон 8900111", ждёт: 3, непроч: 2 },
  { имя: "Петя Жуков", канал: "Артур КП", текст: "ладно, тогда не надо, я бы хотел подешевле", ждёт: 5, непроч: 0 },
  { имя: "Светлана Морозова", канал: "Бригада Петра Иванова КП", текст: "Нам удобней самим привезти телевизор", ждёт: 5, непроч: 1 },
  { имя: "Серый", канал: "Никита КП", текст: "Сколько по стоимости выйдет?", ждёт: 6, непроч: 3 },
  { имя: "Васильцова 7000300", канал: "Вячеслав БТ/МНЧ", текст: "Лесная, 118", ждёт: 8, непроч: 3 },
];

function строка(i: number): ConversationDto {
  const с = СТРОКИ[i];
  const минут = с.ждёт;
  const когда = new Date(Date.UTC(2026, 8, 7, 19, 30) - минут * 60_000).toISOString();
  return {
    id: i === 0 ? "conv-1" : `conv-${i + 1}`,
    status: "in_progress",
    channel: "avito",
    account: { id: `acc-${i}`, title: с.канал },
    client: { id: `cl-${i}`, name: с.имя, phone: null, avito_rating: 4.9 },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled / Выезд в день обращения", url: null, price: "360 ₽" },
    last_message: { id: `m-${i}`, body: с.текст, direction: "in", sender_type: "client", created_at: когда },
    unread_count: с.непроч,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: когда,
    waiting_since: когда,
    status_since: когда,
  } as unknown as ConversationDto;
}

const РЕПЛИКИ: MessageDto[] = [
  { id: "m1", direction: "in", sender_type: "client", body: "", attachments: [{ media_id: "img1", kind: "image", name: "photo.jpg", url: "https://placehold.co/420x240/1a1a1a/888?text=POLAR", size: 1024 }], created_at: "2026-09-07T15:48:00Z", status: "delivered" },
  { id: "m2", direction: "in", sender_type: "client", body: "Нет, не Билли просто он выключился, и он работает, все нету подсветки, я фонариком просветила, работает экран, нету подсветки.", attachments: [], created_at: "2026-09-07T15:49:00Z", status: "delivered" },
  { id: "m3", direction: "in", sender_type: "client", body: "Громкость сесть я могу, клацать каналы, нету подсветки в экране.", attachments: [], created_at: "2026-09-07T15:49:30Z", status: "delivered" },
  { id: "m4", direction: "out", sender_type: "operator", sender_name: "Кузнецов Арсений (Чатер)", body: "Подскажите, куда к вам подъехать (квартира, подъезд, этаж) и ваш номер телефона, запишу вас", attachments: [], created_at: "2026-09-07T16:15:00Z", status: "delivered" },
  { id: "m5", direction: "in", sender_type: "client", body: "Вы мне скажите, сколько будет стоить, а то мне тут сказали 10000?", attachments: [], created_at: "2026-09-07T16:16:00Z", status: "delivered" },
  { id: "m6", direction: "out", sender_type: "operator", sender_name: "Кузнецов Арсений (Чатер)", body: "Замена посадатрку у меня идет от 1000 рублей, точнее по запчастям смогу вас на месте сориентировать", attachments: [], created_at: "2026-09-07T16:26:00Z", status: "delivered" },
] as unknown as MessageDto[];

it("снимает разметку рабочего места на диск", () => {
  queryClient.clear();
  resetSessionStore({
    user: fakeUser,
    permissions: ["conversations:read", "messages:send", "conversations:manage", "notes:read", "notes:write"] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  useInboxStore.setState({ ids: {}, escalatedIds: {}, count: 25, escalated: 0, claimedNotice: null } as never);

  const items = СТРОКИ.map((_, i) => строка(i));
  for (const вариант of [
    { tab: "mine" as const },
    { tab: "mine" as const, q: "" },
  ]) {
    queryClient.setQueryData([...CONVERSATIONS_LIST_KEY, вариант], {
      pages: [{ items, page: { limit: 50, offset: 0, total: items.length } }],
      pageParams: [0],
    });
  }
  queryClient.setQueryData(qk.conversations.detail("conv-1"), makeConversation({
    client: { id: "cl-т", name: "Татьяна", phone: null, avito_rating: 4.9 },
    account: { id: "acc-0", title: "Бригада Петра Иванова КП" },
    item: { title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled / Выезд в день обращения", url: "https://avito.ru/x", price: "360 ₽" },
  }));
  queryClient.setQueryData(qk.messages.list("conv-1"), {
    pages: [{ items: РЕПЛИКИ, page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false } }],
    pageParams: [null],
  });

  const { container } = renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );

  /*
   * ⚠ СТРОКИ СПИСКА СНИМАЮТСЯ ОТДЕЛЬНО, И ЭТО НЕ ЛЕНЬ. Список виртуализован
   * (@tanstack/react-virtual), а видимые строки он считает от ИЗМЕРЕННОЙ
   * высоты контейнера — в jsdom она ноль, и в разметке рабочего места строк
   * не оказывается ни одной. Стенд, собранный из такого снимка, показал бы
   * пустую колонку и объявил, что ничего не обрезается.
   *
   * Поэтому строку рисуем компонентом напрямую, десятью настоящими наборами
   * данных, и стенд раскладывает их внутри настоящего контейнера прокрутки.
   * Разметка при этом остаётся той же самой — меняется только, кто её
   * расставил: не виртуализатор, а стенд.
   */
  const строки = renderWithProviders(
    <div className="chat-list">
      {items.map((row, i) => (
        <ConversationListItem
          key={row.id}
          row={row}
          active={i === 0}
          now={Date.UTC(2026, 8, 7, 19, 30)}
          onOpen={() => {}}
          showChannel
        />
      ))}
    </div>,
  );

  writeFileSync(ВЫХОД, container.innerHTML, "utf8");
  writeFileSync(ВЫХОД.replace(/\.html$/, "-rows.html"), строки.container.innerHTML, "utf8");
  // Снималка обязана падать, если разметки не вышло: пустой файл на стенде
  // выглядел бы как «всё влезает», то есть врал бы в самую удобную сторону.
  expect(container.innerHTML.length).toBeGreaterThan(2000);
});
