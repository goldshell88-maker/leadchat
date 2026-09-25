import { create } from "zustand";
import type {
  BotDetail,
  BotAiProvider,
  BotMode,
  BotScenario,
  BotScenarioIssue,
  BotSchedule,
  BotStep,
  BotStepType,
  BotUpdateInput,
} from "@/shared/api/types";
import { isTerminal, makeStep, renameRefs, uniqueStepId } from "./scenario";

/**
 * Черновик редактора бота (02 §5.1: «черновик живёт в Zustand-сторе и не
 * отправляется на сервер до „Сохранить"»). Здесь же — снимок серверного
 * состояния для кнопки «Отменить изменения» и список ошибок валидации,
 * пришедших из `422`.
 */

const EMPTY_SCENARIO: BotScenario = { version: 1, entry: "", steps: [] };

interface Snapshot {
  name: string;
  isEnabled: boolean;
  /**
   * Поставщик ответа. У обычного бота он ВСЕГДА `claude`, и менять его
   * больше нечем: лид-бот с 12 августа — отдельная система со своим
   * разделом («Настройки → Лид-бот»), а не выбор мозга у бота.
   * Поле осталось, потому что едет в теле запроса и хранится у бота.
   */
  aiProvider: BotAiProvider;
  mode: BotMode;
  accountIds: string[];
  schedule: BotSchedule;
  knowledgeBase: string;
  scenario: BotScenario;
}

interface BotDraftState extends Snapshot {
  botId: string | null;
  loaded: boolean;
  dirty: boolean;
  /** Ошибки последнего ответа сервера (истина, 02 §5.2). */
  serverIssues: BotScenarioIssue[];
  /** Раскрытые карточки-шаги (Accordion multiple). */
  openSteps: string[];
  /** Подсвеченная карточка после клика по строке ошибки. */
  focusedStepId: string | null;

  load: (detail: BotDetail, accountIds: string[]) => void;
  markSaved: (detail: BotDetail, accountIds: string[]) => void;
  revert: () => void;

  setName: (v: string) => void;
  setEnabled: (v: boolean) => void;
  setMode: (v: BotMode) => void;
  setAccounts: (ids: string[]) => void;
  setSchedule: (s: BotSchedule) => void;
  setKnowledge: (v: string) => void;
  setEntry: (id: string) => void;

  addStep: (type: BotStepType, index: number) => void;
  removeStep: (id: string) => void;
  moveStep: (id: string, delta: -1 | 1) => void;
  renameStep: (id: string, nextId: string) => void;
  replaceStep: (id: string, step: BotStep) => void;
  changeStepType: (id: string, type: BotStepType) => void;

  setOpenSteps: (ids: string[]) => void;
  openStep: (id: string) => void;
  focusStep: (id: string | null) => void;
  setServerIssues: (issues: BotScenarioIssue[]) => void;
  toInput: () => BotUpdateInput;
}

function snapshotOf(detail: BotDetail, accountIds: string[]): Snapshot {
  return {
    name: detail.name,
    isEnabled: detail.is_enabled,
    aiProvider: detail.ai_provider ?? "claude",
    mode: detail.mode ?? "suggest",
    accountIds: [...accountIds],
    schedule: structuredClone(detail.schedule ?? { always: true }),
    knowledgeBase: detail.knowledge_base ?? "",
    scenario: structuredClone(detail.scenario ?? EMPTY_SCENARIO),
  };
}

/** Правка сценария всегда идёт через это: свежий объект + dirty + сброс 422. */
function withScenario(
  state: BotDraftState,
  mutate: (scenario: BotScenario) => BotScenario,
): Partial<BotDraftState> {
  return { scenario: mutate(structuredClone(state.scenario)), dirty: true, serverIssues: [] };
}

let snapshot: Snapshot | null = null;

