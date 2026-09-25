import type { BotSchedule, BotScenario, BotScenarioIssue, BotStep } from "@/shared/api/types";
import {
  SEND_POOL_MAX,
  SYSTEM_VARS,
  collectRefs,
  declaredVar,
  extractPlaceholders,
  isTerminal,
  sendVariants,
} from "./scenario";

/**
 * Клиентский слой валидации сценария — те же правила и коды, что на сервере
 * (02-BOT-ENGINE §5.2). Смысл ровно один: мгновенный фидбек в редакторе.
 * ИСТИНА — ответ `PUT /bots/{id}` (`422 bot_scenario_invalid`); его список
 * ошибок рендерится в тот же блок и перекрывает локальный.
 *
 * `error` блокирует сохранение, `warning` — нет (кнопка «Сохранить с
 * предупреждениями»).
 */

const STEP_ID_RE = /^[a-z0-9_]{1,64}$/;
const VAR_RE = /^[a-z][a-z0-9_]{0,31}$/;
const TEXT_MAX = 1000;

function err(code: string, stepId: string | null, message: string, field?: string): BotScenarioIssue {
  return { level: "error", code, step_id: stepId, field: field ?? null, message };
}

function warn(code: string, stepId: string | null, message: string, field?: string): BotScenarioIssue {
  return { level: "warning", code, step_id: stepId, field: field ?? null, message };
}

/** Тексты шага, в которых работают плейсхолдеры (02 §1.2). */
function stepTexts(step: BotStep): Array<{ field: string; value: string | null | undefined; required: boolean }> {
  switch (step.type) {
    case "send": {
      // Пул формулировок: каждая обязана быть готовой к отправке — движок
      // выберет любую (проверка 24.09: `value.trim` падал на списке).
      const variants = sendVariants(step.params.text);
      if (variants.length === 0) return [{ field: "params.text", value: "", required: true }];
      return variants.map((value, i) => ({
        field: variants.length > 1 ? `params.text[${i}]` : "params.text",
        value,
        required: true,
      }));
    }
    case "note":
      return [{ field: "params.text", value: step.params.text, required: true }];
    case "ask":
      return [
        { field: "params.text", value: step.params.text, required: false },
        { field: "params.retry_text", value: step.params.retry_text, required: false },
      ];
    case "menu":
      return [
        { field: "params.text", value: step.params.text, required: true },
        { field: "params.retry_text", value: step.params.retry_text, required: false },
      ];
    case "handoff":
      return [{ field: "params.comment", value: step.params.comment, required: false }];
    case "close":
      return [{ field: "params.text", value: step.params.text, required: false }];
    default:
      return [];
  }
}

function checkRegex(source: string, stepId: string, field: string, out: BotScenarioIssue[]): void {
  if (source.length > 200) {
    out.push(err("bad_regex", stepId, `Шаг ${stepId}: регулярка длиннее 200 символов`, field));
    return;
  }
  try {
    new RegExp(source);
  } catch {
    out.push(err("bad_regex", stepId, `Шаг ${stepId}: регулярка не компилируется`, field));
  }
}

/** Слой 1 — форма шага (проекция JSON Schema 02 §1.4 на то, что видит админ). */
function validateStepShape(step: BotStep, out: BotScenarioIssue[]): void {
  if (!STEP_ID_RE.test(step.id)) {
    out.push(
      err("bad_step_id", step.id, `Идентификатор «${step.id}»: только латиница в нижнем регистре, цифры и _`, "id"),
    );
  }

  for (const t of stepTexts(step)) {
    const value = t.value ?? "";
    if (t.required && value.trim().length === 0) {
      out.push(err("text_required", step.id, `Шаг ${step.id}: текст обязателен`, t.field));
    }
    if (value.length > TEXT_MAX) {
      out.push(err("text_too_long", step.id, `Шаг ${step.id}: текст длиннее ${TEXT_MAX} символов`, t.field));
    }
  }

  if (step.type === "send" && sendVariants(step.params.text).length > SEND_POOL_MAX) {
    out.push(
      err("text_pool_too_big", step.id, `Шаг ${step.id}: формулировок больше ${SEND_POOL_MAX}`, "params.text"),
    );
  }

  if (step.type === "ask") {
    if (!VAR_RE.test(step.params.var ?? "")) {
      out.push(
        err(
          "bad_var_name",
          step.id,
          `Шаг ${step.id}: имя переменной — латиница, с буквы, до 32 символов (например phone)`,
          "params.var",
        ),
      );
    }
    const v = step.params.validate;
    if (v && typeof v === "object" && "regex" in v) checkRegex(v.regex, step.id, "params.validate.regex", out);
    if (step.params.timeout === null || step.params.timeout === undefined) {
      out.push(warn("ask_no_timeout", step.id, `Шаг ${step.id}: без таймаута бот может ждать вечно`, "params.timeout"));
    }
  }

  if (step.type === "menu") {
    if (step.params.options.length === 0) {
      out.push(err("menu_no_options", step.id, `Шаг ${step.id}: у меню нет ни одного варианта`, "params.options"));
    }
    const seen = new Map<string, number>();
    step.params.options.forEach((o) => {
      if (o.match.length === 0) {
        out.push(err("menu_no_match", step.id, `Шаг ${step.id}: у варианта «${o.label}» нет ключевых слов`));
      }
      o.match.forEach((m) => {
        const key = m.trim().toLowerCase();
        if (!key) return;
        seen.set(key, (seen.get(key) ?? 0) + 1);
      });
    });
    for (const [word, count] of seen) {
      if (count > 1) {
        out.push(warn("menu_dup_match", step.id, `Шаг ${step.id}: слово «${word}» есть в нескольких вариантах`));
      }
    }
    const optionIds = step.params.options.map((o) => o.id);
    for (const id of new Set(optionIds.filter((id, i) => optionIds.indexOf(id) !== i))) {
      out.push(
        err(
          "menu_dup_option",
          step.id,
          `Шаг ${step.id}: два варианта с id «${id}» — второй недостижим`,
          "params.options",
        ),
      );
    }
  }

  if (step.type === "condition") {
    if (step.params.conditions.length === 0) {
      out.push(err("condition_empty", step.id, `Шаг ${step.id}: нет ни одного условия`, "params.conditions"));
    }
    step.params.conditions.forEach((c, i) => {
      if (c.if.kind === "text_matches") checkRegex(c.if.regex, step.id, `conditions[${i}].if.regex`, out);
      if (c.if.kind === "text_contains" && c.if.keywords.filter((k) => k.trim()).length === 0) {
        out.push(err("condition_empty", step.id, `Шаг ${step.id}: у условия «текст содержит» нет слов`));
      }
      if ((c.if.kind === "var_exists" || c.if.kind === "var_equals") && !c.if.var.trim()) {
        out.push(err("condition_empty", step.id, `Шаг ${step.id}: в условии не указана переменная`));
      }
    });
  }

  if (step.type === "tag" && step.params.tags.length === 0) {
    out.push(err("tag_empty", step.id, `Шаг ${step.id}: не выбрано ни одного тега`, "params.tags"));
  }
}

