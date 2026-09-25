import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { MessageDto, MessagesPage } from "@/shared/api/types";
import { patchMessageInCache } from "@/shared/realtime/applyWsEvent";
import type { FlushReport } from "@/platform/bridge";

/**
 * Дедупликация оптимистичной строки по `client_message_id` (04 §5.4).
 *
 * Оптимистичный ⏳-пузырь отличается тем, что его `id === client_message_id`
 * (03 §3.4). Когда то же самое сообщение приходит с сервера — ответом API или
 * событием WS `message:new` (01 §6.1: сервер возвращает `client_message_id`) —
 * серверная строка побеждает, локальная убирается. Это единственный
 * содержательный «конфликт» офлайна: ответ 2xx мог потеряться, а сообщение
 * уже доставлено (04 §5.4).
 */

function isTempRow(m: MessageDto): boolean {
  return Boolean(m.client_message_id) && m.id === m.client_message_id;
}

/**
 * Дошёл ли серверный близнец этого сообщения (по `client_message_id`).
 *
 * ⚠ ЗАЧЕМ ЭТО НУЖНО ВЕБУ. Сервер публикует `message:new` ВНУТРИ обработчика
 * POST, до ответа, и кадр приходит в том числе автору. Если ответ потерялся по
 * дороге — обрыв, таймаут, 502 после коммита, — сообщение у клиента УЖЕ есть, а
 * отправляющая вкладка видит только отказ. Единственный надёжный признак того,
 * что оно всё-таки ушло, — тот самый близнец в ленте.
 */
export function serverTwinArrived(convId: string, clientMessageId: string): boolean {
  const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId));
  if (!data) return false;
  return data.pages.some((page) =>
    page.items.some((m) => m.client_message_id === clientMessageId && !isTempRow(m)),
  );
}

/** Убрать ⏳-строки, у которых уже есть серверный близнец. true — что-то удалили. */
export function dedupeOptimisticRows(convId: string): boolean {
  const key = qk.messages.list(convId);
  const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(key);
  if (!data) return false;

  const arrived = new Set<string>();
  for (const page of data.pages) {
    for (const m of page.items) {
      if (m.client_message_id && !isTempRow(m)) arrived.add(m.client_message_id);
    }
  }
  if (arrived.size === 0) return false;

  let changed = false;
  const pages = data.pages.map((page) => {
    const items = page.items.filter((m) => !(isTempRow(m) && arrived.has(m.id)));
    if (items.length === page.items.length) return page;
    changed = true;
    return { ...page, items };
  });
  if (changed) queryClient.setQueryData(key, { ...data, pages });
  return changed;
}

/**
 * Дедуп на каждое изменение ленты. В десктопе ответа POST в JS нет вовсе —
 * серверная строка приходит только по WS, поэтому подписка обязательна;
 * в вебе не включается, поведение браузера не меняется.
 */
export function installOptimisticDedup(): () => void {
  let running = false;
  return queryClient.getQueryCache().subscribe((event) => {
    if (running || event.type !== "updated") return;
    const key = event.query.queryKey;
    if (!Array.isArray(key) || key[0] !== "messages" || typeof key[1] !== "string") return;
    running = true;
    try {
      dedupeOptimisticRows(key[1]);
    } finally {
      running = false;
    }
  });
}

/**
 * Отчёт `outbox_flush` (04 §8.2) → статусы пузырей: 409/422/403 красят строку
 * в ✗ с причиной, рядом «Повторить»/«Удалить»; оставшиеся в очереди — ⏳.
 */
export function applyOutboxReport(report: FlushReport): void {
  for (const f of report.failed) {
    patchMessageInCache(f.conversationId, f.clientMessageId, {
      delivery_status: "failed",
      delivery_error: f.error,
    });
  }
}

/** Строка очереди снова в работе («Повторить»): ⏳ вместо ✗. */
export function markOutboxPending(convId: string, clientMessageId: string): void {
  patchMessageInCache(convId, clientMessageId, { delivery_status: "pending", delivery_error: null });
}

/** «Удалить»: убрать пузырь из ленты вслед за строкой очереди. */
export function dropOptimisticRow(convId: string, clientMessageId: string): void {
  const key = qk.messages.list(convId);
  const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(key);
  if (!data) return;
  let changed = false;
  const pages = data.pages.map((page) => {
    const items = page.items.filter((m) => m.id !== clientMessageId);
    if (items.length === page.items.length) return page;
    changed = true;
    return { ...page, items };
  });
  if (changed) queryClient.setQueryData(key, { ...data, pages });
}
