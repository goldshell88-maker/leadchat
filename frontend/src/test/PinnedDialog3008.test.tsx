import { включитьВсеСочетания } from "./helpers";
/**
 * ЖАЛОБА ВЛАДЕЛЬЦА 30.08: «интерфейс чата резко и самовольно переключается на
 * совершенно другого клиента прямо в процессе набора — сообщения уходят не тем
 * людям».
 *
 * САМОВОЛЬНОГО НЕ БЫЛО НИЧЕГО, И ЭТО ВАЖНО ДЛЯ ПОНИМАНИЯ ПОЧИНКИ. Активный
 * диалог пишется ровно из двух мест — адреса страницы и клика по уведомлению
 * настольного приложения (на вебе мост не подключается вовсе). Ни один фоновый
 * запрос, ни один кадр сокета экран не уводит. Уводила клавиатура, двумя
 * путями:
 *
 * 1. `Mod+Enter` был вторым сочетанием на «принять диалог» и работал прямо из
 *    поля ввода, а композер Enter С МОДИФИКАТОРОМ намеренно не отправлял.
 *    Привычное по Jivo и Telegram «Ctrl+Enter — отправить» попадало в приём:
 *    открытый диалог уже принят, в очереди его нет, и ветка «взять первого
 *    ждущего» открывала ПОСТОРОННЕГО человека, попутно назначив его на
 *    менеджера.
 *
 *    ⚠ САМ `Mod+Enter` С ПРИЁМА СНЯТ И ОТДАН ОТПРАВКЕ (см. SendOnCtrlEnter3008),
 *    поэтому здесь жмётся ОСТАВШЕЕСЯ сочетание приёма — Ctrl+R. Жать снятое
 *    значило бы сторожить пустоту: тест был бы зелёным и со снятым замком.
 *
 * 2. Ctrl+↑/↓ и Alt+↑/↓ — шаг по списку. В Windows Ctrl+стрелка это привычное
 *    перемещение по тексту, и человек жал его посреди слова, не подозревая,
 *    что вместе с курсором уедет переписка.
 *
 * ЧТО СТОРОЖИМ. Поведение, а не текст исходника: нажатия идут настоящим путём
 * через `useChatHotkeys`, как в бою.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useChatHotkeys, useClaimKeyGuard } from "@/features/hotkeys/useChatHotkeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { runAction } from "@/features/hotkeys/dispatch";
import { requestClaim, requestDecline } from "@/features/hotkeys/actionBus";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";

const claimFromQueue = vi.fn();
const navigate = vi.fn();
let очередь: Array<{ id: string; unread_count: number }> = [];

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});
vi.mock("@/features/chats/inbox/api", () => ({
  inboxRows: () => очередь,
  nextInboxId: () => очередь[0]?.id ?? null,
}));
const взятьПервогоССервера = vi.fn();
vi.mock("@/features/chats/inbox/claimFromQueue", () => ({
  claimFromQueue: (...a: unknown[]) => claimFromQueue(...a),
  взятьПервогоССервера: (...a: unknown[]) => взятьПервогоССервера(...a),
}));
vi.mock("@/features/hotkeys/actionBus", async () => {
  const actual = await vi.importActual<typeof import("@/features/hotkeys/actionBus")>(
    "@/features/hotkeys/actionBus",
  );
  return { ...actual, requestDecline: vi.fn(), requestClaim: vi.fn() };
});

/** Диалог, в котором менеджер сейчас печатает: он уже принят, в очереди его нет. */
const МОЙ = "aaaaaaaa-0000-0000-0000-000000000001";
/** Посторонний человек, ждущий в очереди. */
const ЧУЖОЙ = "bbbbbbbb-0000-0000-0000-000000000002";

function нажать(init: KeyboardEventInit) {
  // ⚠ ДВА ХУКА, А НЕ ОДИН. Заслон клавиши приёма 31.08 переехал из
  // `useChatHotkeys` (он живёт только на экране чатов) в раскладку
  // приложения — иначе `Ctrl+R` на настройках и статистике снова
  // перезагружал страницу. Оснастка обязана поднимать оба, как это делает
  // настоящее приложение.
  renderHook(() => {
    useChatHotkeys();
    useClaimKeyGuard();
  });
  window.dispatchEvent(new KeyboardEvent("keydown", { ...init, bubbles: true, cancelable: true }));
}

function поле(): HTMLTextAreaElement {
  const el = document.createElement("textarea");
  document.body.appendChild(el);
  el.focus();
  return el;
}

