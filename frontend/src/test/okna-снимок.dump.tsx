/**
 * НЕ СТОРОЖ. Снималка разметки МОДАЛЬНЫХ ОКОН для стенда адаптива.
 *
 * Довод тот же, что у `nastroyki-снимок.dump.tsx`: jsdom раскладку не считает
 * вовсе, а сторожа проекта утверждают про НАМЕРЕНИЕ (какое свойство
 * выставлено), тогда как едет РЕЗУЛЬТАТ. Разметку снимаем React'ом, а меряем
 * её в настоящем браузере с настоящим (собранным) CSS.
 *
 * ⚠ ОКНА РИСУЮТСЯ В ПОРТАЛ, ТО ЕСТЬ НЕ В КОНТЕЙНЕР RTL. Поэтому снимается
 * `document.body.innerHTML` целиком: там и колонка работы, и портал окна
 * рядом с ней. Место работы делаем СВОИМ контейнером с классом `lc-main` —
 * ровно так, как в бою (`AppLayout`), иначе окно, нарисованное БЕЗ портала
 * (`StepCard`), на стенде оказалось бы в другом окружении, чем у человека.
 *
 * ⚠ У ОКОН СВОЯ БЕДА, КОТОРОЙ НЕТ У СТРАНИЦ: они центрируются по высоте и
 * могут оказаться ВЫШЕ экрана. Меряет это стенд (`.dump/мерка-окон.html`), а
 * здесь — только снимок.
 *
 * Запуск руками:
 *   npx vitest run --config vitest.dump.config.ts --reporter=basic
 */
import { expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// так же поступают соседние сторожа, читающие исходники.
import { writeFileSync } from "node:fs";
import { cleanup, screen, waitFor } from "@testing-library/react";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { ConnectChannelWizard } from "@/features/settings/accounts/ConnectChannelWizard";
import { AssignOperatorsModal } from "@/features/settings/accounts/AssignOperatorsModal";
import { InviteModal } from "@/features/settings/team/InviteModal";
import { OneTimeLinkModal } from "@/features/settings/team/OneTimeLinkModal";
import { HotkeysModal } from "@/features/hotkeys/HotkeysModal";
import { TransferDialog } from "@/features/chats/components/card/TransferDialog";
import { InviteDialog } from "@/features/chats/components/card/InviteDialog";
import { BlockClientDialog } from "@/features/chats/components/card/BlockClientButton";
import { TemplateEditor } from "@/features/templates/TemplatesManager";
import { ImageViewer } from "@/features/chats/components/thread/ImageViewer";
import { TeamPage } from "@/features/settings/team/TeamPage";
import { SettingsLayout } from "@/features/settings/SettingsLayout";
import { Route, Routes } from "react-router-dom";
import type { AvitoAccountDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore, включитьВсеСочетания } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";

const КАТАЛОГ = "./.dump";

const ВСЕ_ПРАВА = [
  "conversations:read", "messages:send", "conversations:manage",
  "notes:read", "notes:write", "templates:own", "templates:shared",
  "stats:own", "stats:all", "accounts:read", "accounts:manage",
  "bots:manage", "settings:manage", "users:manage", "audit:read",
];

/*
 * ⚠ ДЛИННЫЕ ЗНАЧЕНИЯ ВЗЯТЫ НАРОЧНО — в этом весь смысл стенда. «Бригада
 * Андрея Владиславовича КП · Б6» — длиной с настоящее название отдела (37
 * знаков), почта партнёрского домена — 43 знака, адрес чужой подписки Авито —
 * 78. Имена и номера выдуманы, длины — боевые. Стенд, посеянный короткими
 * строками, покажет, что всё влезает, и соврёт.
 */
const ЛЮДИ = [
  { id: "u-1", full_name: "Константинопольский Вячеслав", email: "konstantinopolskiy@partner-lead-centre.ru", role: "admin", department: "Бригада Андрея Владиславовича КП · Б6" },
  { id: "u-2", full_name: "Анна Смирнова", email: "anna.smirnova@partner-lead-centre.ru", role: "head", department: "Диспетчерская МНЧ · смена 2" },
  { id: "u-3", full_name: "Кузнецов Арсений (Чатер)", email: "kuznetsov.arseniy.chater@partner-lead-centre.ru", role: "manager", department: "Бригада Петра Иванова КП" },
  { id: "u-4", full_name: "Дмитрий Соколов", email: "d.sokolov@partner-lead-centre.ru", role: "manager", department: null },
  { id: "u-5", full_name: "Марина Егорова", email: "m.egorova@partner-lead-centre.ru", role: "observer", department: "Наблюдение · Санкт-Петербург" },
  { id: "u-6", full_name: "Григорий Воронцов", email: "vorontsov.grigoriy@partner-lead-centre.ru", role: "manager", department: "Бригада Артура БТ/МНЧ" },
];

const КАНДИДАТЫ = {
  assigned_ids: ["u-1", "u-3", "u-5"],
  candidates: ЛЮДИ.map((ч) => ({
    id: ч.id,
    full_name: ч.full_name,
    role: ч.role,
    department: ч.department,
    can_be_operator: ч.role !== "observer" && ч.role !== "head",
    reason: ч.role === "observer" ? "роль не отвечает клиентам" : ч.role === "head" ? "руководитель не ведёт переписку" : null,
  })),
};

const НАЗНАЧАЕМЫЕ = {
  items: ЛЮДИ.slice(0, 4).map((ч, i) => ({
    id: ч.id,
    full_name: ч.full_name,
    role: ч.role,
    department: ч.department,
    is_online: i !== 3,
    presence: i === 1 ? "away" : "active",
    color: null,
  })),
};

const КАНАЛ: AvitoAccountDto = {
  id: "acc-1",
  title: "Вячеслав БТ/МНЧ · Санкт-Петербург",
  avito_user_id: 100200300,
  status: "active",
} as unknown as AvitoAccountDto;

const КОМАНДА = ЛЮДИ.map((ч, i) => ({
  ...ч,
  color: null,
  is_online: i % 2 === 0,
  is_active: true,
  invite_pending: i === 4,
  handles_conversations: ч.role !== "observer",
  presence: i === 1 ? "away" : "active",
  created_at: "2026-08-01T08:00:00Z",
}));

/*
 * Куда смотрит система: имитатор или настоящий Авито. От этого зависит вид
 * мастера — при имитаторе он показывает жёлтое предупреждение и запирает
 * кнопку, при настоящем даёт подключить и упереться в чужую подписку. Оба
 * вида снимаются: они разной высоты, а мерить надо оба.
 */
let боевойАвито = false;

/** Сеть на все окна разом: путей немного, восемь копий стаба забыли бы одно и то же. */
function поднятьСеть() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const p = url.pathname;
      const method = init?.method ?? "GET";

      if (p.endsWith("/avito-accounts/connect")) {
        /*
         * 409 «канал занят» — не выдумка стенда, а боевой ответ Авито: на
         * аккаунт держится ровно одна подписка. Именно этот вид окна самый
         * высокий (два предупреждения плюс список чужих адресов), и мерить
         * надо его, а не пустую форму.
         */
        return jsonResponse(409, {
          error: {
            code: "conflict",
            message: "Подтвердите, что канал нужно забрать",
            details: {
              reason: "subscription_taken",
              subscriptions: [
                "https://api.jivosite.com/webhook/avito/100200300/incoming-messages?token=8f3a19c2111122223333444455556666",
                "https://hooks.partner-lead-centre.ru/api/v1/webhooks/avito/subscription/100200300",
              ],
            },
          },
        });
      }
      if (p.endsWith("/avito/app")) {
        return jsonResponse(200, { api_base: "https://api.avito.ru", auth_url: "https://api.avito.ru/token", live: боевойАвито, client_id: "", secret_set: false });
      }
      if (p.includes("/avito-accounts/") && p.endsWith("/operators")) return jsonResponse(200, КАНДИДАТЫ);
      if (p.endsWith("/users/assignable")) return jsonResponse(200, НАЗНАЧАЕМЫЕ);
      if (p.endsWith("/templates/folders")) {
        return jsonResponse(200, { items: ["Первый контакт", "Цены", "Возражения и отказы", "Сложные случаи БТ/МНЧ"] });
      }
      if (p.endsWith("/users")) return jsonResponse(200, { items: КОМАНДА, page: { limit: 50, offset: 0, total: КОМАНДА.length } });
      if (p.endsWith("/audit-log")) return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      if (method !== "GET") return jsonResponse(200, {});
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    }),
  );
}

/**
 * Рисует в СВОЁ место с классом `lc-main` — тем самым, что в бою объявляет
 * контейнер `lc-workspace`. Портал окна ложится рядом, в `body`, как в бою.
 */
function место() {
  const узел = document.createElement("div");
  узел.className = "lc-main";
  document.body.appendChild(узел);
  return узел;
}

let текущееМесто: HTMLElement;

function нарисовать(ui: React.ReactNode, route = "/chats") {
  текущееМесто = место();
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
    { container: текущееМесто },
  );
}

