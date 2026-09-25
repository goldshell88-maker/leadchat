import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { WorkingOnStrip } from "@/features/chats/components/thread/WorkingOnStrip";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ОТДЕЛ В СКОБКАХ У ИМЕНИ СОТРУДНИКА (просьба владельца 04.09, дословно:
 * «нужно сделать так, чтобы везде аккуратно в скобочках подписывались (Чатер)
 * (ОКК) и т.д.»).
 *
 * ⚠ ЧТО БЫЛО. Отдел лежал в базе с 7.4 и показывался ровно в одном месте —
 * колонкой на вкладке «Команда». В рабочих экранах человек был одним именем, а
 * у заказчика тринадцать человек из семи с лишним отделов, и часть ведёт один
 * и тот же тип обращений: по имени не отличить, к какому отделу идти.
 *
 * ⚠ ПОЧЕМУ ПОДПИСЬ ОДНА НА ВСЕ ЭКРАНЫ. Разъедься она — «Иванов (ОКК)» в шапке
 * ленты и «Иванов» в списке прочтутся как ДВА РАЗНЫХ ИВАНОВА. Тесты ниже
 * стерегут и саму функцию, и то, что экраны действительно её зовут.
 *
 * ⚠ ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - убрали схлопывание пробелов в `подписьСотрудника` — падает «два пробела
 *    в отделе схлопываются»;
 *  - разрешили скобки у пустого отдела — падает «пустой отдел скобок не даёт»;
 *  - вернули `хозяин.full_name` в `WorkingOnStrip` — падает «шапка ленты
 *    называет отдел ответственного»;
 *  - вернули `p.full_name` у позванных в `ClientCardPane` — падает «у
 *    позванного отдел виден»;
 *  - подписали собственное имя отделом — падает «себя отделом не подписываем».
 */

describe("подпись сотрудника: имя и отдел", () => {
  it("отдел приписывается в скобках после имени", () => {
    expect(подписьСотрудника({ full_name: "Зуев Денис", department: "Чатер" })).toBe(
      "Зуев Денис (Чатер)",
    );
  });

  it("пустой отдел скобок не даёт", () => {
    /*
     * Замер боя: отдел заполнен НЕ У ВСЕХ. «Иванов ()» читается как сломанный
     * экран, а не как «отдел не указан».
     */
    expect(подписьСотрудника({ full_name: "Иванов", department: null })).toBe("Иванов");
    expect(подписьСотрудника({ full_name: "Иванов", department: "" })).toBe("Иванов");
    expect(подписьСотрудника({ full_name: "Иванов", department: "   " })).toBe("Иванов");
    // Старая сборка сервера поля вообще не шлёт — это не повод рисовать скобки.
    expect(подписьСотрудника({ full_name: "Иванов" })).toBe("Иванов");
  });

  it("два пробела в отделе схлопываются", () => {
    /*
     * На бою рядом лежат «Диспетчер МНЧ» и «Диспетчер  МНЧ». Человек читает их
     * как один отдел, и подпись обязана выглядеть одинаково — иначе один и тот
     * же отдел выглядит как два разных.
     */
    expect(подписьСотрудника({ full_name: "Иванов", department: "Диспетчер  МНЧ" })).toBe(
      "Иванов (Диспетчер МНЧ)",
    );
    expect(подписьСотрудника({ full_name: "Иванов", department: "  ОКК " })).toBe("Иванов (ОКК)");
  });

  it("имя стоит первым, отдел — в хвосте", () => {
    /*
     * ЭТО ПРО РАСКЛАДКУ, А НЕ ПРО КРАСОТУ. В узких местах строка обрезается
     * многоточием С КОНЦА (`lc-truncate`, `dt__ell`, `op-grid__person`), и
     * порядок решает, что исчезнет первым. Отдел в хвосте — уходит он, имя
     * остаётся целым. Обратный порядок дал бы «(ОКК) Ольга Ков…»: отдел ценой
     * имени, а имя тут главное.
     */
    const подпись = подписьСотрудника({ full_name: "Ольга Ковалёва", department: "ОКК" });
    expect(подпись.indexOf("Ольга")).toBeLessThan(подпись.indexOf("ОКК"));
  });

  it("без имени возвращается запасное слово и БЕЗ скобок", () => {
    /*
     * «Коллега (ОКК)» обещало бы, что мы знаем, кто это. Запасное слово стоит
     * ровно там, где человека нет: удалённый сотрудник в предложении передачи,
     * реплика без автора.
     */
    expect(подписьСотрудника(null, "Коллега")).toBe("Коллега");
    expect(подписьСотрудника({ full_name: "", department: "ОКК" }, "Оператор")).toBe("Оператор");
  });
});

/** Ответственный — не я: подпись у чужого имени обязана нести отдел. */
const ЧУЖОЙ = { id: "u-okk", full_name: "Ольга Ковалёва", department: "ОКК" };

describe("шапка ленты «В работе у …»", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  it("называет отдел ответственного", () => {
    renderWithProviders(
      <WorkingOnStrip conversation={makeConversation({ assignee: ЧУЖОЙ, participants: [] })} />,
    );
    expect(screen.getByText(/В работе у Ольга Ковалёва \(ОКК\)/)).toBeInTheDocument();
  });

  it("свой диалог остаётся «у вас» — себя отделом не подписываем", () => {
    /*
     * Свой отдел человек знает; скобки здесь были бы шумом в строке, которая и
     * так появляется только когда есть новость. Строка «В работе у вас»
     * рисуется лишь при госте рядом — его и добавляем.
     */
    renderWithProviders(
      <WorkingOnStrip
        conversation={makeConversation({
          assignee: { id: fakeUser.id, full_name: fakeUser.full_name, department: "Чатер" },
          participants: [
            { ...ЧУЖОЙ, reason: null, invited_at: null, kind: "invited" },
          ],
        })}
      />,
    );
    const строка = screen.getByRole("status");
    expect(строка.textContent).toContain("В работе у вас");
    expect(строка.textContent).not.toContain("вас (Чатер)");
    // А вот гость рядом подписан отделом — он-то чужой.
    expect(строка.textContent).toContain("Ольга Ковалёва (ОКК)");
  });
});

describe("карточка диалога: ответственный и позванные", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("client-history")) return jsonResponse(200, { client: {}, items: [] });
        return jsonResponse(200, { items: [] });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("у ответственного и у позванного отдел виден, у себя — нет", async () => {
    queryClient.setQueryData(
      qk.conversations.detail(CONV_ID),
      makeConversation({
        assignee: ЧУЖОЙ,
        participants: [
          { ...ЧУЖОЙ, reason: "нужен взгляд ОКК", invited_at: null, kind: "invited" },
          {
            id: fakeUser.id,
            full_name: fakeUser.full_name,
            department: "Чатер",
            reason: null,
            invited_at: null,
            kind: "self",
          },
        ],
      }),
    );
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    // Ответственный (у роли manager вместо селекта текст) и строка «Позваны».
    expect(await screen.findAllByText(/Ольга Ковалёва \(ОКК\)/)).not.toHaveLength(0);
    // Себя карточка называет «Вы» — и без отдела: свой отдел человек знает.
    // Отрицательная проверка стоит ПЕРВОЙ, чтобы падать по своей причине: иначе
    // её опередил бы точный `getByText("Вы")` ниже.
    expect(screen.queryByText(/Вы \(Чатер\)/)).not.toBeInTheDocument();
    expect(screen.getByText("Вы")).toBeInTheDocument();
  });
});
