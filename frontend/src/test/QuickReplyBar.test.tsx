import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
import { QuickReplyBar } from "@/features/chats/components/composer/QuickReplyBar";
import { Composer } from "@/features/chats/components/composer/Composer";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";
import { qk } from "@/shared/api/queryKeys";
import type { TemplateDto } from "@/shared/api/types";

/**
 * БЫСТРЫЕ ОТВЕТЫ ПОЯВЛЯЮТСЯ САМИ.
 *
 * ⚠ ЧТО БЫЛО. Заготовки открывались только по нажатию — `⚡` или `/` в пустом
 * поле. Про это надо знать, а узнать неоткуда: подсказки на экране нет. В бою
 * получилось так: один диспетчер написал пятнадцать заготовок и пользовался
 * ими, остальные о них не подозревали. Просьба владельца дословно: «хочу,
 * чтобы быстрые ответы сами появлялись, у каждого свои».
 *
 * «У КАЖДОГО СВОИ» — решение владельца: общими эти пятнадцать не делаем.
 * Заготовка отражает манеру конкретного человека, и чужая фраза в своей ленте
 * читается как чужой голос. Список тот же, что у пикера («свои личные + все
 * общие»), так что общие, если их заведут, появятся здесь сами.
 */

const ШАБЛОНЫ: TemplateDto[] = [
  { id: "t1", owner_id: "u1", title: "Выезд сегодня", body: "Приедем сегодня, {{clientName}}", folder: null },
  { id: "t2", owner_id: "u1", title: "Цена замены экрана", body: "Замена экрана — от 8900 ₽", folder: null },
];

function стенд(items: TemplateDto[], visible = true, onPick = vi.fn(), typed = "Выез") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        ({
          ok: true,
          status: 200,
          json: async () => ({ items, page: { limit: 50, offset: 0, total: items.length } }),
        }) as unknown as Response,
    ),
  );
  render(
    <QueryClientProvider client={qc}>
      <MantineProvider>
        <QuickReplyBar convId="conv-1" visible={visible} typed={typed} onPick={onPick} />
      </MantineProvider>
    </QueryClientProvider>,
  );
  return onPick;
}

