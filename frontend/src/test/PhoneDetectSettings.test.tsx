import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * «Телефоны в переписке» — два переключателя на экране «Аккаунты Авито».
 *
 * ЗАЧЕМ ОНИ ЕСТЬ. Разбор находит номер, написанный внутри обычной фразы, и это
 * работа с ЧУЖИМИ данными: система решает за клиента, что вот эта цепочка цифр
 * и есть его телефон. Владельцу нужна возможность остановить это одним
 * щелчком, не дожидаясь выкатки, — до сих пор оба выключателя жили только в
 * базе (`app_settings`), то есть менялись SQL-запросом.
 *
 * ВТОРОЙ ПЕРЕКЛЮЧАТЕЛЬ ОПАСНЕЕ ПЕРВОГО, и это главное, что здесь стережётся.
 * «Писать сразу в карточку» означает, что код домофона или артикул может молча
 * стать телефоном клиента, — а по телефону из карточки звонят и диктуют его
 * мастеру вслух. Поэтому он выключен по умолчанию на сервере, недоступен при
 * выключенном разборе и подписан ценой ошибки, а не пользой.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - сняли `can("settings:manage")` с блока — падает «руководителю блока не
 *    показывают»;
 *  - убрали `disabled={!value.enabled}` у второго тумблера — падает «писать
 *    сразу недоступно, пока разбор выключен»;
 *  - стали слать оба поля разом (`{enabled, autofill}`) — падает «шлём ровно
 *    тот тумблер, который тронули»;
 *  - вернули после ошибки собственное состояние вместо ответа сервера
 *    (тумблер оставался переключённым) — падает «после отказа тумблер
 *    остаётся в прежнем положении».
 */

const ADMIN: Permission[] = ["accounts:read", "accounts:manage", "settings:manage"];
/** Руководитель: каналы видит, настройками системы не управляет (11 §4.1). */
const HEAD: Permission[] = ["accounts:read"];

const ACCOUNT = {
  id: "acc-1",
  title: "LP-Москва",
  avito_user_id: 111,
  status: "active",
  token_expires_at: new Date(Date.now() + 23 * 3_600_000).toISOString(),
  created_at: "2026-07-01T10:00:00Z",
  token: {
    state: "ok",
    message: "Токен активен. Обновится автоматически 13.08 в 16:07",
    last_refresh_at: null,
    action: null,
  },
  webhook: {
    status: "ok",
    url: null,
    last_event_at: null,
    state: "quiet",
    message: "Подписка стоит, событий ещё не было.",
    action: null,
  },
  backfill: { status: "idle" },
  operators: { count: 0, preview: [] },
};

interface Call {
  url: string;
  method: string;
  body: unknown;
}

/** Поля 12.09 (наши номера, автообъединение) — с умолчаниями сервера. */
const ПОЛНЫЕ = { own_numbers: "", own_numbers_parsed: [] as string[], merge_auto: "off" };

function mount({
  permissions = ADMIN,
  settings: неполные = { enabled: true, autofill: false },
  settingsStatus = 200,
  patchStatus = 200,
  patchError = { code: "internal", message: "не вышло" },
}: {
  permissions?: Permission[];
  settings?: { enabled: boolean; autofill: boolean; merge_auto?: string; own_numbers?: string };
  settingsStatus?: number;
  patchStatus?: number;
  patchError?: { code: string; message: string };
} = {}) {
  const settings = { ...ПОЛНЫЕ, ...неполные };
  const calls: Call[] = [];
  resetSessionStore({ user: fakeUser, permissions, accessToken: "t", bootstrapped: true });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, method, body });
      if (url.includes("/settings/phone-detect")) {
        if (method === "PATCH") {
          if (patchStatus !== 200) {
            return jsonResponse(patchStatus, {
              error: { ...patchError, request_id: "req_test" },
            });
          }
          // Сервер отвечает состоянием ПОСЛЕ правки — экран берёт его, а не
          // додумывает своё.
          return jsonResponse(200, { ...settings, ...(body as object) });
        }
        if (settingsStatus !== 200) {
          return jsonResponse(settingsStatus, {
            error: { code: "internal", message: "не вышло", request_id: "req_test" },
          });
        }
        return jsonResponse(200, settings);
      }
      if (url.includes("/avito-accounts")) {
        return jsonResponse(200, { items: [ACCOUNT], page: { limit: 50, offset: 0, total: 1 } });
      }
      return jsonResponse(200, {});
    }),
  );
  renderWithProviders(<AccountsPage />);
  return calls;
}