export const useBotDraft = create<BotDraftState>((set, get) => ({
  botId: null,
  loaded: false,
  dirty: false,
  serverIssues: [],
  openSteps: [],
  focusedStepId: null,
  name: "",
  isEnabled: false,
  aiProvider: "claude",
  // Умолчание совпадает с серверным: пока деталь не пришла, показываем самое
  // безопасное, а не «отвечает клиенту сам».
  mode: "suggest",
  accountIds: [],
  schedule: { always: true },
  knowledgeBase: "",
  scenario: EMPTY_SCENARIO,

  load: (detail, accountIds) => {
    snapshot = snapshotOf(detail, accountIds);
    set({
      botId: detail.id,
      loaded: true,
      dirty: false,
      serverIssues: [],
      openSteps: [],
      focusedStepId: null,
      ...structuredClone(snapshot),
    });
  },

  markSaved: (detail, accountIds) => {
    snapshot = snapshotOf(detail, accountIds);
    set({ dirty: false, serverIssues: [], ...structuredClone(snapshot) });
  },

  revert: () => {
    if (!snapshot) return;
    set({ dirty: false, serverIssues: [], focusedStepId: null, ...structuredClone(snapshot) });
  },

  setName: (v) => set({ name: v, dirty: true }),
  // Вкл/выкл применяется отдельным endpoint'ом сразу (01 §8.4) — это не часть
  // черновика: правим и снимок тоже, иначе «Отменить изменения» вернёт ложь.
  setEnabled: (v) => {
    if (snapshot) snapshot.isEnabled = v;
    set({ isEnabled: v });
  },
  // Режим — часть черновика (в отличие от вкл/выкл): он уезжает в PUT вместе со
  // сценарием, потому что переключение «подсказка → автоответ» почти всегда делается
  // вместе с правкой текстов, и применять его раньше «Сохранить» нельзя.
  setMode: (v) => set({ mode: v, dirty: true }),
  setAccounts: (ids) => set({ accountIds: ids, dirty: true }),
  setSchedule: (s) => set({ schedule: s, dirty: true }),
  setKnowledge: (v) => set({ knowledgeBase: v, dirty: true, serverIssues: [] }),
  setEntry: (id) => set((s) => withScenario(s, (sc) => ({ ...sc, entry: id }))),

  addStep: (type, index) =>
    set((state) =>
      withScenario(state, (sc) => {
        const at = Math.max(0, Math.min(index, sc.steps.length));
        // Новый шаг ведёт на следующий по списку, а если его нет — на первый
        // терминальный: свежая карточка не должна сразу давать broken_ref.
        const after = sc.steps[at];
        const terminal = sc.steps.find((s) => isTerminal(s.type));
        const fallback = after?.id ?? terminal?.id ?? "";
        const step = makeStep(type, uniqueStepId(sc.steps, type), fallback);
        sc.steps.splice(at, 0, step);
        if (!sc.entry) sc.entry = step.id;
        return sc;
      }),
    ),

  removeStep: (id) =>
    set((state) => {
      const patch = withScenario(state, (sc) => {
        sc.steps = sc.steps.filter((s) => s.id !== id);
        // Ссылки на удалённый шаг: nullable-ветки уходят в дефолт «передать
        // менеджеру», обязательные остаются битыми — их починку валидатор
        // требует явно, угадывать переход за админа мы не вправе.
        sc.steps = sc.steps.map((s) => nullOutRefs(s, id));
        if (sc.entry === id) sc.entry = sc.steps[0]?.id ?? "";
        return sc;
      });
      return { ...patch, openSteps: state.openSteps.filter((s) => s !== id) };
    }),

  moveStep: (id, delta) =>
    set((state) =>
      withScenario(state, (sc) => {
        const from = sc.steps.findIndex((s) => s.id === id);
        const to = from + delta;
        if (from < 0 || to < 0 || to >= sc.steps.length) return sc;
        const [step] = sc.steps.splice(from, 1);
        sc.steps.splice(to, 0, step);
        return sc;
      }),
    ),

  renameStep: (id, nextIdRaw) =>
    set((state) => {
      const nextId = nextIdRaw.trim();
      if (!nextId || nextId === id) return {};
      const patch = withScenario(state, (sc) => {
        /*
         * Занятый идентификатор здесь ИГНОРИРУЕТСЯ БЕЗ СЛОВ — и это правильно
         * ровно потому, что говорить некому: стор не рисует. Объяснение живёт
         * там, где человек его увидит: StepCard.commitId проверяет занятость
         * до вызова и оставляет набранное в поле с подписью «уже занят»
         * (BOT-04). Здесь остаётся последний рубеж от дубликата id — на него
         * опираются ссылки между шагами, и второй такой же сломал бы сценарий
         * молча и необратимо.
         */
        if (sc.steps.some((s) => s.id === nextId)) return sc;
        sc.steps = sc.steps.map((s) => renameRefs(s.id === id ? { ...s, id: nextId } : s, id, nextId));
        if (sc.entry === id) sc.entry = nextId;
        return sc;
      });
      return {
        ...patch,
        openSteps: state.openSteps.map((s) => (s === id ? nextId : s)),
        focusedStepId: state.focusedStepId === id ? nextId : state.focusedStepId,
      };
    }),

  replaceStep: (id, step) =>
    set((state) =>
      withScenario(state, (sc) => {
        sc.steps = sc.steps.map((s) => (s.id === id ? step : s));
        return sc;
      }),
    ),

  changeStepType: (id, type) =>
    set((state) =>
      withScenario(state, (sc) => {
        const index = sc.steps.findIndex((s) => s.id === id);
        if (index < 0) return sc;
        const old = sc.steps[index];
        if (old.type === type) return sc;
        // Смена типа сбрасывает params (02 §5.1) — сохраняем только id и,
        // если оба типа нетерминальны, основной переход.
        const keepNext = !isTerminal(old.type) && !isTerminal(type) && "next" in old ? old.next : "";
        const terminal = sc.steps.find((s) => isTerminal(s.type) && s.id !== id);
        sc.steps[index] = makeStep(type, id, keepNext || terminal?.id || "");
        return sc;
      }),
    ),

  setOpenSteps: (ids) => set({ openSteps: ids }),
  openStep: (id) => set((s) => (s.openSteps.includes(id) ? {} : { openSteps: [...s.openSteps, id] })),
  focusStep: (id) => set({ focusedStepId: id }),
  setServerIssues: (issues) => set({ serverIssues: issues }),

  toInput: () => {
    const s = get();
    return {
      name: s.name.trim(),
      ai_provider: s.aiProvider,
      mode: s.mode,
      schedule: s.schedule,
      scenario: s.scenario,
      knowledge_base: s.knowledgeBase,
    };
  },
}));

/** Ссылки на удалённый шаг: nullable — в null, обязательные не трогаем. */
function nullOutRefs(step: BotStep, removed: string): BotStep {
  switch (step.type) {
    case "ask":
      return {
        ...step,
        on_timeout: step.on_timeout === removed ? null : (step.on_timeout ?? null),
        on_invalid: step.on_invalid === removed ? null : (step.on_invalid ?? null),
      };
    case "menu":
      return {
        ...step,
        on_no_match: step.on_no_match === removed ? null : (step.on_no_match ?? null),
        on_timeout: step.on_timeout === removed ? null : (step.on_timeout ?? null),
      };
    case "ai_answer":
      return {
        ...step,
        on_low_confidence: step.on_low_confidence === removed ? null : (step.on_low_confidence ?? null),
      };
    default:
      return step;
  }
}