interface Edge {
  to: string;
  /** Ребро требует ответа клиента (выходит из ask/menu) — для static_loop. */
  waits: boolean;
}

function buildEdges(steps: BotStep[]): Map<string, Edge[]> {
  const edges = new Map<string, Edge[]>();
  for (const step of steps) {
    const waits = step.type === "ask" || step.type === "menu";
    edges.set(
      step.id,
      collectRefs(step).map((r) => ({ to: r.ref, waits })),
    );
  }
  return edges;
}

function reachableFrom(entry: string, edges: Map<string, Edge[]>): Set<string> {
  const seen = new Set<string>();
  const queue = [entry];
  while (queue.length) {
    const id = queue.shift() as string;
    if (seen.has(id) || !edges.has(id)) continue;
    seen.add(id);
    for (const e of edges.get(id) ?? []) queue.push(e.to);
  }
  return seen;
}

/** Шаги, из которых существует путь в handoff/close (обратный обход). */
function canReachTerminal(steps: BotStep[], edges: Map<string, Edge[]>): Set<string> {
  const reverse = new Map<string, string[]>();
  for (const [from, list] of edges) {
    for (const e of list) {
      if (!reverse.has(e.to)) reverse.set(e.to, []);
      (reverse.get(e.to) as string[]).push(from);
    }
  }
  const good = new Set<string>();
  const queue = steps.filter((s) => isTerminal(s.type)).map((s) => s.id);
  while (queue.length) {
    const id = queue.shift() as string;
    if (good.has(id)) continue;
    good.add(id);
    for (const from of reverse.get(id) ?? []) queue.push(from);
  }
  return good;
}

/** Цикл по «бесплатным» рёбрам — гарантированное зацикливание (02 §5.2). */
function findStaticLoop(edges: Map<string, Edge[]>): string | null {
  const state = new Map<string, 0 | 1 | 2>();
  let found: string | null = null;

  const visit = (id: string): void => {
    if (found || !edges.has(id)) return;
    const mark = state.get(id);
    if (mark === 1) {
      found = id;
      return;
    }
    if (mark === 2) return;
    state.set(id, 1);
    for (const e of edges.get(id) ?? []) {
      if (e.waits) continue; // ребро ждёт клиента — вечного цикла не даст
      visit(e.to);
      if (found) return;
    }
    state.set(id, 2);
  };

  for (const id of edges.keys()) {
    visit(id);
    if (found) return found;
  }
  return null;
}

/**
 * Полная проверка сценария. `knowledgeBase` нужен для `ai_without_kb` —
 * шаг `ai_answer` без базы знаний отвечает хуже (02 §5.2).
 */
