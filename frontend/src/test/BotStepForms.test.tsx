import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StepForm } from "@/features/settings/bots/components/StepForms";
import { DEFAULT_REF_LABEL } from "@/features/settings/bots/components/StepRefSelect";
import { makeStep } from "@/features/settings/bots/scenario";
import type { BotAskStep, BotSendStep, BotStep, BotStepType } from "@/shared/api/types";
import { renderWithProviders } from "./render";

/**
 * Формы девяти типов шагов (02 §1.3, поля — 11 §5.2): состав полей, переходы
 * Select'ом по списку шагов сценария и подстановка переменных в тексты.
 */

const STEP_IDS = ["greet", "ask_problem", "handoff_day"];
const VARS = ["client_name", "item_title", "problem"];

function renderForm(step: BotStep, onChange = vi.fn()) {
  const utils = renderWithProviders(<StepForm step={step} stepIds={STEP_IDS} vars={VARS} onChange={onChange} />);
  return { ...utils, onChange };
}

/** Управляемая обёртка — там, где проверяется значение поля после правки. */
function ControlledForm({ initial }: { initial: BotStep }) {
  const [step, setStep] = useState(initial);
  return <StepForm step={step} stepIds={STEP_IDS} vars={VARS} onChange={setStep} />;
}

interface FormCase {
  type: BotStepType;
  /** `label` — для `input[type=time]`: у него нет ARIA-роли, ищем по подписи. */
  fields: Array<[role: "textbox" | "checkbox" | "label", name: string]>;
  texts?: string[];
}

const CASES: FormCase[] = [
  { type: "send", fields: [["textbox", "Текст"], ["textbox", "далее →"]] },
  {
    type: "ask",
    fields: [
      ["textbox", "Вопрос"],
      ["textbox", "Переменная"],
      ["textbox", "Валидатор"],
      ["textbox", "Retry-текст"],
      ["textbox", "Попыток"],
      ["textbox", "Таймаут"],
      ["textbox", "ответ →"],
      ["textbox", "по таймауту →"],
      ["textbox", "попытки исчерпаны →"],
    ],
  },
  {
    type: "menu",
    fields: [
      ["textbox", "Текст вопроса"],
      ["textbox", "Переменная (необязательно)"],
      ["textbox", "Вариант 1: название"],
      ["textbox", "Вариант 1: ключевые слова"],
      ["textbox", "мимо вариантов →"],
      ["textbox", "по таймауту →"],
    ],
  },
  {
    type: "condition",
    fields: [
      ["textbox", "Вид условия 1"],
      ["label", "Рабочее время с"],
      ["label", "Рабочее время до"],
      ["textbox", "иначе →"],
    ],
  },
  {
    type: "ai_answer",
    fields: [
      ["textbox", "Макс. длина ответа"],
      ["textbox", "Глубина контекста"],
      ["textbox", "далее →"],
      ["textbox", "при низкой уверенности →"],
    ],
    texts: ["Порог уверенности: 0.60"],
  },
  {
    type: "handoff",
    fields: [["textbox", "Причина"], ["textbox", "Комментарий менеджеру"], ["textbox", "Теги"]],
    texts: ["завершает сценарий"],
  },
  { type: "close", fields: [["checkbox", "Закрыть молча"]], texts: ["завершает сценарий"] },
  { type: "tag", fields: [["textbox", "Теги"], ["textbox", "далее →"]] },
  { type: "note", fields: [["textbox", "Текст заметки"], ["textbox", "далее →"]] },
];

describe("Формы шагов сценария (02 §1.3)", () => {
  it.each(CASES)("$type: форма рисует поля из спецификации", ({ type, fields, texts }) => {
    renderForm(makeStep(type, `${type}_1`, "handoff_day"));
    for (const [role, name] of fields) {
      const el = role === "label" ? screen.getByLabelText(name) : screen.getByRole(role, { name });
      expect(el).toBeInTheDocument();
    }
    for (const text of texts ?? []) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
  });

  it("у терминальных шагов переходов нет вовсе", () => {
    const { unmount } = renderForm(makeStep("handoff", "handoff_1", ""));
    expect(screen.queryByRole("textbox", { name: "далее →" })).toBeNull();
    unmount();

    renderForm(makeStep("close", "close_1", ""));
    expect(screen.queryByRole("textbox", { name: "далее →" })).toBeNull();
  });

  it("меню добавляет вариант со своим переходом", async () => {
    const user = userEvent.setup();
    const { onChange } = renderForm(makeStep("menu", "menu_1", "handoff_day"));
    await user.click(screen.getByRole("button", { name: "Добавить вариант" }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        params: expect.objectContaining({
          options: expect.arrayContaining([expect.objectContaining({ label: "Новый вариант" })]),
        }),
      }),
    );
  });
});

