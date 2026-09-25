import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HotkeysEditor } from "@/features/settings/profile/HotkeysEditor";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { ACTIONS, hotkeyRows, действующие } from "@/features/hotkeys/catalog";
import { actionFor } from "@/features/hotkeys/dispatch";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * КЛАВИШИ ВЫКЛЮЧЕНЫ, ПОКА ЧЕЛОВЕК САМ ИХ НЕ ВКЛЮЧИТ.
 *
 * ⚠ РЕШЕНИЕ ВЛАДЕЛЬЦА 02.09: «сделай клавиши изначально неактивными, все кроме
 * принятия диалогов».
 *
 * ⚠ ПОЧЕМУ ЭТО ПРАВИЛЬНО, А НЕ ПРОСТО «КАК ПОПРОСИЛИ». За месяц незваная горячая
 * клавиша давала беду трижды подряд: Ctrl+Backspace при наборе ОТКЛОНЯЛ диалог,
 * Ctrl+Enter вместо отправки ПРИНИМАЛ чужой, Ctrl+R перезагружал страницу. Каждая
 * правка чинила свой случай, а причина общая: клавиши работали у всех, а нужны
 * немногим.
 *
 * ⚠ ЧТО ЛЕГКО СЛОМАТЬ. `defaults` теперь означает не «действует», а «встанет,
 * когда включат». Спутай их — и либо клавиши снова заработают у всех, либо
 * кнопка «включить» перестанет что-либо возвращать.
 */

function строка(что: string | RegExp): HTMLElement {
  const tr = screen.getByText(что).closest("tr");
  if (!tr) throw new Error("строка не найдена");
  return tr;
}

describe("Горячие клавиши выключены по умолчанию", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "conversations:manage", "notes:write"],
      accessToken: "t",
      bootstrapped: true,
    });
    useSessionStore.setState({ hotkeys: {} });
    fetchMock = vi.fn(async () => jsonResponse(200, { hotkeys: {} }));
    vi.stubGlobal("fetch", fetchMock);
  });

  it("⚠ у чистой учётной записи работают ТОЛЬКО приём и закрытие", () => {
    /*
     * ⚠ СПИСОК ПОПОЛНЕН 02.09 ПО РЕШЕНИЮ ВЛАДЕЛЬЦА: закрытие диалога включено
     * рядом с приёмом. Приём и закрытие — две половины одного движения: взял
     * диалог, ответил, закрыл. Оставить включённой только первую значило бы
     * заставить тринадцать человек включать вторую руками в тот же день.
     *
     * `escape` и `help` оставлены намеренно и в списке не значатся: их нельзя
     * переназначить, в таблице настройки их нет, и выключенными их нечем
     * вернуть — человек оказался бы в модалке, которую не закрыть, и без
     * справки, чтобы понять почему.
     *
     * ⚠ СПИСОК ПОПОЛНЕН 09.09: листание диалогов (`listNext`/`listPrev`). Это
     * не отмена решения 02.09, а его же правило на новом месте. 02.09
     * выключались УСКОРИТЕЛИ: у каждого действия рядом стояла кнопка, и
     * выключенная клавиша ничего не отнимала. 09.09 владелец велел убрать из
     * шапки ленты шевроны «предыдущий/следующий» — и клавиша перестала быть
     * ускорителем, став способом перейти к соседу без мыши. То есть попала в
     * ту же категорию, по которой остались приём и закрытие.
     *
     * Этот тест — единственное место, где список включённых по умолчанию
     * записан вслух. Добавили действие в умолчания мимо него — узнаете об этом
     * от диспетчера, у которого клавиша сработала сама.
     */
    const работают = ACTIONS.filter((a) => действующие(a).length).map((a) => a.id);
    expect(
      работают.filter((id) => id !== "escape" && id !== "help"),
      "по умолчанию работает не то, что решено: клавиша сработает у того, кто её не включал",
    ).toEqual(["claim", "close", "listNext", "listPrev"]);
  });

  it("Escape оставлен намеренно — иначе окно нечем закрыть", () => {
    /*
     * Его нельзя переназначить (`fixed`), в таблице настройки его нет. Выключив
     * его, мы заперли бы человека в модалке без способа вернуть клавишу.
     */
    const escape = ACTIONS.find((a) => a.id === "escape")!;
    expect(действующие(escape).length).toBeGreaterThan(0);
  });

  it("⚠ выключенное по умолчанию НЕ срабатывает на своей прежней клавише", () => {
    // Берём передачу диалога: закрытие с 02.09 включено, и на нём это уже не
    // проверить. Действие выбрано осознанно — оно из тех, что выключены.
    const событие = new KeyboardEvent("keydown", { code: "KeyT", ctrlKey: true });
    expect(
      actionFor(событие, false, {}),
      "Ctrl+T по-прежнему передаёт диалог у того, кто клавишу не включал",
    ).toBeNull();
  });

  it("закрытие диалога работает без всякой настройки", () => {
    const событие = new KeyboardEvent("keydown", { code: "KeyD", ctrlKey: true });
    expect(actionFor(событие, false, {})?.id, "закрытие диалога выключилось").toBe("close");
  });

  it("приём диалога работает без всякой настройки", () => {
    const событие = new KeyboardEvent("keydown", { code: "KeyR", ctrlKey: true });
    expect(actionFor(событие, false, {})?.id, "приём диалога тоже выключился").toBe("claim");
  });

  it("⚠ включённое человеком работает — умолчание не спорит с настройкой", () => {
    const событие = new KeyboardEvent("keydown", { code: "KeyD", ctrlKey: true });
    expect(actionFor(событие, false, { close: ["Mod+KeyD"] })?.id).toBe("close");
  });

  it("шпаргалка честно пишет «выключено»", () => {
    // Передача — из выключенных; закрытие с 02.09 включено.
    const передача = hotkeyRows({}).find((r) => r.what.includes("Передать диалог"));
    expect(передача?.keys).toBe("выключено");
  });

  it("⚠ «включить» возвращает ПРИВЫЧНОЕ сочетание, а не пустоту", () => {
    /*
     * Здесь и видно, зачем `defaults` сохранены: стерев их, мы оставили бы
     * человека выдумывать комбинации заново.
     */
    renderWithProviders(<HotkeysEditor />);
    // Передача — из выключенных по умолчанию; закрытие с 02.09 включено.
    const tr = строка(/Передать диалог/);
    expect(within(tr).getByText("выключено")).toBeInTheDocument();

    fireEvent.click(within(tr).getByRole("switch", { name: /Включить сочетание/ }));
    expect(
      within(строка(/Передать диалог/)).getByText(/Ctrl \+ T/),
      "включение не вернуло привычное сочетание — человеку нечего включать",
    ).toBeInTheDocument();
  });

  it("⚠ включённое доезжает до сервера, хотя совпадает с прежним умолчанием", async () => {
    /*
     * Ловушка отправки «только отличий»: сравни мы включённый Ctrl+D с
     * `defaults`, он оказался бы «не отличием», не уехал на сервер — и клавиша
     * осталась бы выключенной, хотя человек её только что включил.
     */
    renderWithProviders(<HotkeysEditor />);
    fireEvent.click(
      within(строка(/Передать диалог/)).getByRole("switch", { name: /Включить сочетание/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const тело = JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body));
    // У передачи два сочетания: новое и прежнее, оставленное для привыкших.
    expect(тело.hotkeys.transfer, "включение не доехало до сервера").toEqual([
      "Mod+KeyT",
      "Mod+Shift+KeyT",
    ]);
  });
});