describe("Телефоны в переписке — переключатели разбора", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("администратор видит оба тумблера в том положении, что на сервере", async () => {
    mount({ settings: { enabled: true, autofill: false } });

    expect(await screen.findByText("Телефоны в переписке")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Распознавать телефоны в тексте сообщений/ })).toBeChecked();
    expect(screen.getByRole("switch", { name: /Писать найденный номер прямо в карточку/ })).not.toBeChecked();
  });

  it("руководителю блока не показывают вовсе", async () => {
    // Погашенные тумблеры без объяснения читаются как поломка, а сервер ему всё
    // равно откажет: право `settings:manage` есть только у администратора.
    mount({ permissions: HEAD });

    // Карточка канала на месте — значит экран отрисовался, и дело не в пустой
    // странице.
    expect(await screen.findByText("LP-Москва")).toBeInTheDocument();
    expect(screen.queryByText("Телефоны в переписке")).toBeNull();
  });

  it("шлём ровно тот тумблер, который тронули", async () => {
    // Экран открыт у двоих: один включает разбор, второй в ту же минуту
    // выключает автозапись. Полный PATCH второго вернул бы разбор в то
    // состояние, которое он загрузил минуту назад, молча отменив чужое решение.
    const calls = mount({ settings: { enabled: true, autofill: false } });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("switch", { name: /Писать найденный номер прямо в карточку/ }));

    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH");
      expect(patch?.body).toEqual({ autofill: true });
    });
  });

  it("«писать сразу» недоступно, пока разбор выключен", async () => {
    // Сочетание «писать сразу» без «распознавать» — бессмыслица, и увидеть это
    // человек должен на экране, а не после сохранения.
    mount({ settings: { enabled: false, autofill: false } });

    expect(await screen.findByRole("switch", { name: /Писать найденный номер прямо в карточку/ })).toBeDisabled();
  });

  it("подпись автозаписи называет цену ошибки, а не пользу", async () => {
    mount({ settings: { enabled: true, autofill: true } });

    // Разбор ошибается на цифрах, которые телефоном не являются (код домофона,
    // артикул), а по номеру из карточки звонят. Человек, включающий это, обязан
    // прочитать, чем платит.
    expect(await screen.findByText(/код домофона, артикул/)).toBeInTheDocument();
  });

  it("после отказа сервера тумблер остаётся в прежнем положении", async () => {
    mount({ settings: { enabled: true, autofill: false }, patchStatus: 500 });
    const user = userEvent.setup();

    const autofill = await screen.findByRole("switch", { name: /Писать найденный номер прямо в карточку/ });
    await user.click(autofill);

    // Молчаливо оставленный включённым тумблер — худший исход: человек уходит
    // уверенный, что номера теперь пишутся сами, а сервер об этом не знает.
    await waitFor(() => expect(autofill).not.toBeChecked());
  });

  it("отказ по «Нашим номерам» — словами сервера, поле подсвечено (проверка 24.09)", async () => {
    const { showToast } = await import("@/shared/ui/toast");
    const reason = "Слишком длинно: не больше 2000 символов";
    mount({ patchStatus: 400, patchError: { code: "validation_error", message: reason } });
    const поле = await screen.findByLabelText(/Наши номера/);
    expect(поле).toHaveAttribute("maxlength", "2000");
    await userEvent.type(поле, "+7 900 111-22-41");
    await userEvent.tab();

    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const тост = vi.mocked(showToast).mock.calls.at(-1)![0] as { message?: string };
    expect(тост.message).toBe(reason);
    expect(поле).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("не загрузилось — говорим, что настройки при этом не изменились", async () => {
    mount({ settingsStatus: 500 });

    // Ошибка ЗАГРУЗКИ читается как «настройки слетели», и это самое страшное,
    // что можно подумать про блок, управляющий чужими телефонами.
    //
    // Запросы в приложении повторяются дважды (queryClient, `retry: 2`),
    // поэтому отказу дают дожить до конца повторов — как в DistributionTab.
    // С 11.09 на той же странице стоит соседний блок «Адреса в переписке» с
    // таким же отказом — ищем именно свой.
    await waitFor(
      () =>
        expect(
          screen
            .getAllByRole("alert")
            .some((t) => /распознавания телефонов не загрузились/.test(t.textContent ?? "")),
        ).toBe(true),
      { timeout: 5000 },
    );
    const своя = screen
      .getAllByRole("alert")
      .find((t) => /распознавания телефонов не загрузились/.test(t.textContent ?? ""));
    expect(своя).toHaveTextContent(/настройки при этом не изменились/);
    expect(screen.getAllByRole("button", { name: "Повторить" }).length).toBeGreaterThan(0);
  });
});
