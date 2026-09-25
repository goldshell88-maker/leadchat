/**
 * Формы шагов после проверки 24.09: пул формулировок, свободный номер варианта
 * меню и итог закрытия.
 *
 * * Серверная заготовка «Первичный приём» держит в дожиме список формулировок,
 *   а тип, форма и проверка знали только строку: `value.trim` падал, и
 *   редактор не открывался вовсе.
 * * «Добавить вариант» брал номер `длина + 1`: после удаления первого из двух
 *   появлялся второй `opt_2` с «2», и совпадение всегда уходило в первый.
 * * Итог шага `close` — единственный путь автозаявки из сценарного бота, а
 *   задать его в форме было нельзя.
 *
 * ДИВЕРСИИ: вернуть `step.params.text` строкой в `stepTexts` — краснеет
 * «пул проверяется по формулировкам»; вернуть `opt_${длина + 1}` — «номер
 * после удаления»; убрать проверку дублей id — «дубль id варианта»; убрать
 * выбор итога из `CloseForm` — «итог закрытия».
 */
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StepForm } from "@/features/settings/bots/components/StepForms";
import { makeStep, stepSummary } from "@/features/settings/bots/scenario";
import { validateScenario } from "@/features/settings/bots/validation";
import type { BotCloseStep, BotMenuStep, BotScenario, BotSendStep, BotStep } from "@/shared/api/types";
import { renderWithProviders } from "./render";

const STEP_IDS = ["greet", "ask_problem", "handoff_day"];
const VARS = ["client_name", "problem"];

function renderForm(step: BotStep, onChange = vi.fn()) {
  renderWithProviders(<StepForm step={step} stepIds={STEP_IDS} vars={VARS} onChange={onChange} />);
  return onChange;
}

function ControlledForm({ initial, onChange }: { initial: BotStep; onChange: (s: BotStep) => void }) {
  const [step, setStep] = useState(initial);
  return (
    <StepForm
      step={step}
      stepIds={STEP_IDS}
      vars={VARS}
      onChange={(next) => {
        setStep(next);
        onChange(next);
      }}
    />
  );
}

const pool = (texts: string[]): BotSendStep => ({
  id: "ping",
  type: "send",
  params: { text: texts },
  next: "handoff_day",
});

function scenarioWith(step: BotStep): BotScenario {
  return {
    version: 1,
    entry: step.id,
    steps: [step, { id: "handoff_day", type: "handoff", params: { reason: "scenario" } }],
  };
}

describe("пул формулировок шага send", () => {
  it("каждая формулировка — своё поле, удаление последней лишней возвращает строку", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(<ControlledForm initial={pool(["Ну что, расскажете?", "Подскажете, что случилось?"])} onChange={onChange} />);

    expect(screen.getByRole("textbox", { name: "Формулировка 1" })).toHaveValue("Ну что, расскажете?");
    expect(screen.getByRole("textbox", { name: "Формулировка 2" })).toHaveValue("Подскажете, что случилось?");

    await user.click(screen.getByRole("button", { name: "Удалить формулировку 2" }));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ params: { text: "Ну что, расскажете?" } }),
    );
    expect(screen.getByRole("textbox", { name: "Текст" })).toHaveValue("Ну что, расскажете?");
  });

  it("«Добавить формулировку» превращает строку в пул", async () => {
    const user = userEvent.setup();
    const onChange = renderForm(makeStep("send", "send_1", "greet"));

    await user.click(screen.getByRole("button", { name: "Добавить формулировку" }));

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ params: { text: ["Текст сообщения", ""] } }),
    );
  });

  it("пул проверяется по формулировкам и не роняет проверку", () => {
    const issues = validateScenario(scenarioWith(pool(["Ну что, расскажете?", "  "])), "база");

    expect(issues).toContainEqual(
      expect.objectContaining({ code: "text_required", step_id: "ping", field: "params.text[1]" }),
    );
  });

  it("больше двадцати формулировок — ошибка, как на сервере", () => {
    const texts = Array.from({ length: 21 }, (_, i) => `Вариант ${i + 1}`);
    const issues = validateScenario(scenarioWith(pool(texts)), "база");

    expect(issues).toContainEqual(expect.objectContaining({ code: "text_pool_too_big", level: "error" }));
  });

  it("карточка шага называет размер пула", () => {
    expect(stepSummary(pool(["Ну что, расскажете?", "Подскажете?"]))).toBe(
      "send: «Ну что, расскажете?» · вариантов: 2 → handoff_day",
    );
  });
});

describe("варианты меню", () => {
  it("номер после удаления — следующий свободный, а не длина + 1", async () => {
    const user = userEvent.setup();
    const menu = makeStep("menu", "menu_1", "handoff_day") as BotMenuStep;
    // Первый из двух вариантов удалён: остался `opt_2` с «2».
    menu.params.options = menu.params.options.slice(1);
    const onChange = renderForm(menu);

    await user.click(screen.getByRole("button", { name: "Добавить вариант" }));

    const next = onChange.mock.calls.at(-1)?.[0] as BotMenuStep;
    expect(next.params.options.map((o) => [o.id, o.match])).toEqual([
      ["opt_2", ["2"]],
      ["opt_3", ["3"]],
    ]);
  });

  it("дубль id варианта — ошибка: второй вариант недостижим", () => {
    const menu = makeStep("menu", "menu_1", "handoff_day") as BotMenuStep;
    menu.params.options[1].id = menu.params.options[0].id;

    const issues = validateScenario(scenarioWith(menu), "база");

    expect(issues).toContainEqual(
      expect.objectContaining({ code: "menu_dup_option", level: "error", step_id: "menu_1" }),
    );
  });
});

describe("итог закрытия", () => {
  it("«Выезд назначен» уходит в шаг как outcome = visit", async () => {
    const user = userEvent.setup();
    const onChange = renderForm(makeStep("close", "close_1", "") as BotCloseStep);

    await user.click(screen.getByRole("textbox", { name: "Итог диалога" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Выезд назначен" }));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ params: expect.objectContaining({ outcome: "visit" }) }),
    );
  });
});
