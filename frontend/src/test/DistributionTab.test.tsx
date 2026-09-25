import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { DistributionTab } from "@/features/settings/distribution/DistributionTab";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Настройки автораспределения (`/settings/distribution`).
 *
 * Проверяется то, что ломается тише всего: что «без ограничения» доезжает до
 * сервера отдельным признаком (иначе снять потолок нельзя вообще), что
 * сохранение происходит по кнопке, а не по касанию тумблера, и что подпись
 * под тумблером не утверждает обратное включённому состоянию.
 */

describe("Настройки распределения", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let state = { enabled: false, max_active: 5 as number | null };
  /** Ответ `GET /settings/work-hours`; 500 — чтобы проверить падение блока. */
  let hoursStatus = 200;
  let hours = { start_hour: 8, end_hour: 22 };

  const patches = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "PATCH")
      .map((c) => JSON.parse(String((c[1] as RequestInit).body)));

  beforeEach(() => {
    queryClient.clear();
    state = { enabled: false, max_active: 5 };
    hoursStatus = 200;
    hours = { start_hour: 8, end_hour: 22 };
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["settings:manage"],
      accessToken: "t",
      bootstrapped: true,
    });

    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/settings/distribution")) {
        if (init?.method === "PATCH") {
          const body = JSON.parse(String(init.body));
          if (body.enabled !== undefined) state.enabled = body.enabled;
          if (body.max_active_unlimited) state.max_active = null;
          else if (body.max_active !== undefined) state.max_active = body.max_active;
          return jsonResponse(200, state);
        }
        return jsonResponse(200, state);
      }
      if (url.pathname.endsWith("/settings/work-hours")) {
        if (init?.method === "PATCH") {
          hours = JSON.parse(String(init.body));
          return jsonResponse(200, hours);
        }
        return hoursStatus === 200
          ? jsonResponse(200, hours)
          : jsonResponse(500, { error: { code: "internal_error", message: "боль" } });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const render = () => renderWithProviders(<DistributionTab />, { route: "/settings/distribution" });

  it("по умолчанию выключено — привычный ручной приём", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });
    expect(toggle).not.toBeChecked();
    // В Jivo, откуда переезжает команда, автораспределения нет вовсе. День
    // переезда не должен начинаться с того, что диалоги пошли сами.
    expect(screen.getByText(/принимают вручную/)).toBeInTheDocument();
  });

  it("тумблер сам ничего не сохраняет — только кнопка", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });
    await userEvent.click(toggle);

    // Настройка меняет работу всей смены сразу, и «случайно задел тумблер»
    // стоит дороже лишнего клика.
    expect(patches()).toHaveLength(0);
    expect(screen.getByText("Есть несохранённые изменения")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Сохранить распределение" }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].enabled).toBe(true);
  });

  it("подпись под тумблером следует за состоянием, а не врёт", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });
    await userEvent.click(toggle);
    // Статичная подпись «Выключено — …» после включения утверждала бы обратное,
    // а читают её как состояние системы.
    expect(screen.queryByText(/принимают вручную/)).not.toBeInTheDocument();
    expect(screen.getByText(/уходит свободному менеджеру/)).toBeInTheDocument();
  });

  it("«без ограничения» уезжает на сервер отдельным признаком", async () => {
    render();
    await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });
    await userEvent.click(screen.getByRole("switch", { name: /Раздавать диалоги автоматически/ }));
    await userEvent.click(screen.getByRole("switch", { name: "Без ограничения" }));
    await userEvent.click(screen.getByRole("button", { name: "Сохранить распределение" }));

    await waitFor(() => expect(patches()).toHaveLength(1));
    // В JSON «без ограничения» и «поле не прислали» выглядят одинаково — оба
    // null. Без отдельного признака снять потолок было бы невозможно.
    expect(patches()[0].max_active_unlimited).toBe(true);
    expect(patches()[0].max_active).toBeUndefined();
  });

  it("потолок виден и когда раздача выключена", async () => {
    render();
    // Блок гаснет, но не исчезает: иначе посмотреть текущий лимит можно было бы
    // только включив раздачу — то есть включив её на живой смене ради вопроса.
    expect(await screen.findByLabelText("Сколько диалогов держать на одном менеджере")).toBeInTheDocument();
    expect(screen.getByLabelText("Сколько диалогов держать на одном менеджере")).toBeDisabled();
  });

  it("сохранённое значение остаётся на экране", async () => {
    state = { enabled: true, max_active: 7 };
    render();
    const toggle = await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });
    expect(toggle).toBeChecked();
    expect(await screen.findByLabelText("Сколько диалогов держать на одном менеджере")).toHaveValue("7");
    // Ничего не меняли — кнопка неактивна.
    expect(screen.getByRole("button", { name: "Сохранить распределение" })).toBeDisabled();
  });

  /*
   * DIST-03. Стереть значение, чтобы набрать другое, — обычное движение руки.
   * Пока пустота мгновенно превращалась в 1 (потолок) и в 0 (часы), человек
   * получал «18» вместо «8» и «08» вместо «8», не заметив подмены: он смотрит
   * на клавиатуру.
   */
  it("числовое поле можно очистить, и в нём не появляется подставное значение", async () => {
    state = { enabled: true, max_active: 10 };
    render();
    const cap = await screen.findByLabelText("Сколько диалогов держать на одном менеджере");
    await userEvent.clear(cap);

    expect(cap).toHaveValue("");
    // Сохранять нечего — кнопка гаснет и объясняет, почему.
    expect(screen.getByRole("button", { name: "Сохранить распределение" })).toBeDisabled();
    expect(
      screen.getByText("Впишите потолок диалогов или включите «Без ограничения»"),
    ).toBeInTheDocument();

    await userEvent.type(cap, "8");
    expect(cap).toHaveValue("8");
  });

  it("часы тоже очищаются без подстановки нуля", async () => {
    render();
    const from = await screen.findByLabelText("Начало рабочего дня, час по Москве");
    await userEvent.clear(from);

    expect(from).toHaveValue("");
    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeDisabled();
    expect(screen.getByText("Впишите оба часа — иначе сохранять нечего.")).toBeInTheDocument();

    await userEvent.type(from, "9");
    expect(from).toHaveValue("9:00");
  });

  /*
   * ВЫРОЖДЕННОЕ ОКНО НЕ СОХРАНЯЕТСЯ. Здесь стояло жёлтое предупреждение, а
   * кнопка работала — и на боевой системе так и сохранили 0:00–0:00. Окно
   * нулевой длины даёт ноль рабочих секунд на любом интервале, и медиана
   * «первый ответ в рабочее время» обнулилась у ВСЕХ менеджеров: колонка, по
   * которой оценивают тринадцать диспетчеров, показывала ноль независимо от
   * работы и выглядела совершенно обычно.
   */
  it("не даёт сохранить окно, в котором конец не позже начала", async () => {
    render();
    const from = await screen.findByLabelText("Начало рабочего дня, час по Москве");
    const to = screen.getByLabelText("Конец рабочего дня, час по Москве");

    await userEvent.clear(from);
    await userEvent.type(from, "0");
    await userEvent.clear(to);
    await userEvent.type(to, "0");

    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/Конец должен быть позже начала/);

    // И ни одного PATCH'а: отказ произошёл до запроса, а не тостом после него.
    expect(patches()).toEqual([]);
  });

  it("перевёрнутое окно тоже не сохраняется — оно даёт тот же ноль", async () => {
    /*
     * «С 20 до 8» выглядит как ночная смена, но окно считается внутри
     * КАЛЕНДАРНЫХ суток и через полночь не переходит: рабочих секунд там
     * ровно столько же, сколько в 0:00–0:00, то есть ноль.
     */
    render();
    const from = await screen.findByLabelText("Начало рабочего дня, час по Москве");
    await userEvent.clear(from);
    await userEvent.type(from, "23");

    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeDisabled();
    expect(patches()).toEqual([]);
  });

  it("нормальное окно сохраняется как раньше", async () => {
    // Охранник от чрезмерного запрета: «заблокировать кнопку навсегда»
    // выглядело бы такой же зелёной правкой, как «заблокировать пустое окно».
    render();
    const to = await screen.findByLabelText("Конец рабочего дня, час по Москве");
    await userEvent.clear(to);
    await userEvent.type(to, "21");

    const save = screen.getByRole("button", { name: "Сохранить часы" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(patches()).toEqual([{ start_hour: 8, end_hour: 21 }]));
  });

  /*
   * DIST-01. Пока часы не приехали (или не приехали вовсе), полей с зашитыми
   * 10 и 20 на экране быть не должно: они неотличимы от сохранённых, и
   * администратор уходит уверенным, что окно у него именно такое.
   */
  it("рабочие часы не выдумывают 10 и 20, когда запрос упал", async () => {
    hoursStatus = 500;
    render();

    // Запросы в приложении повторяются дважды (queryClient, retry: 2), поэтому
    // ошибке дают дожить до конца повторов.
    expect(
      await screen.findByText(/Рабочие часы не загрузились — показать нечего/, undefined, {
        timeout: 5000,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Начало рабочего дня, час по Москве")).toBeNull();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });

  it("показывает сохранённое окно, а не значения по умолчанию", async () => {
    render();
    expect(await screen.findByLabelText("Начало рабочего дня, час по Москве")).toHaveValue("8:00");
    expect(screen.getByLabelText("Конец рабочего дня, час по Москве")).toHaveValue("22:00");
    // Годное окно не подменяется ничем и кнопку само не зажигает: сохранять
    // нечего, пока человек ничего не поправил.
    expect(screen.queryByText(/Сохранённое окно/)).toBeNull();
    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeDisabled();
  });

  /*
   * ЛОВУШКА, ИЗ КОТОРОЙ НЕ БЫЛО ВЫХОДА (разбор владельца 11 августа, пункт 1).
   *
   * В базе лежит 0:00–0:00. Экран открывался с этими нулями в полях, красной
   * строкой под ними и ПОГАШЕННОЙ кнопкой «Сохранить часы»: она гасла и
   * потому, что окно негодное, и потому, что «ничего не меняли». Поправить
   * настройку через интерфейс становилось нельзя вовсе — а по этому окну
   * считается «первый ответ в рабочее время» у тринадцати диспетчеров.
   *
   * Проверка ломанием: верните `if (typeof q.data.start_hour === "number")
   * setStart(...)` без проверки годности окна — падает первый тест; верните
   * `disabled={!dirty || !hoursValid}` — падает он же на кнопке.
   */
  it("сохранённое 0:00–0:00 не запирает экран: предложены годные часы", async () => {
    hours = { start_hour: 0, end_hour: 0 };
    render();

    // В полях — предложение, а не нули, которыми ничего не измерить.
    expect(await screen.findByLabelText("Начало рабочего дня, час по Москве")).toHaveValue("10:00");
    expect(screen.getByLabelText("Конец рабочего дня, час по Москве")).toHaveValue("20:00");
    // Подмена названа вслух, с обоими числами: молча подставить значит соврать
    // — отчёты до нажатия кнопки считаются по-прежнему по вырожденному окну.
    expect(screen.getByRole("alert")).toHaveTextContent(/Сохранённое окно 0:00–0:00 нерабочее/);
    // И кнопка живая СРАЗУ, до всякой правки: это и есть выход.
    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeEnabled();
  });

  it("починка сохранённого окна доезжает до сервера", async () => {
    hours = { start_hour: 0, end_hour: 0 };
    render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");

    await userEvent.click(screen.getByRole("button", { name: "Сохранить часы" }));

    await waitFor(() => expect(patches()).toEqual([{ start_hour: 10, end_hour: 20 }]));
    // Ответ сервера теперь годный — жалоба уходит с экрана сама.
    await waitFor(() => expect(screen.queryByText(/Сохранённое окно/)).toBeNull());
  });

  it("перевёрнутое сохранённое окно чинится так же", async () => {
    // «С 20 до 8» выглядит ночной сменой, но окно считается внутри календарных
    // суток: рабочих секунд там ровно ноль, как и в 0:00–0:00.
    hours = { start_hour: 20, end_hour: 8 };
    render();

    expect(await screen.findByLabelText("Начало рабочего дня, час по Москве")).toHaveValue("10:00");
    expect(screen.getByRole("alert")).toHaveTextContent(/Сохранённое окно 20:00–8:00 нерабочее/);
    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeEnabled();
  });

  it("из починенного окна всё ещё нельзя сделать вырожденное", async () => {
    /*
     * Охранник от чрезмерной правки. «Разрешить сохранение всегда» выглядело
     * бы такой же зелёной правкой, как «разрешить сохранение при сломанном
     * сохранённом окне», — и вернуло бы ту самую беду, ради которой запрет и
     * ставили: 0:00–0:00 в базе.
     */
    hours = { start_hour: 0, end_hour: 0 };
    render();
    const to = await screen.findByLabelText("Конец рабочего дня, час по Москве");

    await userEvent.clear(to);
    await userEvent.type(to, "10");

    expect(screen.getByRole("button", { name: "Сохранить часы" })).toBeDisabled();
    expect(patches()).toEqual([]);
  });

  /* DIST-02. Сервер считает окно по Москве, остальное время в интерфейсе — по
     поясу браузера. Не назвать пояс значит дать владивостокской смене окно,
     которое считается по чужим часам. */
  it("часовой пояс рабочих часов назван прямо на экране", async () => {
    render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");
    expect(screen.getByText(/по московскому\s+времени/)).toBeInTheDocument();
  });

  /* DIST-04. Два независимых запроса; падение одного не имеет права уносить
     второй экран. */
  it("падение настроек распределения не уносит с собой рабочие часы", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/settings/work-hours")) return jsonResponse(200, hours);
      return jsonResponse(500, { error: { code: "internal_error", message: "боль" } });
    });
    render();

    expect(
      await screen.findByText(/Настройки распределения не загрузились/, undefined, { timeout: 5000 }),
    ).toBeInTheDocument();
    // Часы живы и редактируются — они грузились отдельным запросом.
    expect(await screen.findByLabelText("Начало рабочего дня, час по Москве")).toHaveValue("8:00");
  });
});
