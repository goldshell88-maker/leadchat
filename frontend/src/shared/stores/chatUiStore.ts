import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ConversationFilters } from "@/shared/api/queryKeys";
import type { MessageDto } from "@/shared/api/types";
import type { Role } from "@/shared/auth/usePermissions";
import { useInboxStore } from "./inboxStore";

/**
 * UI-состояние чатов (03 §2.2): только то, чего нет на сервере.
 * Активный диалог: источник истины — URL (/chats/:id), стор — зеркало для
 * не-роутерных потребителей (applyWsEvent решает, звучать ли звуку и считать ли
 * непрочитанным). Персистятся ТОЛЬКО черновики и звук; фильтры, активный диалог
 * и оверлей карточки — сессионные.
 *
 * Задел на будущее (спринт 2, хвост «в»): per-user read-маркеры здесь НЕ живут —
 * непрочитанные считает сервер (unreadStore лишь агрегирует). Когда маркеры станут
 * персональными, менять придётся unreadStore + 01 §5.1, а не этот стор.
 */

/** Черновик диалога: и текст, и режим — переживают переключение диалогов (03 §2.2). */
export interface Draft {
  text: string;
  isNote: boolean;
  /**
   * На какое сообщение отвечаем (просьба владельца 02.09).
   *
   * ⚠ ЛЕЖИТ В ЧЕРНОВИКЕ, А НЕ В СОСТОЯНИИ ЛЕНТЫ, И ЭТО НЕ СЛУЧАЙНОСТЬ. Выбор
   * «отвечаю на это» — часть незаконченного ответа наравне с набранным
   * текстом: диспетчер переключился на срочный диалог и вернулся — обязаны
   * сохраниться оба, иначе он отправит ответ не туда, не заметив пропажи
   * связи. Храним снимок, а не идентификатор: полоска над полем ввода должна
   * показывать текст сразу, не спрашивая сервер.
   */
  replyTo?: MessageDto | null;
}

interface ChatUiState {
  activeConversationId: string | null;
  filters: ConversationFilters;
  /**
   * Открыта вкладка «Входящие» (7.1). Отдельный флаг, а не пятое значение
   * `filters.tab`: очередь — не срез диалогов, а другой ресурс со своей
   * выборкой, своим порядком и своими действиями. Держать её внутри фильтров
   * значило бы гонять `q`, `account_id` и `assignee_id` по эндпоинту, который
   * их не понимает.
   */
  inboxOpen: boolean;
  /**
   * Имя сотрудника, по которому сужен список, — для чипа над ним.
   *
   * ⚠ ЖИВЁТ РЯДОМ С ФИЛЬТРАМИ, А НЕ ВНУТРИ НИХ, И ЭТО ВАЖНО. `filters` целиком
   * уходит в ключ кэша TanStack Query (`qk.conversations.list`), а имя — не
   * условие выборки, а подпись к ней: положи мы его внутрь, и переименование
   * сотрудника заводило бы второй кэш на тот же самый список.
   *
   * `null` значит «сужение стоит, а имя неизвестно» — так бывает, когда
   * `assigneeId` пришёл откуда-то, кроме отчёта. Чип в этом случае всё равно
   * показывается: важнее сказать, что список сужен, чем кем именно.
   */
  assigneeLabel: string | null;
  soundEnabled: boolean;
  /** Оверлей правой карточки на узких экранах (порог — NARROW_MAX). */
  clientCardOpen: boolean;
  drafts: Record<string, Draft>;
  /** Чьи это черновики: чужие после смены пользователя не показываем. */
  draftsOwnerId: string | null;
  /**
   * Чья личность (`${userId}:${role}`) сейчас разложена по экрану — память
   * `useRoleUiSync`. Живёт в сторе, а НЕ в `useRef` хука: принудительный
   * разлогин (кадр 4403, «сотрудник отключён») размонтирует `AppLayout`, и
   * память внутри компонента умерла бы вместе с ним — следующий человек в той
   * же вкладке выглядел бы «первым входом», и кэш предыдущего не чистился бы.
   * Сессионная (не в `partialize`): после перезагрузки вкладки кэша нет и так.
   */
  uiIdentity: string | null;

  setActive(id: string | null): void;
  /**
   * Любая правка фильтров означает «смотрю обычный список» — очередь закрывается.
   *
   * `assigneeLabel` передаётся ВМЕСТЕ с `assigneeId` тем, кто знает имя (отчёт
   * по сотрудникам). Правило внутри простое и намеренно строгое: тронули
   * `assigneeId` без имени — имя обнуляется. Иначе чип показывал бы прежнего
   * сотрудника при новом сужении, а это хуже, чем не показывать никого.
   */
  setFilters(p: Partial<ConversationFilters> & { assigneeLabel?: string | null }): void;
  setInboxOpen(v: boolean): void;
  setSoundEnabled(v: boolean): void;
  setClientCardOpen(v: boolean): void;
  setDraft(convId: string, d: Draft): void;
  patchDraft(convId: string, p: Partial<Draft>): void;
  clearDraft(convId: string): void;
  /** Сброс UI-состояния под роль (вход, смена роли, смена пользователя). */
  resetForRole(user: { id: string; role: Role }): void;
}

export const DEFAULT_FILTERS: ConversationFilters = { tab: "all" };

