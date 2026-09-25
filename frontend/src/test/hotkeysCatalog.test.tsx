import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { hotkeyRows, hotkeysFor } from "@/features/hotkeys/catalog";
import { HotkeysModal } from "@/features/hotkeys/HotkeysModal";
import { RELEASES } from "@/features/updates/changelog";
import { KIND_CATALOG, SEVERITY_LABELS } from "@/features/notifications/catalog";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const MANAGER: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "notes:read",
  "notes:write",
];

/** Наблюдатель: только смотрит. Ни принять, ни закрыть, ни передать. */
const OBSERVER: Permission[] = ["conversations:read", "notes:read"];

describe("Шпаргалка не учит невозможному (FUNC-35)", () => {
  it("наблюдателю не обещают «принять», «закрыть» и «передать»", () => {
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: OBSERVER,
      bootstrapped: true,
    });
    renderWithProviders(<HotkeysModal opened onClose={() => {}} />);

    expect(screen.queryByText(/Принять диалог/)).toBeNull();
    expect(screen.queryByText(/Закрыть диалог/)).toBeNull();
    expect(screen.queryByText(/Передать диалог/)).toBeNull();
    // Перемещение по списку и поиск ему доступны — их прячем зря не будем.
    expect(screen.getByText(/Поиск по диалогам/)).toBeInTheDocument();
  });

  it("менеджеру показывают всё, что он может нажать", () => {
    resetSessionStore({ user: fakeUser, permissions: MANAGER, bootstrapped: true });
    renderWithProviders(<HotkeysModal opened onClose={() => {}} />);

    expect(screen.getByText(/Принять диалог/)).toBeInTheDocument();
    expect(screen.getByText(/Входящие: очередь/)).toBeInTheDocument();
  });

  it("право у строки — из реального набора прав, а не выдуманное", () => {
    const known: Permission[] = [
      "conversations:read",
      "messages:send",
      "conversations:manage",
      "notes:read",
      "notes:write",
      "templates:own",
      "templates:shared",
      "stats:own",
      "stats:all",
      "bots:manage",
      "accounts:read",
      "accounts:manage",
      "users:manage",
      "audit:read",
      "settings:manage",
    ];
    for (const h of hotkeyRows()) {
      if (h.need) expect(known).toContain(h.need);
    }
  });

  it("без прав остаётся только то, что работает у всех", () => {
    const rows = hotkeysFor(() => false);
    expect(rows.map((r) => r.keys)).toContain("?");
    expect(rows.map((r) => r.keys)).not.toContain("Ctrl + R");
  });
});

/**
 * СЛОВАРЬ ТЕРМИНОВ ОБЯЗАТЕЛЕН (10 §7.2), И ЕГО НАРУШАЛИ ИМЕННО В ЭТИХ СПИСКАХ.
 *
 * Проверяются не исходники, а ДАННЫЕ, которые попадают человеку на экран:
 * шпаргалка клавиш, журнал «Что нового» и подписи видов уведомлений. Сторож на
 * тексте файла ловил бы заодно комментарии — то есть падал бы на объяснении
 * «здесь нельзя говорить „обращение“».
 */
describe("Словарь терминов в списках, которые видит человек (10 §7.2)", () => {
  /** Слово → чем говорим вместо него. */
  const BANNED: Array<[RegExp, string]> = [
    [/обращени/i, "диалог"],
    [/оператор/i, "сотрудник или менеджер"],
    [/тикет/i, "диалог"],
    [/анрид/i, "непрочитанные"],
  ];

  function check(where: string, text: string) {
    for (const [word, instead] of BANNED) {
      expect(word.test(text), `${where}: «${text}» — говорим «${instead}»`).toBe(false);
    }
  }

  it("шпаргалка горячих клавиш", () => {
    for (const h of hotkeyRows()) check("хоткеи", h.what);
    // Статуса «решённый» в продукте нет — есть «Закрыт» (TEXT-43).
    expect(hotkeyRows().some((h) => /решённ/i.test(h.what))).toBe(false);
  });

  it("«Что нового» — его читают все тринадцать", () => {
    for (const r of RELEASES) {
      check("заголовок выпуска", r.title);
      for (const c of r.changes) {
        check("что изменилось", c.what);
        if (c.why) check("зачем", c.why);
      }
    }
  });

  it("подписи видов уведомлений и важности", () => {
    for (const meta of Object.values(KIND_CATALOG)) check("вид уведомления", meta.label);
    for (const label of Object.values(SEVERITY_LABELS)) check("важность", label);
  });
});
