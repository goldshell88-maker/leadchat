import type {
  BotCondition,
  BotConditionKind,
  BotSchedule,
  BotScenario,
  BotStep,
  BotStepType,
  BotTimeout,
  BotWeekDay,
} from "@/shared/api/types";

/**
 * Чистые помощники редактора сценариев: метаданные типов шагов, фабрики
 * дефолтных шагов, работа со ссылками-переходами, словарь плейсхолдеров и
 * человекочитаемые подписи. Ни React, ни сети — всё тестируется как функции.
 * Формат — 02-BOT-ENGINE §1.
 */

export interface StepMeta {
  type: BotStepType;
  icon: string;
  label: string;
  /** Терминальный: переходов нет, движок останавливается (02 §1.1). */
  terminal: boolean;
  hint: string;
}

/** Иконки и подписи — 02 §5.1 / 11 §5.2. Порядок = порядок в Select «Тип». */
export const STEP_META: StepMeta[] = [
  { type: "send", icon: "💬", label: "Сообщение", terminal: false, hint: "Отправить текст клиенту" },
  { type: "ask", icon: "❓", label: "Вопрос", terminal: false, hint: "Задать вопрос и ждать ответ" },
  { type: "menu", icon: "📋", label: "Меню", terminal: false, hint: "Вопрос с вариантами ответа" },
  { type: "condition", icon: "🔀", label: "Условие", terminal: false, hint: "Ветвление без вопроса" },
  { type: "ai_answer", icon: "✨", label: "Ответ AI", terminal: false, hint: "Ответ Claude по базе знаний" },
  { type: "handoff", icon: "👤", label: "Оператору", terminal: true, hint: "Передать диалог человеку" },
  { type: "close", icon: "✅", label: "Закрыть", terminal: true, hint: "Закрыть диалог" },
  { type: "tag", icon: "🏷", label: "Тег", terminal: false, hint: "Повесить теги на диалог" },
  { type: "note", icon: "🗒", label: "Заметка", terminal: false, hint: "Внутренняя заметка для менеджера" },
];

const META_BY_TYPE = new Map<BotStepType, StepMeta>(STEP_META.map((m) => [m.type, m]));

export function stepMeta(type: BotStepType): StepMeta {
  return META_BY_TYPE.get(type) ?? STEP_META[0];
}

export function isTerminal(type: BotStepType): boolean {
  return stepMeta(type).terminal;
}

/** Единое пространство имён подстановок (02 §1.2). Латиница — только боты. */
export const SYSTEM_VARS = ["client_name", "item_title", "item_price", "account_title"] as const;

export const WEEK_DAYS: Array<{ value: BotWeekDay; label: string }> = [
  { value: "mon", label: "пн" },
  { value: "tue", label: "вт" },
  { value: "wed", label: "ср" },
  { value: "thu", label: "чт" },
  { value: "fri", label: "пт" },
  { value: "sat", label: "сб" },
  { value: "sun", label: "вс" },
];

/** Таймауты ask/menu (11 §5.2). Пустая строка = «без лимита» (null в JSON). */
export const TIMEOUT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "30m", label: "30 минут" },
  { value: "1h", label: "1 час" },
  { value: "2h", label: "2 часа" },
  { value: "12h", label: "12 часов" },
  { value: "24h", label: "24 часа" },
  { value: "3d", label: "3 дня" },
  { value: "", label: "без лимита" },
];

export const CONDITION_KINDS: Array<{ value: BotConditionKind; label: string }> = [
  { value: "work_hours", label: "рабочее время" },
  { value: "var_exists", label: "переменная существует" },
  { value: "var_equals", label: "переменная равна" },
  { value: "text_contains", label: "текст содержит" },
  { value: "text_matches", label: "текст по regex" },
];

export const VALIDATE_OPTIONS = [
  { value: "any", label: "любой ответ" },
  { value: "phone", label: "телефон" },
  { value: "number", label: "число" },
  { value: "regex", label: "regex…" },
];

/** Значение Select таймаута из JSON-поля. */
export function timeoutToSelect(timeout: BotTimeout | undefined): string {
  if (timeout === null || timeout === undefined) return "";
  return String(timeout);
}

export function selectToTimeout(value: string | null): BotTimeout {
  return value ? value : null;
}