/*
 * ⚠ ПОРТАЛ СНИМАЕТСЯ ОТДЕЛЬНО ОТ МЕСТА РАБОТЫ, И ЭТО НЕ АККУРАТНОСТЬ РАДИ
 * АККУРАТНОСТИ. Внутренний слой окна Mantine (`m_60c222c7`) объявлен
 * `position: fixed; top: 0; bottom: 0; width: 100%` — БЕЗ `left`. У fixed без
 * `left`/`right` горизонталь берётся от СТАТИЧЕСКОГО положения узла. В бою
 * портал — прямой ребёнок `body`, и это ноль; сложи стенд иначе (портал внутри
 * колонки работы) — и окно уедет вправо на ширину рельса, а стенд объявит
 * бедой то, чего в бою нет. Замер на стенде первой сборки: окно вставало в
 * left 409.5 при экране 390.
 *
 * Отсюда же следует, что ширина рельса модальному окну ничего не меняет:
 * проверено обеими рельсами, числа совпадают до пикселя.
 */
function снять(имя: string, место: HTMLElement) {
  const внутри = место.innerHTML;
  const портал = [...document.body.children]
    .filter((узел) => узел !== место)
    .map((узел) => узел.outerHTML)
    .join("");
  writeFileSync(`${КАТАЛОГ}/okno-${имя}-mesto.html`, внутри, "utf8");
  writeFileSync(`${КАТАЛОГ}/okno-${имя}.html`, портал, "utf8");
  /*
   * Пустой снимок на стенде выглядел бы как «всё влезает», то есть соврал бы
   * в самую удобную сторону.
   *
   * Окно ищем в ОБЕИХ половинах: просмотрщик снимка рисуется прямо в ленте
   * (`inset: 0` считается от окна независимо от места в дереве), и требование
   * «окно обязано быть в портале» отвергло бы верный снимок.
   */
  expect(внутри.length + портал.length, `${имя}: снимок пуст`).toBeGreaterThan(2000);
  expect(внутри + портал, `${имя}: в снимке нет окна`).toMatch(
    /mantine-Modal-content|mantine-Drawer-content|imgview/,
  );
}

function подготовить() {
  queryClient.clear();
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ВСЕ_ПРАВА as never,
    accessToken: "t",
    bootstrapped: true,
  });
  поднятьСеть();
}

