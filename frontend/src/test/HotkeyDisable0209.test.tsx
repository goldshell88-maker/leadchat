import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HotkeysEditor } from "@/features/settings/profile/HotkeysEditor";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { hotkeyRows } from "@/features/hotkeys/catalog";
import { actionFor } from "@/features/hotkeys/dispatch";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * СОЧЕТАНИЕ МОЖНО ВЫКЛЮЧИТЬ ПООТДЕЛЬНОСТИ.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 02.09: «сделать возможность отдельно выключать комбинации
 * клавиш».
 *
 * ⚠ ПОЧЕМУ ЭТО ПУСТОЙ СПИСОК, А НЕ НОВОЕ ПОЛЕ. Разбор нажатия и так молчит на
 * пустом списке: `dispatch.actionFor` берёт `bindings[a.id] ?? a.defaults`, и
 * `[].includes(...)` — ложь. То есть выключение уже существовало в основании, не
 * хватало только способа туда попасть. Заводить рядом флаг `enabled` значило бы
 * получить два ответа на один вопрос — «сочетаний нет» и «выключено», — которые
 * разъедутся на первой же правке.
 *
 * ⚠ ЧТО ЛЕГКО ПОТЕРЯТЬ. «Включить» обязано вернуть ИМЕННО ТО, что стояло, а не
 * умолчание: у человека могло быть своё сочетание, и молча подменить его
 * умолчанием — значит отнять настройку так, что он узнает об этом, только когда
 * привычная клавиша перестанет работать.
 *
 * ⚠ НАБОР ПЕРЕПИСАН В ТОТ ЖЕ ДЕНЬ, И ВОТ ПОЧЕМУ. Через час после него владелец
 * попросил сделать клавиши выключенными ИЗНАЧАЛЬНО, все кроме приёма. Тесты
 * ниже писались, когда «Закрыть диалог» работал по умолчанию, — теперь он
 * выключен, и «выключить» в чистой учётной записи нечего. Поэтому каждый тест
 * сперва ВКЛЮЧАЕТ действие явно: проверяется путь «включено → выключено», а он
 * от смены умолчаний не зависит.
 */

function строка(что: string | RegExp): HTMLElement {
  const tr = screen.getByText(что).closest("tr");
  if (!tr) throw new Error("строка не найдена");
  return tr;
}