export function timeoutLabel(timeout: BotTimeout | undefined): string {
  const value = timeoutToSelect(timeout);
  return TIMEOUT_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

/* ------------------------------- Фабрики шагов ---------------------------- */

/** Свободный `id` вида `send_2` — не пересекается с уже занятыми. */
export function uniqueStepId(steps: BotStep[], base: string): string {
  const taken = new Set(steps.map((s) => s.id));
  if (!taken.has(base)) return base;
  for (let i = 2; i < 500; i += 1) {
    const candidate = `${base}_${i}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${base}_${Date.now()}`;
}

/**
 * Новый шаг с дефолтными параметрами. `fallbackNext` — куда вести переход,
 * чтобы новый шаг не создавал битую ссылку (обычно следующий шаг списка).
 */
export function makeStep(type: BotStepType, id: string, fallbackNext: string): BotStep {
  switch (type) {
    case "send":
      return { id, type, params: { text: "Текст сообщения" }, next: fallbackNext };
    case "ask":
      return {
        id,
        type,
        params: {
          text: "Ваш вопрос клиенту?",
          var: "answer",
          validate: "any",
          retry_text: null,
          max_attempts: 2,
          timeout: "24h",
        },
        next: fallbackNext,
        on_timeout: null,
        on_invalid: null,
      };
    case "menu":
      return {
        id,
        type,
        params: {
          text: "Выберите вариант:\n1. Первый\n2. Второй",
          options: [
            { id: "opt_1", label: "Первый", match: ["1"], next: fallbackNext },
            { id: "opt_2", label: "Второй", match: ["2"], next: fallbackNext },
          ],
          retry_text: "Ответьте, пожалуйста, цифрой 🙂",
          max_attempts: 2,
          timeout: "24h",
        },
        on_no_match: null,
        on_timeout: null,
      };
    case "condition":
      return {
        id,
        type,
        params: {
          conditions: [
            {
              if: { kind: "work_hours", from: "10:00", to: "20:00", timezone: "Europe/Moscow" },
              next: fallbackNext,
            },
          ],
          else: fallbackNext,
        },
      };
    case "ai_answer":
      return {
        id,
        type,
        params: { confidence_threshold: 0.6, max_reply_len: 800, context_messages: 10 },
        next: fallbackNext,
        on_low_confidence: null,
      };
    case "handoff":
      return { id, type, params: { reason: "scenario", comment: null, tags: [] } };
    case "close":
      return { id, type, params: { text: null, silent: true } };
    case "tag":
      return { id, type, params: { tags: [] }, next: fallbackNext };
    case "note":
      return { id, type, params: { text: "Заметка для менеджера" }, next: fallbackNext };
  }
}

/* --------------------------------- Ссылки --------------------------------- */

export interface StepRef {
  /** Путь поля для сопоставления с серверным `issue.field`. */
  field: string;
  ref: string;
}

/** Все исходящие ссылки шага (02 §1.1: next, on_*, options[], conditions[], else). */
export function collectRefs(step: BotStep): StepRef[] {
  const out: StepRef[] = [];
  const push = (field: string, ref: string | null | undefined) => {
    if (ref) out.push({ field, ref });
  };
  switch (step.type) {
    case "send":
    case "tag":
    case "note":
      push("next", step.next);
      break;
    case "ask":
      push("next", step.next);
      push("on_timeout", step.on_timeout);
      push("on_invalid", step.on_invalid);
      break;
    case "menu":
      step.params.options.forEach((o, i) => push(`options[${i}].next`, o.next));
      push("on_no_match", step.on_no_match);
      push("on_timeout", step.on_timeout);
      break;
    case "condition":
      step.params.conditions.forEach((c, i) => push(`conditions[${i}].next`, c.next));
      push("else", step.params.else);
      break;
    case "ai_answer":
      push("next", step.next);
      push("on_low_confidence", step.on_low_confidence);
      break;
    case "handoff":
    case "close":
      break;
  }
  return out;
}

/**
 * Переименование шага: все ссылки на старый `id` переезжают на новый (02 §5.1
 * — «при переименовании все ссылки на шаг обновляются автоматически»).
 */
export function renameRefs(step: BotStep, from: string, to: string): BotStep {
  const swap = (v: string | null | undefined) => (v === from ? to : v);
  switch (step.type) {
    case "send":
    case "tag":
    case "note":
      return { ...step, next: swap(step.next) as string };
    case "ask":
      return {
        ...step,
        next: swap(step.next) as string,
        on_timeout: swap(step.on_timeout) ?? null,
        on_invalid: swap(step.on_invalid) ?? null,
      };
    case "menu":
      return {
        ...step,
        params: {
          ...step.params,
          options: step.params.options.map((o) => ({ ...o, next: swap(o.next) as string })),
        },
        on_no_match: swap(step.on_no_match) ?? null,
        on_timeout: swap(step.on_timeout) ?? null,
      };
    case "condition":
      return {
        ...step,
        params: {
          conditions: step.params.conditions.map((c) => ({ ...c, next: swap(c.next) as string })),
          else: swap(step.params.else) as string,
        },
      };
    case "ai_answer":
      return {
        ...step,
        next: swap(step.next) as string,
        on_low_confidence: swap(step.on_low_confidence) ?? null,
      };
    case "handoff":
    case "close":
      return step;
  }
}

/* ------------------------------- Переменные ------------------------------- */

const PLACEHOLDER_RE = /\{([a-z_][a-z0-9_]*)\}/gi;

/** Плейсхолдеры `{var}` в тексте (02 §1.2). */
export function extractPlaceholders(text: string | null | undefined): string[] {
  if (!text) return [];
  const out: string[] = [];
  let m: RegExpExecArray | null;
  PLACEHOLDER_RE.lastIndex = 0;
  while ((m = PLACEHOLDER_RE.exec(text)) !== null) out.push(m[1]);
  return out;
}

/** Переменная, которую объявляет шаг (`ask` всегда, `menu` — опционально). */
export function declaredVar(step: BotStep): string | null {
  if (step.type === "ask") return step.params.var || null;
  if (step.type === "menu") return step.params.var || null;
  return null;
}

/**
 * Переменные, доступные для вставки в текст шага с индексом `index`:
 * системные + объявленные `ask`/`menu` ВЫШЕ по списку (02 §5.1).
 */
export function varsAvailableAt(steps: BotStep[], index: number): string[] {
  const out = [...SYSTEM_VARS] as string[];
  steps.slice(0, Math.max(0, index)).forEach((s) => {
    const v = declaredVar(s);
    if (v && !out.includes(v)) out.push(v);
  });
  return out;
}

/* --------------------------------- Подписи -------------------------------- */

/** Сколько формулировок держит пул шага `send` — граница серверной схемы. */
export const SEND_POOL_MAX = 20;

/** Формулировки шага `send`: строка — одна, список — пул, из которого движок берёт одну. */
export function sendVariants(text: string | string[]): string[] {
  return Array.isArray(text) ? text : [text];
}

function clip(text: string | null | undefined, max = 42): string {
  if (!text) return "";
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

export function conditionSummary(cond: BotCondition): string {
  switch (cond.kind) {
    case "work_hours":
      return `рабочее время ${cond.from}–${cond.to}`;
    case "var_exists":
      return `есть {${cond.var}}`;
    case "var_equals":
      return `{${cond.var}} = «${clip(cond.value, 20)}»`;
    case "text_contains":
      return `текст содержит ${cond.keywords.slice(0, 3).join(", ")}`;
    case "text_matches":
      return `текст по regex ${clip(cond.regex, 20)}`;
  }
}

/** Сводка карточки одной строкой (11 §5.2). */
export function stepSummary(step: BotStep): string {
  switch (step.type) {
    case "send": {
      const variants = sendVariants(step.params.text);
      const pool = variants.length > 1 ? ` · вариантов: ${variants.length}` : "";
      return `send: «${clip(variants[0])}»${pool} → ${step.next}`;
    }
    case "ask":
      return `ask → ${step.params.var} · таймаут ${timeoutLabel(step.params.timeout)}`;
    case "menu":
      return `menu: ${step.params.options.length} вариант(ов) · «${clip(step.params.text, 28)}»`;
    case "condition":
      return `condition: ${step.params.conditions
        .map((c) => conditionSummary(c.if))
        .slice(0, 1)
        .join("")} → иначе ${step.params.else}`;
    case "ai_answer":
      return `ai_answer · порог ${step.params.confidence_threshold ?? 0.6} → ${step.next}`;
    case "handoff":
      return `handoff · ${step.params.comment ? `«${clip(step.params.comment, 32)}»` : step.params.reason || "scenario"}`;
    case "close":
      return step.params.silent ? "close · молча" : `close · «${clip(step.params.text, 32)}»`;
    case "tag":
      return `tag: ${step.params.tags.join(", ") || "—"} → ${step.next}`;
    case "note":
      return `note: «${clip(step.params.text)}» → ${step.next}`;
  }
}

/** Расписание одной строкой для таблицы списка (11 §5.1). */
export function formatSchedule(schedule: BotSchedule | null | undefined): string {
  if (!schedule || schedule.always !== false) return "24/7";
  const intervals = schedule.intervals ?? [];
  if (intervals.length === 0) return "никогда";
  const tz = schedule.timezone ?? "Europe/Moscow";
  const tzLabel = tz === "Europe/Moscow" ? "МСК" : tz;
  const parts = intervals.map((iv) => {
    const days = iv.days ?? [];
    const label =
      days.length === 7 || days.length === 0
        ? "пн–вс"
        : WEEK_DAYS.filter((d) => days.includes(d.value))
            .map((d) => d.label)
            .join(", ");
    return `${label} ${iv.start}–${iv.end}`;
  });
  return `${parts.join("; ")} ${tzLabel}`;
}

/** Интервал через полночь (02 §2.5): 20:00–10:00. */
export function crossesMidnight(start: string, end: string): boolean {
  return start > end;
}

/** Лимиты-предохранители с учётом пере-определений сценария (02 §2.7). */
export function scenarioLimits(scenario: BotScenario) {
  const s = scenario.settings ?? {};
  return {
    max_steps_total: s.max_steps_total ?? 100,
    max_steps_per_tick: s.max_steps_per_tick ?? 20,
    max_bot_messages_row: s.max_bot_messages_row ?? 5,
    max_offscript_messages: s.max_offscript_messages ?? 2,
  };
}
