import { queryClient } from "@/app/queryClient";
import { toastForNotification } from "@/platform/toast";
import { qk } from "@/shared/api/queryKeys";
import type { NotificationDto, NotifyEventData } from "@/shared/api/types";
import { notifyAssignedToMe as playAlertBeep } from "@/shared/realtime/notify";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { audienceAllowed, severityFromLevel } from "./catalog";
import { useNotificationStore } from "./store";
import { showToast, type ToastTone } from "@/shared/ui/toast";
import type { NotificationSeverity } from "@/shared/api/types";

/**
 * Важность уведомления → тон тоста.
 *
 * ЗЕЛЁНЫЙ ОЗНАЧАЕТ УСПЕХ, И ЭТО БЫЛО ГЛАВНОЙ БЕДОЙ (NOTIF-03). Обычная
 * важность уходила сюда как `color: "lp"`, а «lp» в таблице совместимости
 * тонов — это `success`: зелёный тост с галочкой ✓. Галочку получали
 * «Сертификат скоро истекает», «AI временно недоступен», «Клиент вернулся в
 * закрытый диалог», «Вам передали диалог» — всё, что сервер объявил
 * `severity="info"`. Человек читает галочку как «всё получилось» и закрывает
 * тост, хотя ему сообщили о беде или о свалившейся работе.
 *
 * Таблица нужна отдельным объектом, а не тернарником по месту: тон обязан
 * покрывать КАЖДУЮ важность, и забытое значение здесь роняет `tsc`, а не тихо
 * доезжает до экрана зелёным.
 */
const TONE_BY_SEVERITY: Record<NotificationSeverity, ToastTone> = {
  critical: "danger",
  warning: "warning",
  info: "info",
};

/**
 * WS-событие `notify` → центр уведомлений (14 §4 «Доставка»).
 *
 * Событие в каталоге протокола было с самого начала (01 §11.3) и до сих пор
 * работало тостом. Теперь у него две жизни:
 *
 *  - есть `id` — это строка таблицы `notifications`: кладём её в центр,
 *    счётчик колокольчика растёт, критичное поднимает плашку;
 *  - `id` нет — прежнее служебное сообщение («история загружена»), показываем
 *    тостом ровно как раньше. Класть в центр нечего: записи в базе нет,
 *    и «прочитано» ей некуда сохраниться.
 */

/**
 * ЛИЧНЫЕ ВИДЫ, КОТОРЫЕ СТОЯТ ОКНА БРАУЗЕРА.
 *
 * ⚠ ДО 07.09 ДО ДИСПЕТЧЕРА НЕ ДОХОДИЛО НИ ОДНОЙ ЗАПИСИ ЦЕНТРА. Мост звали
 * только при `severity === "critical"`, а в каталоге сервера
 * (`app/services/notifications.py`, KINDS) КАЖДЫЙ критичный вид адресован
 * `audience="admin"`: аккаунт, копии, планировщик, диск, отобранный канал.
 * Условие было выполнимо только у администратора — то есть у одного человека
 * из тридцати трёх.
 *
 * Правило теперь по АДРЕСНОСТИ, а не по важности: `audience_hint === null`
 * значит «сервер выбрал получателя поимённо» (см. правило в шапке
 * `platform/toast.ts`). Но одной адресности мало — личных видов в каталоге
 * восемь, и три из них дали бы вторую карточку о том, о чём человеку уже
 * сказали. Поэтому список поимённый, и вот он с доводами.
 *
 * ВКЛЮЧЕНЫ (5):
 *  • `conversation.awaiting_you` — «Клиент ждёт вашего ответа». Личное
 *    напоминание ответственному; лестница `awaiting.py` (15 мин, потом 30, 60…)
 *    и склейка по диалогу держат его редким. Ради этого случая всё и делается:
 *    человек с CRM поверх LeadChat узнаёт, что его клиент ждёт.
 *  • `message.undelivered` — «Ваш ответ не дошёл до клиента». Получатель —
 *    АВТОР сообщения; кроме него повторить ответ некому. Склейка по диалогу:
 *    десять неудач в одном диалоге — одна карточка.
 *  • `conversation.transfer_declined` — «От передачи отказались». Диалог молча
 *    вернулся на передавшего, клиент ждёт, и на экране этого не видно.
 *  • `conversation.transfer_expired` — «Передачу не приняли». То же самое, но
 *    решили часы; до 11.08 об этом не узнавал никто вовсе.
 *  • `conversation.transfer_cancelled` — «Передачу отменили». Единственная
 *    весть в сторону ПОЛУЧАТЕЛЯ после предложения — и она обязана дойти именно
 *    карточкой: карточка предложения висит с `requireInteraction` и сама не
 *    гаснет, а замещает её как раз эта (`platform/toast.ts`, `тегЗаписиЦентра`).
 *    Все три вида передачи редки: 119 предложений за 30 дней на всю компанию.
 *
 * НЕ ВКЛЮЧЕНЫ (3) — каждый молчит по своей причине, а не «на всякий случай»:
 *  • `conversation.assigned` («Вам передали диалог») — ДУБЛЬ. О передаче уже
 *    уведомляет `toastForHandoff` из кадра `conversation:assigned`, и у той
 *    карточки есть кнопки «Принять»/«Отклонить». Теги разные, склеиться они не
 *    могут: вышло бы две карточки об одном событии, причём вторая — без кнопок.
 *  • `conversation.reopened` («Клиент вернулся в закрытый диалог») — тот же
 *    диалог тем же движением встаёт во «Входящие», и о нём уже сказала карточка
 *    очереди (`lc-inbox`). Сервер объявил вид `info` со словами «здесь — только
 *    „имей в виду“»; частоту возвратов в бою я измерить не смог (доступа к
 *    прод-базе из этой сессии нет), а включать неизмеренное в поток, который
 *    уже уведомляет, — ровно тот способ получить лавину.
 *  • `conversation.closed_by_other` («Ваш диалог закрыл коллега») — 16 случаев
 *    за месяц против 14 639 обычных закрытий, действия не требует, и сервер
 *    намеренно выбрал ему `info`: «звонить на всю комнату дороже пользы».
 *    Карточка браузера громче плашки, в которой ему отказали.
 *
 * Список — строки видов сервера; сверку каталогов держит
 * `tests/unit/test_notification_catalog.py`.
 */