describe("Выключение отдельного сочетания", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "conversations:manage", "notes:write"],
      accessToken: "t",
      bootstrapped: true,
    });
    // Действие включено явно: с 02.09 по умолчанию оно выключено, и выключать
    // в чистой учётной записи было бы нечего.
    useSessionStore.setState({ hotkeys: { close: ["Mod+KeyD"] } });
    fetchMock = vi.fn(async () => jsonResponse(200, { hotkeys: {} }));
    vi.stubGlobal("fetch", fetchMock);
  });

  it("⚠ выключённое действие не срабатывает на своей клавише", () => {
    /* Главное свойство: без него кнопка «выключить» — украшение. */
    const событие = new KeyboardEvent("keydown", { code: "KeyD", ctrlKey: true });
    expect(
      actionFor(событие, false, { close: ["Mod+KeyD"] })?.id,
      "включённое человеком сочетание не работает",
    ).toBe("close");
    expect(
      actionFor(событие, false, { close: [] }),
      "выключенное действие всё равно срабатывает — выключатель ничего не выключает",
    ).toBeNull();
  });

  it("шпаргалка пишет «выключено», а не пустоту", () => {
    /*
     * Пустая ячейка читается как «не знаем». Человек обязан видеть, что это ОН
     * выключил, иначе будет искать поломку там, где её нет.
     */
    const строки = hotkeyRows({ close: [] });
    const закрытие = строки.find((r) => r.what.includes("Закрыть диалог"));
    expect(закрытие?.keys).toBe("выключено");
  });

  it("⚠ кнопка выключает, и это видно в таблице", () => {
    renderWithProviders(<HotkeysEditor />);
    const tr = строка(/Закрыть диалог/);
    fireEvent.click(within(tr).getByRole("switch", { name: /Выключить сочетание/ }));
    expect(
      within(строка(/Закрыть диалог/)).getByText("выключено"),
      "выключение не видно в таблице — человек не поймёт, сработало ли",
    ).toBeInTheDocument();
  });

  it("⚠ выключение ВКЛЮЧЁННОГО ПО УМОЛЧАНИЮ уходит на сервер пустым списком", async () => {
    /*
     * ⚠ ЗДЕСЬ ПРОВЕРЯЕТСЯ ЕДИНСТВЕННЫЙ СЛУЧАЙ, ГДЕ ПУСТОЙ СПИСОК ОБЯЗАН УЕХАТЬ.
     *
     * Разница принципиальная: нет ключа — «действует умолчание», пустой список —
     * «человек выключил». Для приёма диалога умолчание — ВКЛЮЧЕНО, поэтому
     * выключение обязано быть записано: иначе клавиша вернулась бы сама при
     * следующем входе.
     *
     * У остальных действий умолчание с 02.09 — «выключено», и там пустой список
     * писать НЕ НАДО: отсутствие ключа и так означает «выключено». Хранить
     * лишнее — значит заморозить у человека сегодняшнюю таблицу и не доставить
     * ему завтрашнюю правку умолчаний.
     */
    renderWithProviders(<HotkeysEditor />);
    fireEvent.click(
      within(строка(/Принять диалог/)).getByRole("switch", { name: /Выключить сочетание/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const тело = JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body));
    expect(тело.hotkeys.claim, "выключение приёма не доехало до сервера").toEqual([]);
  });

  it("выключение того, что и так выключено по умолчанию, лишнего не хранит", async () => {
    /*
     * Обратная сторона того же правила: «выключено» совпало с умолчанием —
     * значит хранить нечего, и личная настройка с человека снимается. Так
     * завтрашняя правка умолчаний до него доедет.
     */
    /*
     * Берём ПЕРЕДАЧУ: с 02.09 закрытие включено по умолчанию, и выключение его
     * как раз обязано записаться. Здесь нужно обратное — действие, у которого
     * умолчание уже «выключено».
     */
    useSessionStore.setState({ hotkeys: { transfer: ["Mod+KeyT"] } });
    renderWithProviders(<HotkeysEditor />);
    fireEvent.click(
      within(строка(/Передать диалог/)).getByRole("switch", { name: /Выключить сочетание/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const тело = JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body));
    expect(тело.hotkeys.transfer, "лишняя запись заморозит у человека умолчания").toBeUndefined();
  });

  it("⚠ «включить» возвращает СВОЁ сочетание, а не умолчание", () => {
    useSessionStore.setState({ hotkeys: { close: ["Mod+KeyY"] } });
    renderWithProviders(<HotkeysEditor />);

    const tr = строка(/Закрыть диалог/);
    expect(within(tr).getByText("Ctrl + Y")).toBeInTheDocument();

    fireEvent.click(within(tr).getByRole("switch", { name: /Выключить сочетание/ }));
    expect(within(строка(/Закрыть диалог/)).getByText("выключено")).toBeInTheDocument();

    fireEvent.click(
      within(строка(/Закрыть диалог/)).getByRole("switch", { name: /Включить сочетание/ }),
    );
    expect(
      within(строка(/Закрыть диалог/)).getByText("Ctrl + Y"),
      "включение подменило личное сочетание умолчанием — настройка пропала молча",
    ).toBeInTheDocument();
  });

  it("включение без прежнего личного сочетания возвращает привычное", () => {
    renderWithProviders(<HotkeysEditor />);
    const tr = строка(/Закрыть диалог/);
    fireEvent.click(within(tr).getByRole("switch", { name: /Выключить сочетание/ }));
    fireEvent.click(
      within(строка(/Закрыть диалог/)).getByRole("switch", { name: /Включить сочетание/ }),
    );
    expect(within(строка(/Закрыть диалог/)).getByText(/Ctrl \+ D/)).toBeInTheDocument();
  });
});