it("снимает мастер подключения канала — вид при имитаторе", async () => {
  боевойАвито = false;
  подготовить();
  нарисовать(<ConnectChannelWizard opened onClose={() => {}} />, "/settings/accounts");
  await screen.findByLabelText("Client ID");
  await screen.findByText("Система смотрит на встроенный имитатор");
  снять("wizard", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает мастер подключения канала — вопрос о чужой подписке", async () => {
  боевойАвито = true;
  подготовить();
  const user = userEvent.setup();
  нарисовать(<ConnectChannelWizard opened onClose={() => {}} />, "/settings/accounts");
  await screen.findByLabelText("Client ID");
  await user.type(screen.getByLabelText("Client ID"), "avito-client-1");
  await user.type(screen.getByLabelText("Client Secret"), "secret-1");
  await user.click(screen.getByRole("button", { name: "Подключить" }));
  await screen.findByText("Канал уже кем-то занят");
  снять("wizard-perehvat", текущееМесто);
  боевойАвито = false;
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает «Назначить операторов»", async () => {
  подготовить();
  нарисовать(<AssignOperatorsModal account={КАНАЛ} onClose={() => {}} />, "/settings/accounts");
  await waitFor(() => expect(document.querySelector(".channel-operator")).toBeTruthy());
  снять("assign", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает приглашение в команду", async () => {
  подготовить();
  нарисовать(<InviteModal opened onClose={() => {}} onIssued={() => {}} />, "/settings/team");
  await screen.findByText("Пригласить сотрудника");
  снять("invite-team", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает окно разовой ссылки", async () => {
  подготовить();
  нарисовать(
    <OneTimeLinkModal
      opened
      title="Приглашение для Константинопольского Вячеслава"
      /* Настоящая длина боевой ссылки: домен плюс подписанный токен. */
      url="https://leadchat.partner-lead-centre.ru/set-password?token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI3ZjNhMTljMi0xMTExLTIyMjItMzMzMy00NDQ0NTU1NTY2NjYiLCJleHAiOjE3NzgxMjM0NTZ9"
      onClose={() => {}}
    />,
    "/settings/team",
  );
  await screen.findByTestId("invite-link");
  снять("link", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает справку по горячим клавишам", async () => {
  подготовить();
  включитьВсеСочетания();
  нарисовать(<HotkeysModal opened onClose={() => {}} />);
  await screen.findByText("Горячие клавиши");
  снять("hotkeys", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает окно передачи диалога", async () => {
  подготовить();
  нарисовать(<TransferDialog convId="conv-1" opened currentAssigneeId={null} onClose={() => {}} />);
  await screen.findByText("Передать диалог");
  await waitFor(() => expect(document.body.textContent).toContain("Константинопольский"));
  снять("transfer", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает окно «Позвать в диалог»", async () => {
  подготовить();
  нарисовать(<InviteDialog convId="conv-1" opened currentAssigneeId={null} alreadyIn={[]} onClose={() => {}} />);
  await screen.findByText("Позвать в диалог");
  await waitFor(() => expect(document.body.textContent).toContain("Константинопольский"));
  снять("pozvat", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает окно «Пометить клиента»", async () => {
  подготовить();
  нарисовать(<BlockClientDialog clientId="cl-1" convId="conv-1" blocked={false} opened onClose={() => {}} />);
  await screen.findByText("Пометить клиента");
  снять("pometit", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает редактор быстрого ответа", async () => {
  подготовить();
  нарисовать(
    <TemplateEditor
      state={{
        editingId: "t-2",
        initial: {
          title: "Цена выезда мастера по телевизорам и медиаприставкам",
          body:
            "Здравствуйте! Выезд мастера 500 ₽, диагностика бесплатно при согласии на ремонт.\n" +
            "Замена подсветки от 1000 рублей, точнее по запчастям смогу сориентировать на месте.\n" +
            "Подскажите, куда подъехать (адрес, квартира, подъезд, этаж) и ваш номер телефона — запишу вас.",
          folder: "Цены",
        },
        shared: true,
      }}
      folders={["Первый контакт", "Цены", "Возражения и отказы"]}
      onClose={() => {}}
    />,
    "/settings/templates",
  );
  await screen.findByText("Редактировать быстрый ответ");
  снять("shablon", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает просмотрщик снимка", async () => {
  подготовить();
  нарисовать(
    <ImageViewer
      images={[
        {
          url: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
          name: "IMG_20260907_экран_телевизора_с_полосами_подсветки.jpg",
        },
        { url: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7", name: "второй.jpg" },
      ] as never}
      index={0}
      onClose={() => {}}
      onIndex={() => {}}
    />,
  );
  await waitFor(() => expect(document.querySelector(".imgview")).toBeTruthy());
  снять("snimok", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает песочницу бота (ящик справа)", async () => {
  queryClient.clear();
  resetBotDraft();
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ADMIN_PERMISSIONS,
    accessToken: "t",
    bootstrapped: true,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
      if (url.pathname === `/api/v1/bots/${BOT_ID}`) return jsonResponse(200, botDetail());
      if (url.pathname.endsWith("/sandbox/start")) {
        return jsonResponse(200, { session_id: "sess-1", events: [], state: null });
      }
      if (url.pathname.includes("/sandbox/") && url.pathname.endsWith("/message")) {
        return jsonResponse(200, {
          events: [
            { kind: "bot_message", text: "Здравствуйте, Иван! Это сервис Lead Partner. Подскажите, что случилось с техникой — телевизор, приставка или что-то ещё?" },
            { kind: "step", id: "ask_problem", type: "ask" },
            { kind: "waiting", var: "problem", deadline: "через 24 ч" },
          ],
          state: {
            step: "ask_problem",
            waiting: { kind: "ask", var: "problem", deadline: "через 24 ч" },
            /* Адрес одним куском — то, чем песочницу и проверяют. */
            vars: { problem: "разбил экран айфона", адрес: "Санкт-Петербург,Черёмуховаяаллея,дом12корпус3квартира146" },
            counters: { steps_total: 2, bot_msgs_row: 1, offscript_msgs: 0, ai_calls: 0 },
          },
        });
      }
      if (url.pathname.startsWith("/api/v1/bots/sandbox")) return jsonResponse(204, null);
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    }),
  );
  const user = userEvent.setup();
  текущееМесто = место();
  renderBotEditor({ container: текущееМесто });
  await screen.findByRole("textbox", { name: "Имя бота" });
  await user.click(screen.getByRole("button", { name: "Протестировать" }));
  const поле = await screen.findByRole("textbox", { name: "Ответ клиента" });
  await user.type(поле, "разбил экран айфона");
  await user.click(screen.getByRole("button", { name: "Отправить ответ клиента" }));
  await waitFor(() => expect(document.querySelector(".sandbox-state")?.textContent).toContain("ask_problem"));
  снять("sandbox", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});

it("снимает окна вкладки «Команда»: правка сотрудника и пароль", async () => {
  подготовить();
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/settings/team"]}>
          <Routes>
            <Route path="/settings" element={<SettingsLayout />}>
              <Route path="team" element={<TeamPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
    { container: (текущееМесто = место()) },
  );
  await waitFor(() => expect(document.querySelector(".team-members__table")).toBeTruthy());
  const меню = document.querySelectorAll<HTMLElement>('[aria-label^="Действия"]');
  expect(меню.length, "не нашли меню строки сотрудника").toBeGreaterThan(0);
  await user.click(меню[0]);
  await user.click(await screen.findByText("Изменить имя и почту"));
  await screen.findByText("Изменить сотрудника");
  снять("team-edit", текущееМесто);
  cleanup();
  vi.unstubAllGlobals();
});