function состояние(набрано: string, extra: Record<string, unknown> = {}) {
  useChatUiStore.setState({
    activeConversationId: МОЙ,
    inboxOpen: false,
    filters: { tab: "mine" },
    drafts: набрано ? { [МОЙ]: { text: набрано, isNote: false } } : {},
    ...extra,
  });
}

describe("открытый диалог держится, пока в поле есть набранное", () => {
  beforeEach(() => {
    // Сочетания включены явно: с 02.09 они выключены по умолчанию, а этот набор
    // проверяет САМО ДЕЙСТВИЕ, а не то, включено ли оно из коробки.
    включитьВсеСочетания();
    document.body.innerHTML = "";
    queryClient.clear();
    /*
     * ⚠ СПИСОК НАПОЛНЯЕТСЯ ВСЕГДА, А НЕ ТОЛЬКО ТАМ, ГДЕ ОЖИДАЕТСЯ ПЕРЕХОД.
     *
     * Первая версия этого файла клала строки лишь в одну проверку — и проверка
     * «шаг по списку при набранном тексте диалог не меняет» оставалась зелёной
     * даже со снятым заслоном: уходить было просто некуда, кэш пуст. Она
     * сторожила пустоту. Заметила это только диверсия.
     */
    queryClient.setQueryData(qk.conversations.list({ tab: "mine" }), {
      pages: [
        {
          items: [
            { id: МОЙ, unread_count: 0 },
            { id: ЧУЖОЙ, unread_count: 3 },
          ],
          page: { limit: 50, has_more: false, next_cursor: null },
        },
      ],
      pageParams: [null],
    });
    claimFromQueue.mockClear();
    navigate.mockClear();
    vi.mocked(requestDecline).mockClear();
    vi.mocked(requestClaim).mockClear();
    useInboxStore.setState({ ids: {} } as never);
    взятьПервогоССервера.mockClear();
    очередь = [{ id: ЧУЖОЙ, unread_count: 1 }];
  });

  it("приём из поля ввода не открывает постороннего клиента", () => {
    /*
     * ⚠ ГЛАВНАЯ ПРОВЕРКА — ровно боевая жалоба: менеджер дописал ответ своему
     * клиенту и нажал привычное «отправить».
     */
    состояние("Здравствуйте, мастер подъедет");
    поле();
    нажать({ key: "r", code: "KeyR", ctrlKey: true });

    expect(
      claimFromQueue,
      "приняли и открыли постороннего человека — менеджер допишет ответ уже не тому",
    ).not.toHaveBeenCalled();
    expect(navigate, "экран увели из диалога, в котором печатают").not.toHaveBeenCalled();
  });

  it("с пустым полем приём берёт следующего из очереди — в любой вкладке", () => {
    /*
     * ⚠ ПРЕЖНЯЯ РЕДАКЦИЯ ТРЕБОВАЛА ОБРАТНОГО — «с открытым диалогом не прыгать
     * никогда». 30.08 владелец решение переподтвердил: «диалог должен браться
     * через комбинацию клавиш в любой вкладке». Настоящая беда утреннего
     * разбора (уехал посреди набора, ответ ушёл не тому) закрыта не запретом
     * прыжка, а замком на недописанном — он проверяется соседним тестом.
     */
    состояние("");
    нажать({ key: "r", code: "KeyR", ctrlKey: true });

    expect(claimFromQueue, "очередь снова не разобрать из открытого диалога").toHaveBeenCalledWith(
      ЧУЖОЙ,
      expect.anything(),
    );
  });

  it("открытый диалог из очереди принимается сам — без прыжка", () => {
    // Человек смотрит на ждущего — его и берёт; признак тот же, что у панели
    // решения (слово сервера/стор), а не кэш REST-выдачи.
    useInboxStore.setState({ ids: { [МОЙ]: true } } as never);
    состояние("");
    нажать({ key: "r", code: "KeyR", ctrlKey: true });

    expect(requestClaim, "открытый ждущий диалог не принялся").toHaveBeenCalled();
    expect(claimFromQueue, "вместо открытого взяли постороннего").not.toHaveBeenCalled();
  });

  it("шаг по списку при набранном тексте диалог не меняет", () => {
    состояние("Уточню у мастера и вернусь");
    for (const [key, mod] of [
      ["ArrowDown", "ctrlKey"],
      ["ArrowUp", "ctrlKey"],
      ["ArrowDown", "altKey"],
      ["ArrowUp", "altKey"],
    ] as const) {
      navigate.mockClear();
      нажать({ key, [mod]: true });
      expect(navigate, `${mod} + ${key} унесло из недописанного ответа`).not.toHaveBeenCalled();
    }
  });

  it("с пустым полем шаг по списку работает как раньше", () => {
    // Заслон обязан молчать ровно там, где есть что терять, а не отнимать
    // клавиатуру вовсе: списком ходят весь день.
    состояние("");
    // Сочетание действующее: с 09.09 листание — это `Ctrl + ↓` (голая стрелка
    // снята с умолчаний, чтобы не отнимать прокрутку). Жать снятое значило бы
    // проверять тишину.
    нажать({ key: "ArrowDown", ctrlKey: true });
    expect(navigate, "клавиатурная навигация по списку перестала работать").toHaveBeenCalled();
  });

  it("приём работает, когда кэш очереди пуст (боевой сбой 30.08)", () => {
    /*
     * ⚠ РЕГРЕССИЯ, СЛОМАВШАЯ СМЕНУ. Ветка приёма спрашивала кэш выдачи
     * `GET /inbox`. У диспетчера, не открывавшего «Входящие» в этой вкладке,
     * кэша нет — и открытый, вполне берущийся диалог в нём не находился.
     * Действие возвращало «ничего не сделал», обработчик клавишу не гасил, и
     * Ctrl+R доставался браузеру: страница перезагружалась, диалог оставался
     * непринятым. Жалоба дословно: «ребята не могут принимать диалоги, и
     * сейчас просто обновляется страница».
     */
    очередь = []; // кэша REST-выдачи нет вовсе
    useInboxStore.setState({ ids: { [МОЙ]: true } } as never); // но диалог ждёт — это знает стор
    состояние("");
    runAction("claim", { navigate, rows: () => [], can: () => true });

    expect(
      requestClaim,
      "приём снова зависит от кэша очереди — Ctrl+R перезагрузит страницу вместо принятия",
    ).toHaveBeenCalled();
  });

  it("Ctrl+R не достаётся браузеру, даже если делать было нечего", () => {
    /*
     * Второй замок, независимый от первого: любая будущая ветка, вернувшая
     * «ничего не сделал», иначе воспроизвела бы сбой заново. Перезагрузка
     * осталась на F5, а перезагрузка ВМЕСТО ожидаемого действия стоит человеку
     * набранного текста.
     */
    очередь = [];
    состояние("", { activeConversationId: null });
    renderHook(() => {
      useChatHotkeys();
      useClaimKeyGuard();
    });
    const событие = new KeyboardEvent("keydown", {
      key: "r",
      code: "KeyR",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    window.dispatchEvent(событие);

    expect(
      событие.defaultPrevented,
      "клавиша ушла браузеру — страница перезагрузится посреди работы",
    ).toBe(true);
  });

  it("Ctrl+R гасится даже под открытым окном", () => {
    /*
     * ⚠ ПОВТОР БОЕВОГО СБОЯ 30.08: «Ctrl+R так и обновляет страницу». Первая
     * починка была верной, но до неё можно было не дойти: общий обработчик
     * первым делом молчит, если поверх экрана лежит слой (`role="dialog"` —
     * поповер шаблонов, просмотр картинки, любое окно Mantine). Для обычной
     * клавиши это безобидно, для этой означает перезагрузку рабочего места.
     */
    const слой = document.createElement("div");
    слой.setAttribute("role", "dialog");
    document.body.appendChild(слой);
    состояние("", { activeConversationId: null });
    renderHook(() => {
      useChatHotkeys();
      useClaimKeyGuard();
    });
    const e = new KeyboardEvent("keydown", {
      key: "r", code: "KeyR", ctrlKey: true, bubbles: true, cancelable: true,
    });
    window.dispatchEvent(e);
    expect(e.defaultPrevented, "под открытым окном клавиша ушла браузеру").toBe(true);
  });

  it("Ctrl+R гасится, даже если всплытие остановлено", () => {
    /*
     * Второй обход: слушатель общего разбора висит на окне в фазе всплытия, и
     * любой обработчик по дороге может событие остановить — тогда до нас оно
     * не доедет вовсе. Заслон приёма живёт на ПЕРЕХВАТЕ, и остановкой
     * всплытия его не отменить.
     */
    состояние("", { activeConversationId: null });
    renderHook(() => {
      useChatHotkeys();
      useClaimKeyGuard();
    });
    const поле = document.createElement("input");
    document.body.appendChild(поле);
    поле.addEventListener("keydown", (ev) => ev.stopPropagation());
    const e = new KeyboardEvent("keydown", {
      key: "r", code: "KeyR", ctrlKey: true, bubbles: true, cancelable: true,
    });
    поле.dispatchEvent(e);
    expect(e.defaultPrevented, "остановленное всплытие снова отдаёт клавишу браузеру").toBe(true);
  });

  it("без кэша очереди клавиша спрашивает сервер — «не заходя во Входящие»", () => {
    /*
     * ⚠ ТРЕТЬЕ ВОЗВРАЩЕНИЕ ЖАЛОБЫ 30.08: «нужно кликать на диалог во входящих
     * и затем нажимать Ctrl+R, а я хочу принимать без клика мышки и не заходя
     * во вкладку входящие». Первый ждущий брался из кэша выдачи /inbox, а кэш
     * появляется только при открытии вкладки — у работающего из «Моих» клавише
     * было неоткуда узнать, кто ждёт, и она молчала.
     */
    очередь = []; // кэш пуст: «Входящие» не открывали ни разу
    состояние("", { activeConversationId: null });
    runAction("claim", { navigate, rows: () => [], can: () => true });

    expect(
      взятьПервогоССервера,
      "клавиша снова молчит без открытой вкладки «Входящие»",
    ).toHaveBeenCalled();
  });

  it("и из открытого чужого диалога с пустым полем — тоже к серверу", () => {
    очередь = [];
    состояние(""); // открыт МОЙ, не в очереди, поле пустое
    runAction("claim", { navigate, rows: () => [], can: () => true });

    expect(взятьПервогоССервера).toHaveBeenCalled();
  });

  it("недописанный ответ держит и при открытой очереди", () => {
    /*
     * ⚠ ЕДИНСТВЕННЫЙ ЗАМОК НА ПРЫЖОК — НАБРАННОЕ, и он не зависит от
     * `inboxOpen`. Первая версия замка опиралась на этот флаг и была дырявой:
     * он не сбрасывается после приёма и у разбирающего очередь включён всю
     * смену.
     */
    состояние("Здравствуйте, мастер подъедет", { inboxOpen: true });
    runAction("claim", { navigate, rows: () => очередь, can: () => true });
    expect(claimFromQueue, "прыжок на постороннего поверх недописанного ответа").not.toHaveBeenCalled();
  });

  it("разбор очереди с клавиатуры цел (просьба владельца 22.08)", () => {
    // «Следующий в очереди» — отдельное действие, заведённое ровно для того,
    // чтобы брать следующего, не отрывая рук от клавиатуры.
    состояние("", { inboxOpen: true });
    runAction("queueNext", { navigate, rows: () => очередь, can: () => true });
    expect(navigate, "очередь снова не разобрать без мыши").toHaveBeenCalledWith(`/chats/${ЧУЖОЙ}`);
  });

  it("отказ не срабатывает с набранным ответом даже вне поля ввода", () => {
    /*
     * ⚠ САМЫЙ КОВАРНЫЙ ИЗ НАЙДЕННЫХ ПУТЕЙ. Отказ закрыт признаком «не работает
     * при наборе», но тот смотрит на ФОКУС — а фокус теряется не по своей
     * воле: сервер сам возвращает диалог в очередь (протухший presence, три
     * минуты без пинга), поле ввода подменяется панелью «Принять/Отклонить»,
     * и фокус падает на страницу. Человек в этот момент печатает и жмёт
     * Ctrl+Backspace — «стереть слово», — а попадает в отказ от диалога с
     * переходом к другому клиенту.
     */
    состояние("Здравствуйте, мастер подъедет");
    нажать({ key: "Backspace", ctrlKey: true });
    expect(requestDecline, "отказ прошёл поверх недописанного ответа").not.toHaveBeenCalled();
  });

  it("закрытие диалога не уводит с недописанного ответа", () => {
    /*
     * Закрытие висит на Mod+Shift+Enter — это промах мимо Shift+Enter у
     * человека, чей палец уже лежит на Ctrl по привычке «Ctrl+Enter —
     * отправить». Само закрытие оставлено: человек мог и правда закрыть
     * диалог, не дописав. Отнят переход — он и был «самовольным
     * переключением на другого клиента».
     */
    const исходник = readFileSync("src/features/chats/hooks/useConversationActions.ts", "utf-8") as string;
    expect(
      исходник,
      "переход после закрытия снова безусловный — набранное останется в брошенном диалоге",
    ).toMatch(/if \(next && !набранное\) navigate\(/);
  });

  it("приём с пустого экрана цел (просьба владельца 28.08)", () => {
    состояние("", { activeConversationId: null });
    runAction("claim", { navigate, rows: () => очередь, can: () => true });
    expect(claimFromQueue, "с пустого экрана диалог больше не принять").toHaveBeenCalled();
  });
});