describe("Числовые поля шага (11 §5.2)", () => {
  it("правка в границах уходит в шаг числом, а не строкой", async () => {
    const user = userEvent.setup();
    const { onChange } = renderForm(makeStep("ask", "ask_1", "greet") as BotAskStep);

    const attempts = screen.getByRole("textbox", { name: "Попыток" });
    await user.clear(attempts);
    await user.type(attempts, "4");

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ params: expect.objectContaining({ max_attempts: 4 }) }),
    );
  });

  it("значение вне диапазона поджимается к границе на blur", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ControlledForm initial={makeStep("ask", "ask_1", "greet")} />);

    const attempts = screen.getByRole("textbox", { name: "Попыток" });
    await user.clear(attempts);
    await user.type(attempts, "99");
    await user.tab();

    expect(attempts).toHaveValue("5"); // max_attempts ≤ 5
  });

  it("буквы в поле не попадают, пустое поле не роняет шаг в NaN", async () => {
    const user = userEvent.setup();
    const { onChange } = renderForm(makeStep("ask", "ask_1", "greet") as BotAskStep);

    const attempts = screen.getByRole("textbox", { name: "Попыток" });
    await user.clear(attempts);
    await user.type(attempts, "две");

    expect(attempts).toHaveValue("");
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Переходы — Select со списком шагов (02 §5.1)", () => {
  it("обязательный переход предлагает все id шагов сценария", async () => {
    const user = userEvent.setup();
    const { onChange } = renderForm(makeStep("ask", "ask_1", "greet") as BotAskStep);

    await user.click(screen.getByRole("textbox", { name: "ответ →" }));
    const listbox = await screen.findByRole("listbox");
    for (const id of STEP_IDS) {
      expect(within(listbox).getByRole("option", { name: id })).toBeInTheDocument();
    }

    await user.click(within(listbox).getByRole("option", { name: "handoff_day" }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ next: "handoff_day" }));
  });

  it("nullable-переход первым пунктом даёт «— дефолт: передать оператору —»", async () => {
    const user = userEvent.setup();
    const { onChange } = renderForm({
      ...(makeStep("ask", "ask_1", "greet") as BotAskStep),
      on_timeout: "handoff_day",
    });

    await user.click(screen.getByRole("textbox", { name: "по таймауту →" }));
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).getAllByRole("option")[0]).toHaveTextContent(DEFAULT_REF_LABEL);

    await user.click(within(listbox).getByRole("option", { name: DEFAULT_REF_LABEL }));
    // null в JSON = дефолтный handoff движка (02 §1.3), а не «никуда».
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ on_timeout: null }));
  });

  it("битую ссылку показывает как есть — молча подменять её нельзя", () => {
    renderForm({ ...(makeStep("send", "send_1", "greet") as BotSendStep), next: "tag_contct" });
    expect(screen.getByRole("textbox", { name: "далее →" })).toHaveValue("tag_contct (нет такого шага)");
  });
});

describe("Подстановка переменных в текстовые поля (02 §5.1)", () => {
  it("кнопка {…} вставляет плейсхолдер в текст шага", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ControlledForm initial={makeStep("send", "send_1", "greet")} />);

    const textarea = screen.getByRole("textbox", { name: "Текст" });
    await user.clear(textarea);
    await user.type(textarea, "Здравствуйте, ");

    await user.click(screen.getByRole("button", { name: "Вставить переменную: Текст" }));
    await user.click(await screen.findByRole("menuitem", { name: "{client_name}" }));

    expect(textarea).toHaveValue("Здравствуйте, {client_name}");
  });

  it("в списке — системные плейсхолдеры и переменные ask-шагов выше по списку", async () => {
    const user = userEvent.setup();
    renderForm(makeStep("note", "note_1", "handoff_day"));

    await user.click(screen.getByRole("button", { name: "Вставить переменную: Текст заметки" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "{client_name}" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "{item_title}" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "{problem}" })).toBeInTheDocument();
  });
});