export function validateScenario(scenario: BotScenario, knowledgeBase: string): BotScenarioIssue[] {
  const out: BotScenarioIssue[] = [];
  const steps = scenario.steps;

  if (steps.length === 0) {
    out.push(err("empty_scenario", null, "В сценарии нет ни одного шага"));
    return out;
  }

  const byId = new Map<string, BotStep>();
  for (const step of steps) {
    if (byId.has(step.id)) {
      out.push(err("duplicate_id", step.id, `Идентификатор «${step.id}» встречается дважды`, "id"));
    } else {
      byId.set(step.id, step);
    }
    validateStepShape(step, out);
  }

  if (!byId.has(scenario.entry)) {
    out.push(err("entry_missing", null, `Стартовый шаг «${scenario.entry}» не найден в сценарии`, "entry"));
  }

  for (const step of steps) {
    for (const ref of collectRefs(step)) {
      if (!byId.has(ref.ref)) {
        out.push(err("broken_ref", step.id, `Шаг ${step.id}: переход на несуществующий шаг «${ref.ref}»`, ref.field));
      }
    }
  }

  const edges = buildEdges(steps);
  const reachable = byId.has(scenario.entry) ? reachableFrom(scenario.entry, edges) : new Set<string>();

  for (const step of steps) {
    if (byId.has(scenario.entry) && !reachable.has(step.id)) {
      out.push(warn("unreachable_step", step.id, `Шаг ${step.id} недостижим из стартового шага`));
    }
  }

  const terminating = canReachTerminal(steps, edges);
  for (const id of reachable) {
    if (!terminating.has(id)) {
      out.push(err("no_terminal_path", id, `Шаг ${id}: из него нельзя дойти до handoff или close — сценарий повиснет`));
    }
  }

  const loop = findStaticLoop(edges);
  if (loop) {
    out.push(err("static_loop", loop, `Шаг ${loop}: цикл без вопроса клиенту — бот зациклится`));
  }

  // Переменные: объявленные ask/menu + системные. Путевую точность (объявлена
  // ли переменная НА ПУТИ до этого шага) считает сервер — здесь достаточно
  // глобального множества, оно ловит опечатки, ради которых проверка и нужна.
  const declared = new Set<string>(SYSTEM_VARS);
  const writers = new Map<string, string[]>();
  for (const step of steps) {
    const v = declaredVar(step);
    if (!v) continue;
    declared.add(v);
    if (!writers.has(v)) writers.set(v, []);
    (writers.get(v) as string[]).push(step.id);
  }
  for (const [v, owners] of writers) {
    if (owners.length > 1) {
      out.push(warn("var_shadowed", owners[1], `Переменную {${v}} пишут несколько шагов: ${owners.join(", ")}`));
    }
  }
  for (const step of steps) {
    for (const t of stepTexts(step)) {
      for (const name of extractPlaceholders(t.value)) {
        if (!declared.has(name)) {
          out.push(
            warn("var_undefined", step.id, `Шаг ${step.id}: переменная {${name}} нигде не заполняется`, t.field),
          );
        }
      }
    }
  }

  const entryStep = byId.get(scenario.entry);
  if (entryStep && entryStep.type === "ask" && !entryStep.params.text) {
    out.push(
      warn("first_step_asks", entryStep.id, `Шаг ${entryStep.id}: сценарий стартует молчаливым ожиданием ответа`),
    );
  }

  if (steps.some((s) => s.type === "ai_answer") && knowledgeBase.trim().length === 0) {
    const aiStep = steps.find((s) => s.type === "ai_answer") as BotStep;
    out.push(warn("ai_without_kb", aiStep.id, "База знаний пуста — шаг ai_answer ответит хуже"));
  }

  return out;
}

export function hasBlockingErrors(issues: BotScenarioIssue[]): boolean {
  return issues.some((i) => i.level === "error");
}

export const NAME_MAX = 200;
export const KNOWLEDGE_BASE_MAX = 20_000;

/**
 * Проверки «обёртки» бота — имени, базы знаний и расписания (01 §8.3). Сервер
 * отвергает их обычным `400 validation_error`, без списка шагов, поэтому
 * ловим их здесь: иначе админ получит безликое «не удалось сохранить».
 */
export function validateBotForm(
  name: string,
  knowledgeBase: string,
  schedule: BotSchedule,
): BotScenarioIssue[] {
  const out: BotScenarioIssue[] = [];

  if (!name.trim()) out.push(err("name_required", null, "Имя бота не может быть пустым", "name"));
  if (name.length > NAME_MAX) out.push(err("name_too_long", null, `Имя длиннее ${NAME_MAX} символов`, "name"));
  if (knowledgeBase.length > KNOWLEDGE_BASE_MAX) {
    out.push(
      err(
        "knowledge_base_too_long",
        null,
        `База знаний длиннее ${KNOWLEDGE_BASE_MAX} символов`,
        "knowledge_base",
      ),
    );
  }

  if (schedule.always === false) {
    const intervals = schedule.intervals ?? [];
    if (intervals.length === 0) {
      out.push(err("schedule_empty", null, "Расписание без интервалов не включит бота никогда", "schedule"));
    }
    intervals.forEach((iv, i) => {
      if (iv.start === iv.end) {
        out.push(err("schedule_interval_empty", null, `Интервал ${i + 1}: начало и конец совпадают`, "schedule"));
      }
      if ((iv.days ?? []).length === 0) {
        out.push(err("schedule_no_days", null, `Интервал ${i + 1}: не выбран ни один день недели`, "schedule"));
      }
    });
  }

  return out;
}
