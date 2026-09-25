import { useState } from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ConnectChannelWizard } from "@/features/settings/accounts/ConnectChannelWizard";
import { renderWithProviders } from "./render";

/**
 * ЧУЖАЯ ПОДПИСКА: СЕРВЕР ПРОСИТ ПОДТВЕРДИТЬ — ЗНАЧИТ НУЖНО ЧЕМ.
 *
 * ⚠ БОЕВОЙ СЛУЧАЙ 28.08, снимок от владельца: два одинаковых красных тоста
 * «Не подключилось» с текстом «…Подтвердите, что канал нужно забрать».
 *
 * Авито держит на аккаунт РОВНО ОДНУ подписку на события, и подключение
 * вытесняет ту, что стоит сейчас — у владельца это работающий Jivo на боевом
 * канале. Сервер поэтому честно отказывает 409 с `reason: subscription_taken`
 * и просит прислать `takeover_confirmed`.
 *
 * А интерфейс показывал этот отказ обычным тостом: требование «подтвердите» и
 * НИ ОДНОГО способа подтвердить. Человек жал «Подключить» снова и получал тот
 * же тост вторым экземпляром — ровно то, что на снимке.
 */

const ОТВЕТ_409 = {
  error: {
    code: "conflict",
    message: "На этом аккаунте Авито уже стоит подписка на события…",
    details: {
      reason: "subscription_taken",
      subscriptions: ["https://jivo.example/webhook/avito/123"],
      confirm_field: "takeover_confirmed",
    },
    request_id: "req_1",
  },
};

function ответ(status: number, body: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe("Перехват канала у чужой системы", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  const тела = () =>
    fetchMock.mock.calls
      .filter(([, o]) => (o as RequestInit | undefined)?.method === "POST")
      .map(([u, o]) => ({
        url: String(u),
        body: JSON.parse(String((o as RequestInit).body ?? "{}")),
      }))
      .filter((c) => c.url.includes("/avito-accounts/connect"));

  beforeEach(() => {
    queryClient.clear();
    // ⚠ СЧЁТЧИК СВОЙ, А НЕ ПО `mock.calls`: текущий вызов туда уже записан к
    // моменту, когда тело мока выполняется, и «это первый запрос?» всегда
    // отвечало «нет». Первая редакция теста на этом и споткнулась: 409 не
    // приходил ни разу, а проверка выглядела как поломка кода.
    let подключений = 0;
    fetchMock = vi.fn(async (url: string) => {
      if (String(url).includes("/avito/app")) {
        return ответ(200, { live: true });
      }
      if (String(url).includes("/avito-accounts/connect")) {
        подключений += 1;
        return подключений === 1
          ? ответ(409, ОТВЕТ_409)
          : ответ(200, { id: "a1", title: "Канал", created: true });
      }
      return ответ(200, {});
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function заполнить() {
    renderWithProviders(<ConnectChannelWizard opened onClose={vi.fn()} />);
    await userEvent.type(await screen.findByLabelText(/Client ID/i), "cid");
    await userEvent.type(screen.getByLabelText(/Client Secret/i), "sec");
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
  }

  it("на 409 показывает ВОПРОС С КНОПКОЙ, а не тупиковый тост", async () => {
    await заполнить();
    expect(
      await screen.findByRole("button", { name: "Забрать канал себе" }),
      "подтверждать по-прежнему нечем — человек будет жать «Подключить» по кругу",
    ).toBeInTheDocument();
  });

  it("называет чужие подписки: решение не должно быть слепым", async () => {
    await заполнить();
    await screen.findByRole("button", { name: "Забрать канал себе" });
    expect(screen.getByText(/jivo\.example/)).toBeInTheDocument();
  });

  it("первый запрос идёт БЕЗ согласия — оно про нажатие, а не про аккаунт", async () => {
    await заполнить();
    await screen.findByRole("button", { name: "Забрать канал себе" });
    expect(тела()[0].body.takeover_confirmed).toBeUndefined();
  });

  it("подтверждение шлёт takeover_confirmed вторым запросом", async () => {
    await заполнить();
    await userEvent.click(await screen.findByRole("button", { name: "Забрать канал себе" }));
    expect(тела()).toHaveLength(2);
    expect(тела()[1].body.takeover_confirmed).toBe(true);
    // Ключи те же: повторный запрос — продолжение того же действия.
    expect(тела()[1].body.client_id).toBe("cid");
  });

  /*
   * ⚠ СОГЛАСИЕ НЕ ПЕРЕЖИВАЕТ ОКНО И СМЕНУ КЛЮЧЕЙ (проверка 24.09). Вопрос про
   * аккаунт A оставался после «Отмена»: окно для аккаунта B открывалось с
   * плашкой подписок A и единственной кнопкой «Забрать канал себе», и она
   * уезжала с ключами B — снимая его подписку, которую человек не видел.
   */
  // Окном управляет обёртка, а не `rerender`: перерисовка мимо провайдеров
  // роняет запрос внутри мастера (разбор — в ConnectWizardResetsSource).
  function Wrapper() {
    const [opened, setOpened] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setOpened((v) => !v)}>
          переключить
        </button>
        <ConnectChannelWizard opened={opened} onClose={() => setOpened(false)} />
      </>
    );
  }

  it("после повторного открытия окна первый запрос снова идёт без согласия", async () => {
    renderWithProviders(<Wrapper />);
    await userEvent.type(await screen.findByLabelText(/Client ID/i), "cid");
    await userEvent.type(screen.getByLabelText(/Client Secret/i), "sec");
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    await screen.findByRole("button", { name: "Забрать канал себе" });

    await userEvent.click(screen.getByRole("button", { name: "Отмена" }));
    await userEvent.click(screen.getByRole("button", { name: "переключить" }));

    expect(screen.queryByRole("button", { name: "Забрать канал себе" })).toBeNull();
    expect(screen.queryByText(/jivo\.example/)).toBeNull();
    await userEvent.type(await screen.findByLabelText(/Client ID/i), "cid-B");
    await userEvent.type(screen.getByLabelText(/Client Secret/i), "sec-B");
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(тела()[1].body.client_id).toBe("cid-B");
    expect(тела()[1].body.takeover_confirmed).toBeUndefined();
  });

  it("правка ключей после вопроса снимает согласие", async () => {
    await заполнить();
    await screen.findByRole("button", { name: "Забрать канал себе" });

    await userEvent.type(screen.getByLabelText(/Client ID/i), "-исправлено");

    expect(screen.queryByRole("button", { name: "Забрать канал себе" })).toBeNull();
    expect(screen.getByRole("button", { name: "Подключить" })).toBeInTheDocument();
  });
});
