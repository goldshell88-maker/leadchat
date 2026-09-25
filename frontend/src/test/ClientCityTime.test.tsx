import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto } from "@/shared/api/types";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * Город клиента и время в нём (требования владельца 3-4 от 11 августа).
 *
 * «Непонятно, на какое объявление пишет клиент, из-за этого я не знаю город»
 * и «хочу, чтобы после установления города показывалось время города, по
 * которому пишет клиент». Смысл второго — чтобы диспетчер не набрал номер в
 * три ночи: от Керчи до Петропавловска девять часов разницы.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - убрали `<ItemFactsLine>` из карточки — падает «показывает город и время»;
 *  - в `useCityClock` выбросили `timeZone: tz` из Intl (время машины
 *    оператора вместо времени клиента) — падает тот же тест;
 *  - убрали проверку `if (!tz) return null` — падает «город без пояса
 *    показывается без времени»;
 *  - убрали `window.setInterval` — падает «часы идут сами»;
 *  - убрали `try/catch` вокруг Intl — падает «мусорный пояс не роняет
 *    карточку»;
 *  - нарисовали ссылку на профиль всегда, а не при непустом `profile_url` —
 *    падает «ссылки на профиль сегодня нет».
 */
describe("Карточка клиента — город и время у клиента", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          ({
            ok: true,
            status: 200,
            json: async () => ({ items: [] }),
          }) as Response,
      ),
    );
    // 20:40 UTC — в Москве 23:40, ровно пример из требования владельца.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-11T20:40:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function render(conversation: ConversationDetailDto) {
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conversation);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  function withCity(
    item: Partial<ConversationDetailDto["item"]>,
  ): ConversationDetailDto {
    return makeConversation({
      item: {
        title: "Ремонт телевизоров",
        url: "https://www.avito.ru/moskva/predlozheniya_uslug/remont_8213779975",
        price: "от 1500 ₽",
        ...item,
      },
    });
  }

  it("показывает город и время в нём", () => {
    render(
      withCity({
        city_slug: "moskva",
        city_name: "Москва",
        city_tz: "Europe/Moscow",
      }),
    );
    expect(screen.getByText(/23:40 у клиента/)).toBeInTheDocument();
    expect(screen.getByText(/Москва/)).toBeInTheDocument();
  });

  it("время считается в поясе КЛИЕНТА, а не оператора", () => {
    /*
     * Петропавловск-Камчатский — UTC+12: в те же 20:40 UTC там уже 08:40
     * следующего дня. Если время берётся с машины оператора, здесь будет
     * московское, и вся затея теряет смысл.
     */
    render(
      withCity({
        city_slug: "petropavlovsk-kamchatskiy",
        city_name: "Петропавловск-Камчатский",
        city_tz: "Asia/Kamchatka",
      }),
    );
    expect(screen.getByText(/08:40 у клиента/)).toBeInTheDocument();
  });

  it("часы идут сами, а не замирают на момент открытия", () => {
    /*
     * Карточку держат открытой часами. Время, посчитанное один раз, к вечеру
     * врёт сильнее, чем его отсутствие: оператор поверит и позвонит.
     */
    render(
      withCity({
        city_slug: "moskva",
        city_name: "Москва",
        city_tz: "Europe/Moscow",
      }),
    );
    expect(screen.getByText(/23:40 у клиента/)).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2 * 60 * 1000 + 1000);
    });
    expect(screen.getByText(/23:42 у клиента/)).toBeInTheDocument();
  });

  it("город без часового пояса показывается без времени, а не с угаданным", () => {
    /*
     * Города нет в справочнике — сервер честно отдаёт слаг без пояса. Час,
     * угаданный «по соседям», стоит звонка клиенту ночью; пустое место не
     * стоит ничего. Сам слаг показываем: сразу видно, что справочник надо
     * пополнить.
     */
    render(
      withCity({ city_slug: "zazerkalye", city_name: null, city_tz: null }),
    );
    // С 12 августа цена и город стоят ОДНОЙ серой строкой под названием
    // (правка 9: две строки вместо трёх), поэтому ищем вхождение, а не
    // точное совпадение узла.
    expect(screen.getByText(/zazerkalye/)).toBeInTheDocument();
    expect(screen.queryByText(/у клиента/)).not.toBeInTheDocument();
  });

  it("поля пояса нет вовсе (старый кадр WS) — времени нет, а не время оператора", () => {
    /*
     * САМЫЙ ОПАСНЫЙ ИЗ ПУСТЫХ СЛУЧАЕВ, и он не выдуман: во время выкатки в
     * кэше живут строки, пришедшие старым кадром WS, где полей города нет
     * вовсе — `city_tz` там `undefined`, а не `null`. Intl на `undefined`
     * НЕ БРОСАЕТ: он молча берёт пояс машины оператора и рисует уверенное
     * «23:40 у клиента» для Владивостока. Ошибка, которую невозможно
     * заметить глазами, — ровно та, ради которой всё это и делалось.
     */
    render(withCity({ city_slug: "vladivostok", city_name: "Владивосток" }));
    expect(screen.getByText(/Владивосток/)).toBeInTheDocument();
    expect(screen.queryByText(/у клиента/)).not.toBeInTheDocument();
  });

  it("мусорный часовой пояс не роняет карточку", () => {
    /*
     * Intl.DateTimeFormat на незнакомое имя бросает RangeError. Уронить им
     * карточку — худший исход: без карточки оператор не видит телефон.
     */
    render(
      withCity({
        city_slug: "moskva",
        city_name: "Москва",
        city_tz: "Europe/Moskow",
      }),
    );
    expect(screen.getByText(/Москва/)).toBeInTheDocument();
    expect(screen.queryByText(/у клиента/)).not.toBeInTheDocument();
  });

  it("без города строки нет вовсе", () => {
    // «Город: —» приучает не читать строку, и тогда она не сработает там, где нужна.
    render(withCity({ city_slug: null, city_name: null, city_tz: null }));
    expect(screen.queryByText(/у клиента/)).not.toBeInTheDocument();
    expect(screen.getByText(/Ремонт телевизоров/)).toBeInTheDocument();
  });
});