const ЛИЧНЫЕ_В_ОКНО: ReadonlySet<string> = new Set([
  // Коллега позвал в диалог: другого сигнала у приглашения нет вовсе.
  "conversation.invited",
  "conversation.awaiting_you",
  "message.undelivered",
  "conversation.transfer_declined",
  "conversation.transfer_expired",
  "conversation.transfer_cancelled",
]);

/**
 * Адресная ли это строка И стоит ли она окна. Оба вопроса вместе, потому что
 * порознь каждый неверен: адресная строка бывает шумной, а вид из списка —
 * разосланным по роли (тогда он про чужую работу).
 */
function личноеДляОкна(record: NotificationDto): boolean {
  return record.audience === null && ЛИЧНЫЕ_В_ОКНО.has(record.kind);
}

/** Строка центра из кадра WS; null — кадр без id (служебный тост). */
export function notificationFromWsEvent(data: NotifyEventData, ts: string): NotificationDto | null {
  if (!data.id) return null;
  return {
    id: data.id,
    kind: data.kind ?? "notify",
    severity: data.severity ?? severityFromLevel(data.level),
    title: data.title,
    body: data.text || null,
    entity: data.entity ?? null,
    action: data.action ?? null,
    repeat_count: data.repeat_count ?? 1,
    is_read: false,
    audience: data.audience_hint ?? null,
    created_at: data.created_at ?? ts,
    last_seen_at: ts,
  };
}

export function applyNotifyEvent(data: NotifyEventData, ts: string): void {
  const record = notificationFromWsEvent(data, ts);
  const severity = record?.severity ?? severityFromLevel(data.level);

  if (record) {
    const permissions = useSessionStore.getState().permissions;
    // Вторая линия фильтра получателей: хаб уже адресует кадр, но рассылка
    // по роли не должна всплыть у роли, которой она не адресована.
    if (!audienceAllowed(permissions, data.audience_hint)) return;

    useNotificationStore.getState().push(record);
    // Открытый журнал `/notifications` должен показать строку сразу.
    void queryClient.invalidateQueries({ queryKey: qk.notifications.listRoot, refetchType: "active" });

    if (severity === "critical") {
      // Критичное не прячется в тост Mantine — его показывает плашка сверху
      // экрана. Звук переиспользуем тот же, что у передачи диалога (03 §3.5).
      playAlertBeep();
      // То же событие уходит окном платформы: нативным в десктопе (14 §4),
      // браузерным в вебе. Без моста вызов молча выходит.
      toastForNotification(record);
      return;
    }

    /*
     * Личное — тоже в окно, хотя важность не критичная. Звук здесь НЕ играем:
     * критичное будит потому, что работа встала; личное напоминание — потому,
     * что оно про тебя, и этого хватает. Тост Mantine ниже остаётся: человек,
     * который смотрит в эту вкладку, окна не увидит вовсе.
     */
    if (личноеДляОкна(record)) toastForNotification(record);
  }

  // О предложении передачи уже сказал тост кадра `conversation:assigned` —
  // второй всплывал бы тут же о том же самом.
  if (record?.kind === "conversation.assigned") return;

  showToast({
    title: data.title,
    message: data.text,
    tone: TONE_BY_SEVERITY[severity],
    autoClose: severity === "critical" ? false : 6000,
    news: true,
  });
}
