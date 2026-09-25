// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в ProfileNotifyFeedUpdates0509.test.tsx: сторож читает файлы стилей.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ProfilePage } from "@/features/settings/profile/ProfilePage";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * СТОРОЖА ПЕРЕСБОРКИ ЭКРАНА «ПРОФИЛЬ» (05.09).
 *
 * ЧТО БЫЛО. Владелец посмотрел прошлую правку и сказал дословно: «вкладка
 * профиль так и не переработана». Он был прав: подняли кегли заголовков, а
 * раскладка осталась прежней — одна колонка из восьми блоков `.settings-block`
 * с горизонтальными чертами между ними. Аватар 40 px внутри такого же блока,
 * имя человека ступенью текста, «на месте / отошёл» третьим блоком сверху и
 * только на вкладке «Учётная запись». На 1440 колонка занимала 880 px из 1162,
 * на 2560 — 880 из 2282.
 *
 * ЧТО СТАЛО И ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ. Три яруса с разными ролями: шапка (кто я и
 * в каком я состоянии), карточки настроек, полоса справки. Сторожа держат
 * именно РАЗЛИЧИЕ ярусов — то, что подтяжкой типографики не восстановишь:
 * шапка стоит над вкладками, присутствие живёт в ней, у карточек есть
 * отделённая линией шапка, справка обходится без рамки, а на широком экране
 * колонок становится две и три.
 *
 * ⚠ ЧАСТЬ ПРОВЕРОК ЧИТАЕТ CSS ТЕКСТОМ. В vitest стоит `css: false`, jsdom
 * раскладку не считает — мерить в тесте нечего. Числа в сторожах сняты со
 * стенда (`dev-preview.html?screen=profile`) на пяти ширинах, а не прикинуты.
 */

const PROFILE_CSS = "src/features/settings/profile/profile.css";
const PRESENCE_CSS = "src/features/presence/presence-self.css";
const SETTINGS_CSS = "src/features/settings/settings.css";

/** CSS без комментариев: разбор в шапке файла вправе называть что угодно. */
function css(путь: string): string {
  return (readFileSync(путь, "utf-8") as string).replace(
    /\/\*[\s\S]*?\*\//g,
    " ",
  );
}

/** Тело правила `селектор { … }` — из указанного куска CSS. */
function правило(текст: string, селектор: string): string {
  const at = текст.indexOf(`${селектор} {`);
  expect(at, `правило ${селектор} не найдено`).toBeGreaterThan(-1);
  return текст.slice(at, текст.indexOf("}", at));
}

/**
 * Тело медиазапроса целиком — со всеми правилами внутри.
 *
 * Скобки считаем, а не ищем первую закрывающую: иначе из блока в четыре
 * правила сторожу достаётся одно, и диверсия в остальных трёх проходит мимо.
 */
function медиа(текст: string, условие: string): string {
  const at = текст.indexOf(`@media ${условие}`);
  expect(at, `медиазапрос @media ${условие} не найден`).toBeGreaterThan(-1);
  let глубина = 0;
  for (let i = текст.indexOf("{", at); i < текст.length; i += 1) {
    if (текст[i] === "{") глубина += 1;
    if (текст[i] === "}") {
      глубина -= 1;
      if (глубина === 0) return текст.slice(at, i + 1);
    }
  }
  throw new Error(`@media ${условие}: не закрыт`);
}

const MANAGER: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "templates:own",
];

function открытьПрофиль(
  role: Role = "manager",
  permissions: Permission[] = MANAGER,
) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/settings/profile"]}>
          <ProfilePage />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("Профиль: у экрана есть лицо 0509", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { items: [] })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("шапка стоит ДО полосы вкладок, а не внутри одной из них", () => {
    const { container } = открытьПрофиль();
    const шапка = container.querySelector(".prof-hero");
    const вкладки = container.querySelector(".lc-tabs");
    expect(шапка, "шапки профиля нет вовсе").not.toBeNull();
    expect(вкладки).not.toBeNull();
    /*
     * Порядок важнее наличия. Стой шапка внутри вкладки — состояние
     * «на месте / отошёл» пропадало бы с экрана при переходе на «Интерфейс»,
     * ровно как было до 05.09.
     */
    const после = шапка!.compareDocumentPosition(вкладки!);
    expect(
      после & Node.DOCUMENT_POSITION_FOLLOWING,
      "вкладки стоят выше шапки",
    ).toBeTruthy();
  });

  it("имя, роль и почта разведены по ролям, а не слиты в одну строку", () => {
    const { container } = открытьПрофиль("manager");
    expect(container.querySelector(".prof-hero__name")?.textContent).toBe(
      fakeUser.full_name,
    );
    // Роль пилюлей — признак человека. Строкой «Роль: Менеджер» она читалась
    // как ещё одна настройка среди семи соседних.
    expect(container.querySelector(".prof-hero__role")?.textContent).toBe(
      "Менеджер",
    );
    expect(container.querySelector(".prof-hero__mail")?.textContent).toBe(
      fakeUser.email,
    );
  });

  it("состояние «на месте / отошёл» видно с ОБЕИХ вкладок", async () => {
    const user = userEvent.setup();
    открытьПрофиль();

    expect(
      screen.getByRole("radiogroup", { name: "Моё состояние" }),
    ).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "Интерфейс" }));
    expect(screen.getByRole("tab", { name: "Интерфейс" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    /*
     * Это и есть цена прежней раскладки: настройку, которую меняют по
     * нескольку раз в смену, было не видно с соседней вкладки. Обед начинался
     * с двух нажатий — «Учётная запись», потом «Отошёл».
     */
    expect(
      screen.getByRole("radiogroup", { name: "Моё состояние" }),
      "присутствие пропало при переходе на «Интерфейс»",
    ).toBeTruthy();
  });
});

