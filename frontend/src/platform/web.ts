import { BUILD_VERSION } from "./buildVersion";
import {
  EMPTY_FLUSH_REPORT,
  OfflineUnsupported,
  дополнитьСклейкой,
  type CachedSnapshot,
  type NotificationAction,
  type PlatformBridge,
} from "./bridge";
import { наЭкранеЛи, этаВкладкаОповещает } from "./наЭкранеЛи";
import { показатьЧерезВоркер, приДействииИзУведомления } from "./serviceWorker";
import { прочитатьСнимок, сохранитьСнимок, стеретьСнимок } from "./снимокСписка";

/**
 * Веб-мост (03 §7) — честный минимум, ничего не эмулирует: офлайн-очереди в
 * браузере нет, бейдж живёт в `document.title` (03 §3.5).
 *
 * Исключение одно и оно намеренное — СНИМОК СПИСКА в IndexedDB
 * (`снимокСписка.ts`): он не эмулирует офлайн, а лишь заполняет первый кадр
 * тем, что и так будет запрошено, и первый же ответ сервера его вытесняет.
 */
export function createWebBridge(): PlatformBridge {
  const listeners: Array<(a: NotificationAction) => void> = [];

  return {
    kind: "web",

    async notify(n) {
      // Разрешение сами не спрашиваем — это делает переключатель в /settings/profile.
      if (typeof Notification === "undefined") return;
      if (Notification.permission !== "granted") return;
      /*
       * ⚠ ЗАСЛОН ПО ФОКУСУ, А НЕ ПО ВИДИМОСТИ (07.09). Здесь стояло
       * `document.visibilityState === "visible" → выйти`: диспетчер, у
       * которого поверх LeadChat открыта CRM, считался «смотрящим» и не
       * получал НИЧЕГО — то есть уведомлений не было ровно в том случае, ради
       * которого они и нужны. Предикат теперь общий с десктопом (04 §4.1).
       */
      if (наЭкранеЛи()) return;
      // Вкладок у диспетчера 11–13 за смену, и каждая показала бы своё:
      // оповещает одна — та, куда человек вернётся (см. `наЭкранеЛи.ts`).
      if (!этаВкладкаОповещает()) return;

      const запрос = дополнитьСклейкой(n);
      /*
       * Сначала сервис-воркер: кнопки «Принять»/«Отклонить» бывают ТОЛЬКО у
       * него (`registration.showNotification`), у `new Notification` их нет.
       * `false` — воркера нет (не зарегистрировался, отдан не тем типом,
       * приватное окно): показываем сами, без кнопок, но показываем.
       */
      let показалВоркер = false;
      try {
        показалВоркер = await показатьЧерезВоркер(запрос);
      } catch (e) {
        console.warn("[уведомления] воркер показать не смог, показываю сам:", запрос.tag, e);
      }
      if (показалВоркер) return;

      // Иконка — та, что реально лежит в public/. Раньше здесь стоял
      // `/icon-192.png`, которого в сборке нет: браузер молча получал 404 и
      // рисовал свою заглушку. Молча — потому и жило.
      const note = new Notification(запрос.title, {
        body: запрос.body,
        icon: "/favicon.svg",
        // Десять сообщений одного клиента обязаны заменять друг друга.
        tag: запрос.tag,
        requireInteraction: запрос.requireInteraction,
      });
      note.onclick = () => {
        window.focus();
        // Системное уведомление центра приходит без диалога — переходить некуда.
        const convId = запрос.conversationId;
        if (!convId) return;
        for (const cb of listeners) cb({ conversationId: convId });
      };
    },

    async setBadge() {
      // Бейдж в вебе = title «(N) LeadChat» (03 §3.5) — здесь no-op.
    },

    onNotificationAction(cb) {
      listeners.push(cb);
      /*
       * Нажатие по уведомлению ИЗ ВОРКЕРА приходит не сюда, а в сам воркер:
       * страницы в этот момент может не быть вовсе. Он присылает выбранное
       * действие сообщением, и мост подаёт его тем же путём, что и клик по
       * своему `new Notification` — подписчик у платформы один.
       */
      const отписатьсяОтВоркера = приДействииИзУведомления((a) => {
        if (!a.conversationId) return;
        cb({ conversationId: a.conversationId, action: a.action });
      });
      return () => {
        отписатьсяОтВоркера();
        const i = listeners.indexOf(cb);
        if (i >= 0) listeners.splice(i, 1);
      };
    },

    offlineQueue: {
      async push() {
        throw new OfflineUnsupported(); // Composer покажет «нет соединения»
      },
      async drain() {
        return EMPTY_FLUSH_REPORT;
      },
      async size() {
        return 0;
      },
      async list() {
        return [];
      },
      async retry() {
        throw new OfflineUnsupported();
      },
      async remove() {
        throw new OfflineUnsupported();
      },
      onReport() {
        return () => {};
      },
    },

    convCache: {
      /**
       * Снимок списка из IndexedDB — веб-двойник десктопного SQLite (04 §5.4).
       * Отказ хранилища (приватное окно, запрет политикой, молчащая база)
       * возвращает null, то есть прежнее поведение «скелет до ответа сервера».
       */
      async warmup(): Promise<CachedSnapshot | null> {
        const запись = await прочитатьСнимок().catch(() => null);
        if (!запись) return null;
        return {
          dialogs: запись.dialogs,
          savedAt: запись.savedAt,
          ownerId: запись.ownerId,
          tab: запись.tab,
          counts: запись.counts,
        };
      },

      async persist(s: CachedSnapshot) {
        /*
         * ⚠ ПИШЕТ ТОЛЬКО ВИДИМАЯ ВКЛАДКА. У диспетчера 11–13 вкладок за смену,
         * и все они делят одну базу профиля: писали бы все — тринадцать
         * перезаписей одной и той же записи каждые две секунды, из которых
         * двенадцать про экраны, на которые никто не смотрит. Снимок означает
         * «последнее, что человек ВИДЕЛ», и скрытая вкладка про это не знает.
         * Тот же довод стоит у упреждающего обновления токена в `http.ts`.
         */
        if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
        /*
         * Снимок без владельца проверить некому — такой не храним вовсе. Это
         * не перестраховка: `clear()` при выходе стирает базу сразу, а запись,
         * назначенная до него, приходит после — и без этого замка легла бы
         * поверх стёртого.
         */
        if (!s.ownerId) return;
        await сохранитьСнимок({
          ownerId: s.ownerId,
          build: BUILD_VERSION,
          tab: s.tab ?? "all",
          dialogs: s.dialogs,
          counts: s.counts ?? null,
          savedAt: s.savedAt ?? new Date().toISOString(),
        });
      },

      async loadMessages() {
        // Ленты в вебе не кэшируем: снимок открывает СПИСОК, а лента приезжает
        // своим запросом, который и так идёт сразу за открытием диалога.
        return [];
      },

      // Выход из аккаунта и отзыв сессии — см. довод в `стеретьСнимок`.
      async clear() {
        await стеретьСнимок().catch(() => {});
      },
    },

    async openExternal(url) {
      window.open(url, "_blank", "noopener");
    },
  };
}