/**
 * Вкладка по умолчанию (11 §2.5): менеджер начинает со «Своих», остальные
 * роли — со «Всех» (руководитель и наблюдатель смотрят весь поток).
 */
export function defaultTabForRole(role: Role | undefined): ConversationFilters["tab"] {
  return role === "manager" ? "mine" : "all";
}

/** Стабильная ссылка: селектор черновика не должен дёргать ререндер новым объектом. */
export const EMPTY_DRAFT: Draft = { text: "", isNote: false };

export const useChatUiStore = create<ChatUiState>()(
  persist(
    (set) => ({
      activeConversationId: null,
      filters: DEFAULT_FILTERS,
      assigneeLabel: null,
      inboxOpen: false,
      soundEnabled: true,
      clientCardOpen: false,
      drafts: {},
      draftsOwnerId: null,
      uiIdentity: null,

      setActive: (id) => set({ activeConversationId: id }),
      setFilters: ({ assigneeLabel, ...p }) =>
        set((s) => ({
          filters: { ...s.filters, ...p },
          assigneeLabel:
            "assigneeId" in p ? (p.assigneeId ? (assigneeLabel ?? null) : null) : s.assigneeLabel,
          inboxOpen: false,
        })),
      setInboxOpen: (v) => set({ inboxOpen: v }),
      setSoundEnabled: (v) => set({ soundEnabled: v }),
      setClientCardOpen: (v) => set({ clientCardOpen: v }),

      setDraft: (convId, d) => set((s) => ({ drafts: { ...s.drafts, [convId]: d } })),

      patchDraft: (convId, p) =>
        set((s) => ({
          drafts: { ...s.drafts, [convId]: { ...(s.drafts[convId] ?? EMPTY_DRAFT), ...p } },
        })),

      /**
       * Очистить поле после отправки.
       *
       * ⚠ РЕЖИМ ЗАМЕТКИ ПЕРЕЖИВАЕТ ОЧИСТКУ, И ЭТО НЕ УДОБСТВО, А БЕЗОПАСНОСТЬ
       * (обход экранов 31.08). Здесь удалялась вся запись черновика целиком —
       * вместе с признаком `isNote`. Композер читает черновик и, не найдя
       * записи, берёт умолчание `isNote: false`: жёлтое поле «Заметка» само
       * перекрашивалось в обычное «Сообщение».
       *
       * Оператор этого не заказывал. Он пишет для своих — «клиенту скидку не
       * обещать, клиент мутный», — жмёт Enter, поле очищается, он продолжает
       * ту же мысль и жмёт Enter снова. Вторая строка уходит КЛИЕНТУ в Авито,
       * где отозвать сообщение нельзя.
       *
       * Что это была случайность, а не решение, видно по ветке ошибки в
       * `useSendMessage`: при неудаче режим восстанавливают явно
       * (`setDraft(convId, { text, isNote })`). То есть при провале мы его
       * бережём, а при успехе теряли.
       *
       * Обычное сообщение по-прежнему стирает запись целиком: черновики лежат
       * в localStorage, и держать там пустышку на каждый открытый диалог
       * незачем. Пустышка остаётся только у заметки — там она несёт смысл.
       */
      clearDraft: (convId) =>
        set((s) => {
          const было = s.drafts[convId];
          if (!было) return s;
          if (было.isNote) {
            return { drafts: { ...s.drafts, [convId]: { text: "", isNote: true } } };
          }
          const next = { ...s.drafts };
          delete next[convId];
          return { drafts: next };
        }),

      /**
       * Смена роли меняет сам смысл экрана (у head нет «Моих», у observer нет
       * фильтра по менеджеру), поэтому сессионное состояние сбрасывается целиком:
       * фильтры → дефолт роли, оверлей карточки закрыт. Черновики переживают
       * перезагрузку, но НЕ переезжают к другому пользователю на том же компьютере.
       */
      resetForRole: ({ id, role }) =>
        set((s) => {
          /**
           * Очередь — тоже личное состояние: у другого человека свой набор
           * каналов (7.2) и свой счётчик, а наблюдателю и руководителю очередь
           * не положена вовсе. Оставить число от предыдущего сотрудника — то же
           * самое, что оставить его кэш, а этот дефект уже ловили однажды.
           */
          useInboxStore.getState().clear();
          return {
            filters: { tab: defaultTabForRole(role) },
            // Фильтры обнулились — подпись к ним тоже, иначе следующий человек
            // увидел бы чип с чужим именем над своим полным списком.
            assigneeLabel: null,
            inboxOpen: false,
            clientCardOpen: false,
            // activeConversationId НЕ трогаем: источник истины — URL (03 §6), и на
            // холодном старте /chats/:id эффект страницы отрабатывает раньше этого.
            drafts: s.draftsOwnerId === id ? s.drafts : {},
            draftsOwnerId: id,
            uiIdentity: `${id}:${role}`,
          };
        }),
    }),
    {
      name: "leadchat-chat-ui",
      partialize: (s) => ({
        soundEnabled: s.soundEnabled,
        drafts: s.drafts,
        draftsOwnerId: s.draftsOwnerId,
      }),
    },
  ),
);

/** Черновик конкретного диалога (пустой — общая стабильная ссылка). */
export function selectDraft(convId: string | null) {
  return (s: ChatUiState): Draft => (convId ? (s.drafts[convId] ?? EMPTY_DRAFT) : EMPTY_DRAFT);
}
