import { useBotDraft } from "@/features/settings/bots/draftStore";
import type { Permission } from "@/shared/auth/usePermissions";
import type { AvitoAccountsPage, BotDetail, BotScenario, BotsPage } from "@/shared/api/types";

/** Фикстуры экранов ботов: деталь бота, список и сброс черновика-стора. */

export const BOT_ID = "bot-1";

/**
 * Сценарий для тестов редактора: все основные типы шагов и переходов на
 * знакомых id (`greet`, `ask_problem`, `ask_phone`, `tag_contact`…).
 *
 * Это ДАННЫЕ ТЕСТОВ, а не шаблон нового бота: шаблон один и живёт на сервере
 * (`app/bots/scenarios/primary_intake.json`), «Создать бота» берёт его оттуда
 * (проверка 24.09). Отсюда и расхождения — например, `close_silent` по
 * таймауту: в тестах он проверяет форму шага `close`, в бою его нет.
 */
export const SAMPLE_SCENARIO: BotScenario = {
  version: 1,
  revision: 1,
  entry: "greet",
  settings: { max_bot_messages_row: 5, max_offscript_messages: 2 },
  steps: [
    {
      id: "greet",
      type: "send",
      params: {
        text: "Здравствуйте, {client_name}! Это сервис Lead Partner 👋\nПодскажите, что случилось с техникой — модель и проблему?",
      },
      next: "ask_problem",
    },
    {
      id: "ask_problem",
      type: "ask",
      params: {
        text: null,
        var: "problem",
        validate: "any",
        retry_text: null,
        max_attempts: 1,
        timeout: "24h",
      },
      next: "ai_draft",
      on_timeout: "close_silent",
      on_invalid: null,
    },
    {
      id: "ai_draft",
      type: "ai_answer",
      params: { confidence_threshold: 0.6, max_reply_len: 800, context_messages: 10 },
      next: "check_hours",
      on_low_confidence: null,
    },
    {
      id: "check_hours",
      type: "condition",
      params: {
        conditions: [
          {
            if: { kind: "work_hours", from: "10:00", to: "20:00", timezone: "Europe/Moscow" },
            next: "handoff_day",
          },
        ],
        else: "night_msg",
      },
    },
    {
      id: "handoff_day",
      type: "handoff",
      params: {
        reason: "scenario",
        comment: "Клиент описал проблему, бот дал предварительный ответ",
        tags: ["первичный-приём"],
      },
    },
    {
      id: "night_msg",
      type: "send",
      params: { text: "Мастер ответит утром. Оставьте телефон — перезвоним первыми ✔" },
      next: "ask_phone",
    },
    {
      id: "ask_phone",
      type: "ask",
      params: {
        text: null,
        var: "phone",
        validate: "phone",
        retry_text: "Кажется, это не номер телефона 🙂 Напишите в формате +7 900 000-00-00",
        max_attempts: 2,
        timeout: "12h",
      },
      next: "tag_contact",
      on_timeout: "handoff_night",
      on_invalid: "handoff_night",
    },
    {
      id: "tag_contact",
      type: "tag",
      params: { tags: ["контакт собран"] },
      next: "note_contact",
    },
    {
      id: "note_contact",
      type: "note",
      params: { text: "🤖 Бот собрал контакт: {phone}\nПроблема со слов клиента: {problem}" },
      next: "handoff_night",
    },
    {
      id: "handoff_night",
      type: "handoff",
      params: {
        reason: "scenario",
        comment: "Ночной диалог: проблема зафиксирована, перезвонить утром первыми",
        tags: ["ночной-лид"],
      },
    },
    {
      id: "close_silent",
      type: "close",
      params: { text: null, silent: true },
    },
  ],
};

/** Набор прав admin (01 §12) — боты доступны только ему. */
export const ADMIN_PERMISSIONS: Permission[] = [
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
];

export function botDetail(overrides: Partial<BotDetail> = {}): BotDetail {
  return {
    id: BOT_ID,
    name: "Первичный приём",
    is_enabled: true,
    schedule: { always: true },
    scenario: structuredClone(SAMPLE_SCENARIO),
    knowledge_base: "Замена экрана iPhone 13 — от 8900 ₽, срок 1–2 часа.",
    // Боевые умолчания: думает наш Claude, клиенту молчит. Кейсы про отправку
    // и про лид-бота задают эти поля сами.
    ai_provider: "claude",
    mode: "suggest",
    ...overrides,
  };
}

export function botsPage(): BotsPage {
  return {
    items: [
      {
        id: BOT_ID,
        name: "Первичный приём",
        is_enabled: true,
        schedule: { always: true },
        accounts: [{ id: "acc-1", title: "LP-Москва" }],
        scenario_steps_count: 11,
        knowledge_base_present: true,
        ai_provider: "leadbot",
        mode: "auto",
        conversations_7d: 154,
      },
      {
        id: "bot-2",
        name: "Ночной дежурный",
        is_enabled: false,
        schedule: {
          always: false,
          timezone: "Europe/Moscow",
          intervals: [
            { days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], start: "20:00", end: "10:00" },
          ],
        },
        accounts: [],
        scenario_steps_count: 4,
        ai_provider: "claude",
        mode: "suggest",
        knowledge_base_present: false,
        conversations_7d: 31,
      },
    ],
    page: { limit: 50, offset: 0, total: 2 },
  };
}

export function avitoAccountsPage(): AvitoAccountsPage {
  return {
    items: [
      {
        id: "acc-1",
        title: "LP-Москва",
        avito_user_id: 111,
        status: "active",
        token_expires_at: null,
        webhook: { status: "ok", url: null, last_event_at: null },
        bot_id: BOT_ID,
        created_at: "2026-07-01T10:00:00Z",
      },
      {
        id: "acc-2",
        title: "LP-Химки",
        avito_user_id: 222,
        status: "active",
        token_expires_at: null,
        webhook: { status: "ok", url: null, last_event_at: null },
        bot_id: null,
        created_at: "2026-07-02T10:00:00Z",
      },
    ],
    page: { limit: 50, offset: 0, total: 2 },
  };
}

/** Сценарий с битой ссылкой — локальный валидатор обязан её поймать. */
export function brokenScenario(): BotScenario {
  const scenario = structuredClone(SAMPLE_SCENARIO);
  const askPhone = scenario.steps.find((s) => s.id === "ask_phone");
  if (askPhone && askPhone.type === "ask") askPhone.next = "tag_contct";
  return scenario;
}

/** Сценарий с предупреждением: ask без таймаута — бот может ждать вечно. */
export function warningScenario(): BotScenario {
  const scenario = structuredClone(SAMPLE_SCENARIO);
  const askPhone = scenario.steps.find((s) => s.id === "ask_phone");
  if (askPhone && askPhone.type === "ask") askPhone.params.timeout = null;
  return scenario;
}

/** Стор черновика — модульный синглтон, между тестами его надо обнулять. */
export function resetBotDraft(): void {
  useBotDraft.setState({
    botId: null,
    loaded: false,
    dirty: false,
    serverIssues: [],
    openSteps: [],
    focusedStepId: null,
    name: "",
    isEnabled: false,
    accountIds: [],
    schedule: { always: true },
    knowledgeBase: "",
    scenario: { version: 1, entry: "", steps: [] },
  });
}
