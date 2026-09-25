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
 * «Служебные записи Авито» — выключатель показа в ленте переписки.
 *
 * ⚠ ЗАЧЕМ ЭТИ ТЕСТЫ ВООБЩЕ НАПИСАНЫ. Настройку завели 14 августа по дословной
 * просьбе владельца («сделай так, чтобы можно было отключить все системные
 * сообщения в диалоге, которые отправляет Авито, так как в самой Jivo их нет»),
 * лента научилась её слушать — а записать значение было НЕЧЕМ: ни ручки на
 * сервере, ни экрана. Просьба оказалась выполнена наполовину, и обнаружилось
 * это уже после выкатки. Заслон здесь ровно от этого: настройка, у которой нет
 * выключателя, — не настройка.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - сняли `can("settings:manage")` с блока — падает «руководителю не показываем»;
 *  - вернули после ошибки собственное состояние вместо ответа сервера — падает
 *    «после отказа тумблер остаётся в прежнем положении»;
 *  - убрали `invalidateQueries(["messages"])` — падает «лента перечитывается».
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
  token: { state: "ok", message: "Токен активен", last_refresh_at: null, action: null },
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

function mount({
  permissions = ADMIN,
  hidden = false,
  patchStatus = 200,
}: { permissions?: Permission[]; hidden?: boolean; patchStatus?: number } = {}) {
  const calls: Call[] = [];
  resetSessionStore({ user: fakeUser, permissions, accessToken: "t", bootstrapped: true });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, method, body });
      if (url.includes("/settings/thread")) {
        if (method === "PATCH") {
          if (patchStatus !== 200) {
            return jsonResponse(patchStatus, {
              error: { code: "internal", message: "не вышло", request_id: "req_test" },
            });
          }
          // Сервер отвечает состоянием ПОСЛЕ правки — экран берёт его, а не
          // додумывает своё.
          return jsonResponse(200, { avito_system_hidden: false, ...(body as object) });
        }
        return jsonResponse(200, { avito_system_hidden: hidden });
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

/** Ищем по РОЛИ, а не по подписи: у Mantine `Switch` подпись с полем
 *  связана через обёртку, и `getByLabelText` его не находит. */
const SWITCH = /Не показывать служебные записи Авито в ленте/;
const toggle = () => screen.getByRole("switch", { name: SWITCH });
const findToggle = () => screen.findByRole("switch", { name: SWITCH });

describe("Служебные записи Авито — выключатель показа", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("выключатель есть на экране и по умолчанию снят — записи показываются", async () => {
    mount();

    expect(await findToggle()).not.toBeChecked();
    // Подпись описывает ТЕКУЩЕЕ состояние, а не возможность: статичная
    // продолжала бы описывать выключенный режим после включения.
    expect(screen.getByText(/Переписка показывается целиком/)).toBeInTheDocument();
  });

  it("щелчок уходит на сервер и возвращается его ответом", async () => {
    const user = userEvent.setup();
    const calls = mount();

    await user.click(await findToggle());

    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH" && c.url.includes("/settings/thread"));
      expect(patch?.body).toEqual({ avito_system_hidden: true });
    });
    await waitFor(() => expect(toggle()).toBeChecked());
    expect(screen.getByText(/не видны/)).toBeInTheDocument();
  });

  it("сервер отказал — тумблер остаётся в прежнем положении", async () => {
    const user = userEvent.setup();
    mount({ patchStatus: 500 });

    await user.click(await findToggle());

    // Положение берётся из ответа сервера, а не из своей памяти. Иначе экран
    // показывал бы «спрятано», когда на сервере ничего не изменилось, — и
    // человек ушёл бы уверенным, что настроил.
    await waitFor(() => expect(toggle()).not.toBeChecked());
  });

  it("руководителю выключателя не показывают вовсе", async () => {
    mount({ permissions: HEAD });

    // Дожидаемся отрисовки экрана, чтобы проверка не прошла на пустоте.
    expect(await screen.findByText("LP-Москва")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: SWITCH })).not.toBeInTheDocument();
    // И запроса за настройкой быть не должно: сервер ему всё равно откажет,
    // а погашенный тумблер без объяснения читается как поломка.
    expect(screen.queryByText("Служебные записи Авито")).not.toBeInTheDocument();
  });

  it("настройка спрятана — экран открывается с уже поднятым тумблером", async () => {
    mount({ hidden: true });

    await waitFor(() => expect(toggle()).toBeChecked());
  });
});