describe("Полоса быстрых ответов", () => {
  it("НАД ПУСТЫМ ПОЛЕМ ПОЛОСЫ НЕТ — она подсказка, а не список", async () => {
    /*
     * ⚠ ВТОРАЯ РЕДАКЦИЯ ПРАВИЛА (28.08, вечер). Сперва полоса показывалась и
     * над пустым полем, подбирая ответы к последним словам клиента. Владелец
     * увидел это в работе: «сейчас они перекрывают диалоги, когда открываешь
     * диалог… условно пишу „Здра" — только тогда появляются подсказки».
     *
     * Он прав: человек только что открыл диалог и читает переписку, а ему на
     * неё кладут список, которого он не звал.
     */
    стенд(ШАБЛОНЫ, true, vi.fn(), "");
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();
  });

  it("одной буквы мало: подсказка начинается со второй", async () => {
    /*
     * Одна буква совпадает почти со всем и превращает подсказку в помеху на
     * первом же нажатии. Две — это уже намерение.
     */
    стенд(ШАБЛОНЫ, true, vi.fn(), "В");
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();
  });

  it("с двух букв подсказка появляется", async () => {
    стенд(ШАБЛОНЫ, true, vi.fn(), "Вы");
    expect(await screen.findByRole("option", { name: /Выезд сегодня/ })).toBeTruthy();
  });

  it("нажатие отдаёт шаблон вставляющему — подстановка идёт тем же путём", async () => {
    /*
     * Полоса САМА текст не подставляет: вставку делает композер
     * (`insertTemplate`), и переменные — имя клиента, объявление — подставляются
     * там же, что и у пикера. Второй путь вставки означал бы вторую логику
     * подстановки, а она уже однажды разъезжалась.
     */
    const onPick = стенд(ШАБЛОНЫ);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("option", { name: /Выезд сегодня/ }));
    expect(onPick).toHaveBeenCalledWith(ШАБЛОНЫ[0]);
  });

  it("начало текста ВИДНО в строке: похожие заготовки различают до вставки", async () => {
    /*
     * Было всплывающей подсказкой `title` — а её надо ждать секунду наведением,
     * и на сенсорном экране её нет вовсе. Раз строка списка широкая, начало
     * текста помещается прямо в неё и читается сразу.
     */
    стенд(ШАБЛОНЫ);
    const строка = await screen.findByRole("option", { name: /Выезд сегодня/ });
    expect(строка.textContent).toContain("Приедем сегодня");
  });

  it("поле не пустое — полосы нет: она мешает тому, кто уже пишет", () => {
    /*
     * ⚠ ПЕРВАЯ РЕДАКЦИЯ ЭТОЙ ПРОВЕРКИ ЗЕЛЕНЕЛА ПО НЕВЕРНОЙ ПРИЧИНЕ. Она просто
     * передавала `visible={false}`, а тогда выключается и сам запрос: список
     * пуст, и полосы нет из-за пустого списка, а не из-за скрытия. Диверсия
     * «убрать проверку `!visible`» такую проверку не роняла.
     *
     * Поэтому кэш наполняем ЗАРАНЕЕ: заготовки есть, а полосы всё равно быть не
     * должно.
     */
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(qk.templates.list("all"), {
      items: ШАБЛОНЫ,
      page: { limit: 50, offset: 0, total: ШАБЛОНЫ.length },
    });
    render(
      <QueryClientProvider client={qc}>
        <MantineProvider>
          <QuickReplyBar convId="conv-1" visible={false} typed="" onPick={vi.fn()} />
        </MantineProvider>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();
    expect(screen.queryByRole("option", { name: /Выезд сегодня/ })).toBeNull();
  });

  it("ПРОВОДКА: полоса появляется в композере и молчит, пока открыт список «/»", async () => {
    /*
     * ⚠ ЭТА ПРОВЕРКА БЫЛА ПО ТЕКСТУ ИСХОДНИКА и сверяла условие показа
     * буквально: `visible={!isNote && !pickerOpen}`. Такая проверка краснеет
     * на переименовании и молчит на поломке — в этом проекте тесты по тексту
     * исходника уже пропускали дефекты. Проверяем то же самое поведением.
     *
     * Без проводки все остальные проверки полосы зеленеют впустую: её можно
     * вырезать из композера, и на экране её не будет при зелёном файле.
     */
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
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/templates")
          ? jsonResponse(200, {
              items: [
                { id: "t9", owner_id: null, title: "Приветствие", body: "Здравствуйте!", folder: null },
                // Косая в ТЕЛЕ заготовки — чтобы «/18» нашлось и полосой тоже.
                { id: "t8", owner_id: null, title: "Часы", body: "Работаем 9/18 без выходных", folder: null },
              ],
              page: { limit: 200, offset: 0, total: 2 },
            })
          : jsonResponse(404, { error: { code: "not_found", message: "нет" } }),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const input = screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;

    await user.click(input);
    await user.type(input, "Здра");
    // Полоса подсказок жива и видит НАБРАННОЕ слово.
    const полоса = await screen.findByRole("listbox", { name: "Быстрые ответы" });
    expect(within(полоса).getByRole("option", { name: /Приветствие/ })).toBeInTheDocument();

    /*
     * ⚠ ДВА СПИСКА ОДНИХ И ТЕХ ЖЕ ЗАГОТОВОК НА ЭКРАНЕ — ЭТО ДЕФЕКТ. Стрелки
     * двигали бы метку сразу в обоих, а Enter вставлял бы не то, на что
     * человек смотрит. Команда «/» открывает свой список — полоса обязана
     * замолчать.
     *
     * ⚠ КОМАНДА ЗДЕСЬ «/18», И ЭТО НЕ ПРИДИРКА. Первая редакция проверки
     * набирала «/прив»: полоса и без всякого заслона молчала бы, потому что
     * слова «/прив» нет ни в одной заготовке. Диверсия — снять заслон —
     * проходила насквозь. «/18» же есть в теле («Работаем 9/18»), так что
     * полоса нашла бы совпадение и показалась, не запрети мы ей этого.
     */
    await user.type(input, " /18");
    expect(await screen.findByRole("dialog", { name: "Быстрые ответы" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull());
  });

  it("во время набора подсказка сужается по НАБРАННОМУ", async () => {
    /*
     * Просьба владельца: «чтобы он в момент моего ввода сообщений предлагал
     * варианты ответов». Оператор набирает НАЧАЛО того, что хочет сказать, и
     * часто это две-три буквы — поэтому ищем вхождением подстроки, а не по
     * темам: тема нужна там, где клиент пишет целыми фразами.
     */
    стенд(ШАБЛОНЫ, true, vi.fn(), "экран");
    expect(await screen.findByRole("option", { name: /Цена замены экрана/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /Выезд сегодня/ })).toBeNull();
  });

  it("под набранное ничего не подошло — молчим, а не показываем похожее", async () => {
    /*
     * Человек пишет своё. Подсказка, висящая над каждым словом, — это помеха, а
     * не помощь: она закрывает ленту и отвлекает от собственной мысли.
     */
    стенд(ШАБЛОНЫ, true, vi.fn(), "ключи от квартиры");
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();
  });

  it("список короткий: не больше пяти строк", async () => {
    /*
     * ⚠ ПЕРВАЯ РЕДАКЦИЯ ВЫВАЛИВАЛА ВСЕ ЗАГОТОВКИ. У диспетчера их полтора
     * десятка: ряд переносился на три строки и отнимал у переписки заметный
     * кусок экрана. «По факту он вываливает все ответы, которые были».
     */
    const много = Array.from({ length: 12 }, (_, i) => ({
      id: `m${i}`,
      owner_id: "u1",
      title: `Заготовка ${i}`,
      body: "текст",
      folder: null,
    }));
    стенд(много, true, vi.fn(), "Заго");
    await screen.findByRole("listbox", { name: "Быстрые ответы" });
    expect(screen.getAllByRole("option").length).toBeLessThanOrEqual(5);
  });

  it("заготовок нет — пустой полосы тоже нет", async () => {
    стенд([]);
    // Ждём ответа запроса, иначе проверяем момент до загрузки.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();
  });
});
