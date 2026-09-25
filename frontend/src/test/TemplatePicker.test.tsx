import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { applyTemplateVars } from "@/features/templates/vars";
import type { TemplateDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

const TEMPLATES: TemplateDto[] = [
  {
    id: "t1",
    owner_id: null,
    title: "Приветствие",
    body: "Здравствуйте, {имя}! Это {менеджер} из сервиса Lead Partner",
    folder: "Приветствия",
  },
  { id: "t2", owner_id: null, title: "Доставка", body: "Курьер приедет завтра", folder: "Доставка" },
  { id: "t3", owner_id: fakeUser.id, title: "Моя скидка", body: "Скидка 5% при заказе сегодня", folder: null },
  // Ходовая: в бою «Здравствуйте» с 586 применениями стояло 32-й строкой из 64.
  { id: "t4", owner_id: null, title: "Ходовая", body: "Куда к вам подъехать?", folder: "Яяя-последняя", used_count: 586 },
];

describe("TemplatePickerPopover — быстрые ответы (11 §3.1, 10 §5.1)", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/templates")) {
          return jsonResponse(200, { items: TEMPLATES, page: { limit: 200, offset: 0, total: 3 } });
        }
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function открытьПикер() {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    await user.click(screen.getByLabelText("Текст сообщения"));
    await user.keyboard("/");
    await screen.findByRole("dialog", { name: "Быстрые ответы" });
    return user;
  }

  it("«/» печатается в поле и сама поднимает список; косая внутри слова — обычный символ", async () => {
    /*
     * ⚠ ДОГОВОР ИЗМЕНЁН 03.09 ПО ПРОСЬБЕ ВЛАДЕЛЬЦА («сделай точный аналог,
     * который был сделан на Jivo»). Раньше «/» перехватывалась и в поле НЕ
     * попадала, а запрос набирался второй раз в отдельной строке попапа.
     * Теперь «/» — обычный знак: она видна в сообщении, а список поднимается
     * потому, что под кареткой появилась команда.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;
    await user.click(input);
    await user.keyboard("/");

    expect(await screen.findByRole("dialog", { name: "Быстрые ответы" })).toBeInTheDocument();
    expect(input.value).toBe("/"); // знак остался в сообщении — его видно

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Быстрые ответы" })).toBeNull());

    // Косая ВНУТРИ слова командой не считается: так пишут адреса и часы работы.
    await user.clear(input);
    await user.type(input, "Ленина 5/2");
    expect(input.value).toBe("Ленина 5/2");
    expect(screen.queryByRole("dialog", { name: "Быстрые ответы" })).toBeNull();
  });

  it("набор ПРЯМО В ПОЛЕ фильтрует список, Enter вставляет шаблон вместо команды", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;
    await user.click(input);
    await user.keyboard("/");

    await screen.findByRole("option", { name: /Приветствие/ });
    expect(screen.getByRole("option", { name: /Доставка/ })).toBeInTheDocument();

    // Фильтр идёт по тому, что набрано в САМОМ сообщении — второй строки нет.
    await user.keyboard("привет");
    expect(input.value).toBe("/привет");
    await waitFor(() => expect(screen.queryByRole("option", { name: /Доставка/ })).toBeNull());

    await user.keyboard("{Enter}");

    // {имя} → имя клиента, {менеджер} → своё full_name (01 §7); «/привет» стёрто.
    await waitFor(() => {
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe(
        "Здравствуйте, Иван Петров! Это Анна Смирнова из сервиса Lead Partner",
      );
    });
    expect(screen.queryByRole("dialog", { name: "Быстрые ответы" })).toBeNull();
  });

  it("подсказка жива и во втором абзаце, и посреди текста", async () => {
    /*
     * ⚠ ДВА ЖИВЫХ ДЕФЕКТА ОДНОЙ ПРИЧИНЫ (03.09). Запрос подсказки брался «от
     * последнего ПРОБЕЛА до конца поля».
     *
     *   1. После Shift+Enter «последним словом» становилась вся предыдущая
     *      строка вместе с переносом — совпасть с заготовкой она не могла
     *      никогда, и в сообщении из двух абзацев подсказки не было вовсе.
     *   2. Слово бралось с конца поля, а стиралось перед КАРЕТКОЙ: Tab
     *      посреди текста съедал соседние буквы.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;

    await user.click(input);
    await user.type(input, "Первый абзац{Shift>}{Enter}{/Shift}Привет");
    const полоса = await screen.findByRole("listbox", { name: "Быстрые ответы" });
    expect(within(полоса).getByRole("option", { name: /Приветствие/ })).toBeInTheDocument();
  });

  it("Tab посреди текста заменяет слово ПОД КАРЕТКОЙ, не трогая соседей", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;

    await user.click(input);
    await user.type(input, "аа привет д");
    await user.keyboard("{ArrowLeft>2/}"); // каретка сразу после «привет»
    await screen.findByRole("listbox", { name: "Быстрые ответы" });
    await user.keyboard("{Tab}");

    await waitFor(() => {
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe(
        "аа здравствуйте, Иван Петров! Это Анна Смирнова из сервиса Lead Partner д",
      );
    });
  });

  it("каретка ушла из команды — список закрывается", async () => {
    /*
     * Команда — это то, что под курсором, а не режим, в который вошли. Уведи
     * каретку в начало строки — и панель, висящая поверх переписки, относится
     * уже ни к чему.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;
    await user.click(input);
    await user.type(input, "/дост");
    expect(await screen.findByRole("dialog", { name: "Быстрые ответы" })).toBeInTheDocument();

    await user.keyboard("{ArrowLeft>5/}"); // каретка перед «/»
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Быстрые ответы" })).toBeNull());
    expect(input.value).toBe("/дост"); // набранное на месте — закрылась только панель
  });

  it("команда, ВСТАВЛЕННАЯ из буфера, тоже поднимает список", async () => {
    /*
     * ⚠ ЭТА ПРОВЕРКА СТОИТ ЗДЕСЬ ИЗ-ЗА ПРОВАЛИВШЕЙСЯ ДИВЕРСИИ. Пересчёт команды
     * висит на двух событиях поля: `onChange` (значение изменилось) и
     * `onSelect` (каретка поехала). Снял `onChange` — все проверки остались
     * зелёными: React объявляет `onSelect` и по `keyup`, то есть при наборе с
     * клавиатуры второе событие подменяло первое, и заслон только КАЗАЛСЯ
     * проверенным.
     *
     * Вставка из буфера отличает их честно: значение меняется, а клавиш никто
     * не нажимал. Так люди и работают — заготовку копируют из соседнего окна.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;
    await user.click(input);
    await user.paste("/дост");

    expect(input.value).toBe("/дост");
    await screen.findByRole("option", { name: /Доставка/ });
  });

  it("команда посреди сообщения заменяется на месте, не трогая набранное вокруг", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;
    await user.click(input);
    await user.type(input, "Хорошо, /дост");
    await screen.findByRole("option", { name: /Доставка/ });
    await user.keyboard("{Enter}");

    /*
     * ⚠ И РЕГИСТР ПОДОГНАН: тело «Курьер приедет завтра» встаёт в середину
     * фразы со строчной. Заглавная посреди предложения уходила клиенту 386 раз
     * за тридцать суток — см. `features/templates/склейка`.
     */
    await waitFor(() => {
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe("Хорошо, курьер приедет завтра");
    });
  });

  it("кнопка ⚡ открывает тот же пикер, ↓ двигает выделение", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.click(screen.getByRole("button", { name: "Быстрые ответы" }));
    await screen.findByRole("option", { name: /Приветствие/ });

    // Активна первая строка списка; ↓ переносит выделение на следующую.
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowDown}");
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "false");
    expect(screen.getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
  });

  it("недописанный черновик НЕ открывает полосу подсказок сам собой", async () => {
    /*
     * ⚠ ПРАВИЛО ПОЛОСЫ — «ТОЛЬКО ВО ВРЕМЯ НАБОРА» (решение владельца 28.08:
     * «сейчас они перекрывают диалоги, когда открываешь диалог»). Считалась же
     * она по содержимому поля, а поле переживает уход из диалога. Возвращаешься
     * к недописанному ответу — и список ложится поверх переписки, которую
     * пришёл перечитать, хотя ни одной клавиши ещё не нажато.
     */
    const user = userEvent.setup();
    useChatUiStore.setState({
      drafts: { [CONV_ID]: { text: "Здра", isNote: false } },
      activeConversationId: CONV_ID,
    });
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    // Дать запросу заготовок доехать: молчание должно быть решением, а не гонкой.
    await screen.findByLabelText("Текст сообщения");
    await waitFor(() => expect(queryClient.getQueryData(["templates", "all"])).toBeDefined(), {
      timeout: 2000,
    });
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();

    await user.click(screen.getByLabelText("Текст сообщения"));
    await user.keyboard("в");
    expect(await screen.findByRole("listbox", { name: "Быстрые ответы" })).toBeInTheDocument();
  });

  it("пикер «⚡» ЗАМЕНЯЕТ набранное слово, если заготовка под него подходит", async () => {
    /*
     * ⚠ ЗАМЕР БОЯ: около сорока сообщений в месяц вида «по По цене так сразу
     * не скажу…». Человек начал набирать «дост», не нашёл нужного в подсказке,
     * открыл пикер и выбрал — а набранное осталось перед вставленным и ушло
     * клиенту.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;

    await user.click(input);
    await user.type(input, "дост");
    await user.click(screen.getByRole("button", { name: "Быстрые ответы" }));
    await user.click(await screen.findByRole("option", { name: /Доставка/ }));

    await waitFor(() =>
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe("Курьер приедет завтра"),
    );
  });

  it("а слово, к заготовке не относящееся, остаётся на месте", async () => {
    /*
     * Обратная половина того же правила. Пикер показывает ВСЁ, а не только
     * подошедшее: «я» здесь не недописанный запрос, а слово сообщения, и
     * стереть его значило бы испортить написанное человеком.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;

    await user.click(input);
    await user.type(input, "Добрый день, я");
    await user.click(screen.getByRole("button", { name: "Быстрые ответы" }));
    await user.click(await screen.findByRole("option", { name: /Доставка/ }));

    await waitFor(() =>
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe(
        "Добрый день, я курьер приедет завтра",
      ),
    );
  });

  it("подстановка: клиент без имени → «Клиент», непроставляемая переменная остаётся плейсхолдером", () => {
    expect(
      applyTemplateVars("Здравствуйте, {имя}! По объявлению «{объявление}»", {
        clientName: null,
        managerName: "Анна",
      }),
    ).toBe("Здравствуйте, Клиент! По объявлению «{объявление}»");
  });

  it("ходовые подняты отдельной группой наверх", async () => {
    /*
     * ⚠ ЗАМЕР БОЯ 03.09. Пикер сортировал строго по алфавиту и выбрасывал
     * частоту, которую сам же копит: «Здравствуйте» с 586 применениями стояло
     * 32-й строкой из 64, а в окно помещается пять. До самой ходовой заготовки
     * надо было прокрутить шесть экранов — при том что верхние восемь дают
     * 57 % всех вставок.
     */
    await открытьПикер();
    const группы = screen.getAllByText(/^ЧАСТЫЕ$|^ОБЩИЕ|^МОИ/);
    expect(группы[0].textContent, "группа частых не первая").toBe("ЧАСТЫЕ");

    const строки = screen.getAllByRole("option").map((el) => el.textContent ?? "");
    expect(строки[0], "ходовая заготовка не первая").toContain("Ходовая");
  });

  it("ходовая не задваивается в своей папке", async () => {
    /*
     * ⚠ ДУБЛЬ ЗДЕСЬ — НЕ КОСМЕТИКА. Порядок обхода стрелками считается по
     * плоскому списку через `indexOf`: появись строка дважды, метка подсветит
     * обе, а Enter вставит первую. Человек стоит на одной, клиенту уходит
     * другая — ровно тот класс ошибки, что уже разбирали здесь для фокуса.
     */
    await открытьПикер();
    const ходовые = screen.getAllByRole("option").filter((el) => (el.textContent ?? "").includes("Ходовая"));
    expect(ходовые).toHaveLength(1);
  });

  it("при поиске группа частых не спорит с запросом", async () => {
    /*
     * Человек, который набрал запрос, уже сказал, что ему нужно. Поднимать
     * поверх его слов что-то своё значит спорить с ним.
     */
    const user = await открытьПикер();
    /*
     * ⚠ ЗАПРОС ОБЯЗАН НАХОДИТЬ САМУ ХОДОВУЮ. Первая редакция искала «курьер»,
     * которого у ходовой нет: группа выходила пустой по другой причине, и
     * диверсия «частые лезут и в поиск» проходила насквозь. Здесь ищется
     * слово из тела ходовой — значит, появись группа, тест её увидит.
     */
    await user.keyboard("подъехать");
    await screen.findByRole("option", { name: /Ходовая/ });
    expect(screen.queryByText("ЧАСТЫЕ"), "группа частых спорит с запросом человека").toBeNull();
  });
});