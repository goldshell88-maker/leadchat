import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HotkeysEditor } from "@/features/settings/profile/HotkeysEditor";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ПЕРЕНАЗНАЧЕНИЕ ГОРЯЧИХ КЛАВИШ (требование заказчика от 13 августа).
 *
 * Два свойства здесь дороже самой записи, и оба легко потерять:
 *
 *   * НА СЕРВЕР УХОДЯТ ТОЛЬКО ОТЛИЧИЯ. Отправь мы всю таблицу — человек заморозил бы
 *     у себя все восемнадцать действий в сегодняшнем виде, и завтрашняя правка
 *     умолчаний до него бы не доехала. Молча: у всех работает, у него нет;
 *   * СОЧЕТАНИЕ ОТБИРАЕТСЯ У ПРЕЖНЕГО ВЛАДЕЛЬЦА. Два действия на одной клавише —
 *     это молчаливый спор, в котором побеждает порядок строк в реестре, а человек
 *     видит «клавиша делает не то».
 */

function строка(что: string | RegExp): HTMLElement {
  const ячейка = screen.getByText(что);
  const tr = ячейка.closest("tr");
  if (!tr) throw new Error("строка не найдена");
  return tr;
}

/*
 * ⚠ КНОПКА В СТРОКЕ ТЕПЕРЬ НЕ ОДНА (02.09). Рядом с «изменить» появилась
 * «выключить» — просьба владельца «отдельно выключать комбинации клавиш».
 * Поэтому кнопка ищется ПО ИМЕНИ, а не как единственная в строке: запрос
 * «единственная» молча сломался бы при следующем добавлении, и падение
 * выглядело бы как поломка переназначения, которым эти проверки и заняты.
 */
describe("Переназначение горячих клавиш", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "conversations:manage", "notes:write"],
      accessToken: "t",
      bootstrapped: true,
    });
    // ⚠ Сбрасываем ЯВНО: `resetSessionStore` ставит только переданные поля, и личные
    // сочетания из предыдущего теста иначе протекают в следующий — тот падает на
    // значении, которого сам не задавал, и причина ищется где угодно, только не здесь.
    useSessionStore.setState({ hotkeys: {} });
    fetchMock = vi.fn(async () => jsonResponse(200, { hotkeys: { claim: ["Mod+KeyY"] } }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("показывает текущие сочетания человека, а не только умолчания", () => {
    useSessionStore.setState({ hotkeys: { claim: ["Mod+KeyY"] } });
    renderWithProviders(<HotkeysEditor />);
    expect(строка(/Принять диалог/).textContent).toContain("Ctrl + Y");
  });

  it("ловит нажатое сочетание", () => {
    renderWithProviders(<HotkeysEditor />);
    const tr = строка(/Принять диалог/);
    fireEvent.click(within(tr).getByRole("button", { name: /Изменить сочетание/ }));
    fireEvent.keyDown(within(tr).getByRole("button", { name: /Изменить сочетание/ }), { code: "KeyY", key: "y", ctrlKey: true });
    expect(tr.textContent).toContain("Ctrl + Y");
  });

  it("⚠ ОТБИРАЕТ СОЧЕТАНИЕ У ПРЕЖНЕГО ВЛАДЕЛЬЦА", () => {
    /**
     * Иначе Ctrl+D осталось бы у двух действий разом, и какое сработает — решал бы
     * порядок строк в реестре. Человек увидел бы «клавиша делает не то», и понять
     * причину по экрану было бы нельзя.
     */
    renderWithProviders(<HotkeysEditor />);
    const принять = строка(/Принять диалог/);
    fireEvent.click(within(принять).getByRole("button", { name: /Изменить сочетание/ }));
    // Ctrl+D — сочетание «Закрыть диалог».
    fireEvent.keyDown(within(принять).getByRole("button", { name: /Изменить сочетание/ }), { code: "KeyD", key: "d", ctrlKey: true });

    expect(принять.textContent).toContain("Ctrl + D");
    // У «Закрыть» этого сочетания больше нет.
    expect(строка(/Закрыть диалог/).textContent).not.toContain("Ctrl + D");
  });

  it("Escape отменяет захват, а не назначается", () => {
    renderWithProviders(<HotkeysEditor />);
    const tr = строка(/Принять диалог/);
    const btn = within(tr).getByRole("button", { name: /Изменить сочетание/ });
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { code: "Escape", key: "Escape" });
    expect(tr.textContent).toContain("Ctrl + R");
  });

  it("на сервер уходят только отличия", async () => {
    renderWithProviders(<HotkeysEditor />);
    const tr = строка(/Принять диалог/);
    fireEvent.click(within(tr).getByRole("button", { name: /Изменить сочетание/ }));
    fireEvent.keyDown(within(tr).getByRole("button", { name: /Изменить сочетание/ }), { code: "KeyY", key: "y", ctrlKey: true });
    const сохранить = screen.getByRole("button", { name: "Сохранить" });
    expect(сохранить).not.toBeDisabled();
    fireEvent.click(сохранить);

    await waitFor(() => {
      const вызов = fetchMock.mock.calls.find(([u]) => String(u).includes("/auth/me/hotkeys"));
      expect(вызов).toBeTruthy();
      const тело = JSON.parse((вызов![1] as RequestInit).body as string);
      // Ровно одно действие — то, которое трогали. Никакого снимка всей таблицы.
      expect(Object.keys(тело.hotkeys)).toEqual(["claim"]);
    });
  });

  it("«Вернуть по умолчанию» отправляет пустоту", async () => {
    useSessionStore.setState({ hotkeys: { claim: ["Mod+KeyY"] } });
    renderWithProviders(<HotkeysEditor />);
    fireEvent.click(screen.getByRole("button", { name: "Вернуть по умолчанию" }));
    await waitFor(() => {
      const вызов = fetchMock.mock.calls.find(([u]) => String(u).includes("/auth/me/hotkeys"));
      expect(JSON.parse((вызов![1] as RequestInit).body as string)).toEqual({ hotkeys: {} });
    });
  });

  it("непереназначаемых в таблице нет", () => {
    /**
     * Esc — договор с браузером, «?» открывает саму справку, через которую настройку
     * и ищут. Дай их переназначить — и однажды человек останется без выхода из окна
     * и без способа узнать, что случилось.
     */
    renderWithProviders(<HotkeysEditor />);
    expect(screen.queryByText(/Закрыть по очереди/)).toBeNull();
    expect(screen.queryByText(/Эта справка/)).toBeNull();
  });

  it("наблюдателю не показывают того, чего он не может", () => {
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    renderWithProviders(<HotkeysEditor />);
    const таблица = document.querySelector("table")!.textContent ?? "";
    expect(таблица).not.toContain("Принять диалог");
    // А то, что работает у всех, — на месте, у каждой половины пары своя строка.
    expect(таблица).toContain("Следующий диалог");
    expect(таблица).toContain("Предыдущий диалог");
  });
});