describe("Профиль: настройки — карточки, а не стопка с чертами 0509", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { items: [] })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /*
   * ⚠ ОБЕ ПРОВЕРКИ НИЖЕ ОБХОДЯТ ВКЛАДКИ, А НЕ СМОТРЯТ ПЕРВУЮ. Первая редакция
   * читала только «Учётную запись» — и диверсия, вернувшая `.settings-block`
   * блоку «Всплывающие уведомления» (вкладка «Интерфейс»), прошла мимо
   * сторожа: зелёный тест на испорченном коде хуже отсутствующего.
   */
  async function поВсемВкладкам(
    container: HTMLElement,
    проверить: (карточки: Element[]) => void,
  ) {
    const user = userEvent.setup();
    for (const имя of ["Учётная запись", "Интерфейс"]) {
      await user.click(screen.getByRole("tab", { name: имя }));
      expect(
        container.querySelectorAll(".settings-block").length,
        `${имя}: вернулась стопка`,
      ).toBe(0);
      проверить([...container.querySelectorAll(".prof-card")]);
    }
  }

  it("ни один блок профиля не остался стопкой `.settings-block`", async () => {
    const { container } = открытьПрофиль("admin", [...MANAGER, "users:manage"]);
    await поВсемВкладкам(container, (карточки) => {
      expect(
        карточки.length,
        "карточек не осталось вовсе",
      ).toBeGreaterThanOrEqual(3);
    });
  });

  it("у каждой карточки есть шапка с заголовком, отделённая от контрола", async () => {
    const { container } = открытьПрофиль();
    await поВсемВкладкам(container, (карточки) => {
      expect(карточки.length).toBeGreaterThan(0);
      for (const карточка of карточки) {
        const шапка = карточка.querySelector(".prof-card__head");
        const имя = карточка.querySelector("h2")?.textContent?.trim();
        expect(
          шапка,
          `карточка «${имя}» без шапки: заголовок снова весит как контрол`,
        ).not.toBeNull();
        // Заголовок обязан лежать ВНУТРИ шапки — иначе линия отделяет не то.
        expect(
          шапка!.querySelector("h2"),
          `карточка «${имя}»: заголовок вне шапки`,
        ).not.toBeNull();
      }
    });
  });

  it("справка помечена отдельно от настроек", () => {
    const { container } = открытьПрофиль();
    const справка = [...container.querySelectorAll(".prof-card--ref")].map(
      (e) => e.querySelector("h2")?.textContent?.trim(),
    );
    /*
     * «Доступ и пароль» читают один раз за всё время работы, а стоял он в
     * одинаковом блоке с «Оформлением», которое трогают каждую смену.
     * («О приложении» тоже `--ref`, но в браузере блока нет вовсе —
     * десктопного моста здесь не существует.)
     */
    expect(справка).toContain("Доступ и пароль");
  });

  it("подсказки не показывают пальцем «выше» и «ниже»", () => {
    const { container } = открытьПрофиль();
    const текст = container.textContent ?? "";
    /*
     * В одну колонку «формой выше» было правдой, в две — враньём: форма стоит
     * не выше, а справа. Указание на место заменено именем формы, и оно верно
     * на всех пяти ширинах.
     */
    expect(
      текст,
      "подсказка снова указывает направление, а не имя",
    ).not.toMatch(/формой выше/);
    expect(текст).not.toMatch(/формой ниже/);
    expect(текст).toMatch(/формой «Написать администратору»/);
  });
});