/**
 * Идентификатор Авито — ТРЕМЯ СТРОКАМИ МЕЛКИМ ШРИФТОМ В БЛОКЕ «КЛИЕНТ», а не
 * отдельной карточкой (правка 9 от 12 августа).
 *
 * Блок «Клиент на Авито» повторял имя клиента вторым экземпляром и, когда
 * имени не было, занимал целую карточку, чтобы сообщить «Авито не прислал имя
 * профиля». Идентификатор при этом отвечает на тот же вопрос, что имя и
 * телефон, — КТО ЭТОТ ЧЕЛОВЕК, — и живёт теперь там же.
 */
describe("Карточка клиента — идентификатор Авито", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          ({
            ok: true,
            status: 200,
            json: async () => ({ items: [] }),
          }) as Response,
      ),
    );
  });

  function renderWithClient(client: Partial<ConversationDetailDto["client"]>) {
    const conv = makeConversation();
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), {
      ...conv,
      client: { ...conv.client, ...client },
    });
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  it("показывает идентификатор с кнопкой копирования", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });

    renderWithClient({ external_id: "923456789", profile_url: null });

    // Отдельного блока больше нет — и это проверяется прямо здесь, иначе
    // семь блоков вернутся первой же правкой «а давайте вынесем обратно».
    expect(screen.queryByText("Клиент на Авито")).not.toBeInTheDocument();
    expect(screen.getByText("ID 923456789")).toBeInTheDocument();
    screen.getByLabelText("Скопировать идентификатор клиента на Авито").click();
    expect(writeText).toHaveBeenCalledWith("923456789");
  });

  it("ссылки ещё не спрашивали — подсказка обещает, что она появится", () => {
    /*
     * ⚠ ЭТА ПОДСКАЗКА ПЯТЬ МЕСЯЦЕВ ГОВОРИЛА НЕПРАВДУ. В ней стояло «Ссылку на
     * профиль Авито не отдаёт» — довод верный на день написания и опровергнутый
     * 02.09: Авито присылает `public_user_profile` с ключом `url`, и ссылка
     * доехала до 186 карточек из 186, про которые успели спросить.
     *
     * Пустая ссылка теперь значит одно из двух, и путать их нельзя. Здесь —
     * первый случай: про клиента ещё не спрашивали, вопрос уходит при открытии
     * диалога, и через секунду кнопка появится сама.
     */
    renderWithClient({
      external_id: "923456789",
      profile_url: null,
      profile_checked: false,
    });
    expect(screen.queryByText(/Профиль на Авито/)).not.toBeInTheDocument();
    const hint = screen.getByLabelText(/Спрашиваем у Авито ссылку на профиль/);
    expect(hint).toHaveAttribute("title", expect.stringContaining("появится"));
  });

  it("спросили и Авито не дал — подсказка говорит именно это", () => {
    /*
     * Второй случай, и он противоположен по смыслу: ждать больше нечего.
     * Обещать «появится через секунду» здесь было бы новой неправдой — человек
     * будет ждать и вернётся с тем же вопросом.
     */
    renderWithClient({
      external_id: "923456789",
      profile_url: null,
      profile_checked: true,
    });
    const hint = screen.getByLabelText(/Авито не дал ссылку на профиль/);
    expect(hint).toHaveAttribute(
      "title",
      expect.stringContaining("только идентификатор"),
    );
  });

  it("сервер отдал ссылку — она появилась, а подсказка ушла", () => {
    /*
     * ⚠ ЭТОТ ТЕСТ ПИСАЛСЯ КАК ОБЕЩАНИЕ И СТАЛ ПРОВЕРКОЙ ЖИВОГО. Он появился
     * 12 августа доказательством, что включение ссылки будет правкой ОДНОЙ
     * функции на сервере, без похода во фронт. 02.09 это и произошло: разметка
     * не менялась ни на строку, менялся только сервер.
     */
    renderWithClient({
      external_id: "923456789",
      profile_url: "https://www.avito.ru/user/xyz",
    });
    expect(screen.getByText(/Профиль на Авито/)).toBeInTheDocument();
    // Объяснять нечего: ссылка есть, и значок-подсказка обязан исчезнуть
    // вместе с причиной, ради которой он появлялся.
    expect(
      screen.queryByLabelText(/Ссылку на профиль Авито не отдаёт/),
    ).toBeNull();
  });

  it("ссылка уходит в новую вкладку и не даёт чужой странице доступ к нашей", () => {
    renderWithClient({
      external_id: "923456789",
      profile_url: "https://www.avito.ru/user/xyz",
    });
    const ссылка = screen.getByText(/Профиль на Авито/).closest("a");
    expect(ссылка).toHaveAttribute("target", "_blank");
    /*
     * `noopener` записан ЯВНО, хотя сегодня действует и без него: `noreferrer`
     * его подразумевает, а сверху лежит `Cross-Origin-Opener-Policy`. Защита,
     * держащаяся на чужом неявном правиле и на заголовке в конфиге, живёт до
     * первой правки конфига.
     */
    expect(ссылка?.getAttribute("rel")).toContain("noopener");
    expect(ссылка?.getAttribute("rel")).toContain("noreferrer");
  });

  it.each([
    ["javascript:alert(1)", "исполняемая схема"],
    ["data:text/html,<b>x</b>", "встроенный документ"],
    ["file:///etc/passwd", "локальный файл"],
  ])("адрес со схемой %s ссылкой не рисуется (%s)", (адрес) => {
    /*
     * ⚠ АДРЕС ПРИШЁЛ ОТ НАШЕГО СЕРВЕРА, НО СОЧИНИЛ ЕГО НЕ ОН. И профиль, и
     * объявление приезжают из ответа Авито; `item_url` на бэкенде проверяется
     * только на «это строка». То есть содержимое чужого JSON решало бы, куда
     * уйдёт человек по нажатию.
     *
     * В браузере `javascript:` погасила бы CSP — но в настольном приложении
     * тот же адрес уходит в `bridge.openExternal`, то есть в СИСТЕМНЫЙ
     * обработчик схемы, а он открывает не браузер, а что угодно. Полагаться на
     * вторую линию, когда первая ставится одной строкой, — плохой размен.
     */
    renderWithClient({ external_id: "923456789", profile_url: адрес });
    expect(screen.queryByText(/Профиль на Авито/)).not.toBeInTheDocument();
  });
});