describe("Профиль: раскладка от 375 до 2560 0509", () => {
  it("две колонки от 1200 и потолок ровно на пороге", () => {
    const блок = медиа(css(PROFILE_CSS), "(min-width: 1200px)");
    expect(блок).toMatch(/grid-template-columns:\s*repeat\(2,/);
    /*
     * Потолок 1200 = ширина окна, на которой раскладка появилась. Без него на
     * 2560 карточка с одним переключателем растянулась бы на 1117 px (замер
     * стендом), и подпись под ним пришлось бы читать, водя головой.
     */
    expect(
      блок,
      "потолок ширины пропал — карточка растёт вместе с монитором",
    ).toMatch(/max-width:\s*1200px/);
  });

  it("три колонки от 2000 — там, где они не сужают карточку", () => {
    const блок = медиа(css(PROFILE_CSS), "(min-width: 2000px)");
    expect(блок).toMatch(/grid-template-columns:\s*repeat\(3,/);
    expect(блок).toMatch(/max-width:\s*1800px/);
  });

  it("широкая карточка растягивается ТОЛЬКО там, где колонок больше одной", () => {
    const текст = css(PROFILE_CSS);
    /*
     * ⚠ Не придирка к месту объявления. `grid-column: span 2` в сетке из одной
     * колонки заводит НЕЯВНУЮ вторую колонку шириной `auto` — на телефоне
     * таблица сочетаний растащила бы сетку надвое, и все карточки съехали бы в
     * половину экрана.
     */
    const доМедиа = текст.slice(0, текст.indexOf("@media"));
    expect(доМедиа, "`span 2` объявлен вне медиазапроса").not.toMatch(
      /grid-column:\s*span 2/,
    );
    expect(медиа(текст, "(min-width: 1200px)")).toMatch(
      /grid-column:\s*span 2/,
    );
  });

  it("на 767 и уже аватар с именем встают в столбец", () => {
    const блок = медиа(css(PROFILE_CSS), "(max-width: 767px)");
    /*
     * На 375 столбцу имени остаётся 247 px: имя ломается на две строки, почта
     * ещё на две, и аватар при `align-items: center` повисает посередине
     * стопки — против пустого места. Порог общий для продукта (`MOBILE_MAX`).
     */
    expect(блок).toMatch(/flex-direction:\s*column/);
  });

  it("шапка не роняет рамку присутствия под имя, пока места хватает", () => {
    /*
     * Перенос во `flex-wrap` случается ДО сжатия, а базой при `auto` служит
     * ширина содержимого — почта в 54 знака просит 411 px. На окне 1200 блок
     * имени просил 510, рамка 340, вместе с зазором 874 — ровно вся ширина, и
     * рамка съезжала вниз, хотя места ей хватало.
     */
    expect(правило(css(PROFILE_CSS), ".prof-hero__id")).toMatch(
      /flex:\s*1 1 420px/,
    );
  });
});

describe("Профиль: ничего не потеряно и не обрезано 0509", () => {
  it("таблица сочетаний прокручивается вбок, а не уезжает за край", () => {
    /*
     * Замер стендом на 375: таблица просит 495 px при доступных 277, а
     * `.settings-content` объявлен `overflow: hidden` — столбец с кнопкой
     * «изменить» и переключателем исчезал молча, без полосы прокрутки.
     * Девятнадцать сочетаний с телефона было не включить.
     */
    const блок = правило(css(PROFILE_CSS), ".prof-hotkeys__scroll");
    expect(блок).toMatch(/overflow-x:\s*auto/);
    // Без снятия минимального размера флекс-ребёнку прокручиваться нечему.
    expect(блок).toMatch(/min-width:\s*0/);
  });

  it("переключатели профиля не растягиваются во всю карточку", () => {
    /*
     * Правка жила в settings.css и целилась в `.settings-block`. Профиль с
     * него ушёл — и без перечисления новых мест «Тёмная / Светлая / Как в
     * системе» растянулось бы на всю ширину карточки, а выбранный пункт
     * перестал бы читаться как один из трёх.
     */
    const текст = css(SETTINGS_CSS);
    const at = текст.indexOf(".mantine-SegmentedControl-root");
    expect(at).toBeGreaterThan(-1);
    const селекторы = текст.slice(
      текст.lastIndexOf("}", at) + 1,
      текст.indexOf("{", at),
    );
    expect(селекторы, "карточки профиля выпали из правила ширины").toContain(
      ".prof-card",
    );
    expect(селекторы, "рамка присутствия выпала из правила ширины").toContain(
      ".presence-self",
    );
  });

  it("справка обходится без рамки и занимает строку целиком", () => {
    const блок = правило(css(PROFILE_CSS), ".prof-card--ref");
    /*
     * Стоя ячейкой рядом с карточками, справка читалась бы как карточка, у
     * которой не нарисовалась рамка, — то есть как поломка.
     */
    expect(блок).toMatch(/grid-column:\s*1 \/ -1/);
    expect(блок).toMatch(/background:\s*none/);
  });

  it("рамка присутствия не растягивается на всю шапку", () => {
    /*
     * Без верхней границы на 2560 рамка растянулась бы на 700 px под
     * переключатель шириной 190 (замер стендом) — пустая коробка вокруг двух
     * слов.
     */
    expect(правило(css(PRESENCE_CSS), ".presence-self")).toMatch(
      /flex:\s*0 1 340px/,
    );
  });
});
