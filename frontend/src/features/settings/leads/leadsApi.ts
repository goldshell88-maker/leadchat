import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http } from "@/shared/api/http";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";
import { qk } from "@/shared/api/queryKeys";

/**
 * Автозаявки: расширение владельца забирает лиды и заводит заявки в лид-центрах.
 *
 * ⚠ ТОКЕНА В ЭТИХ ТИПАХ НЕТ, кроме одного места — ответа на выпуск. Сервер
 * хранит его зашифрованным и обратно не отдаёт: состояние говорит только
 * «задан». Увидеть значение можно ровно один раз, сразу после нажатия.
 */

export interface LeadsState {
  configured: boolean;
  /** Сводка: `total`, `unacked` и по одному ключу на каждый исход. */
  stats: Record<string, number>;
}

/**
 * Диалог, который заявкой стать не может, и почему.
 *
 * ⚠ ЭТИХ СТРОК НА ЭКРАНЕ НЕ БЫЛО ВОВСЕ (28.08), хотя подпись выше по странице
 * обещает: «Если не сработали оба пути, заявка придерживается — и это видно в
 * журнале ниже, а не пропадает молча». Придержанный лид не создаёт строки
 * выдачи, и в журнале его быть не могло; сервер писал их только в свой лог.
 * По замеру владельца из шести диалогов с итогом «Выезд» телефон есть у двух —
 * то есть две трети заявок висели невидимыми.
 */
export interface HeldLead {
  conversation_id: string;
  client_name: string | null;
  account_title: string;
  reason: string;
}

export interface LeadHandout {
  conversation_id: string;
  handed_at: string;
  src_key: string;
  src_label: string;
  acked: boolean;
  decision: string | null;
  decision_label: string | null;
  /** Заявка в лид-центре ЕСТЬ. «Отдали» и «создана» — разные вещи. */
  created: boolean;
  request_id: string | null;
  message: string | null;
}

const KEY = ["settings", "leads"] as const;
const HANDOUTS = ["settings", "leads", "handouts"] as const;

export function useLeadsState() {
  return useQuery({
    queryKey: KEY,
    queryFn: () => http.get<LeadsState>("/settings/leads"),
  });
}

export function useHandouts() {
  return useQuery({
    queryKey: HANDOUTS,
    queryFn: () =>
      http.get<{ items: LeadHandout[]; held_back: HeldLead[] }>(
        "/settings/leads/handouts?limit=100",
      ),
    // Расширение опрашивает по расписанию, и журнал обязан обновляться сам:
    // человек открыл экран посмотреть, «доехало ли», и ждёт ответа, а не F5.
    refetchInterval: 30_000,
  });
}

/**
 * ⚠ ВЫЗОВОВ НЕТ НАМЕРЕННО, И ЭТО НЕ ЗАБЫТЫЙ КОД. Кнопку выпуска убрал владелец
 * 16.08: заявки бот-диалогов уже создаются через очередь лид-бота и расширение
 * «Автозаявки», и второй путь отсюда создавал бы КАЖДУЮ заявку в CRM дважды.
 * Разбор — в `LeadsTab.tsx` у блока «Подключение расширения»; на сервере стоит
 * предохранитель `leads.SECOND_PATH_FUSE` (POST → 409).
 *
 * Хук оставлен на случай, когда поток осознанно переведут на LeadChat. Пометка
 * здесь затем, чтобы обход неиспользуемого кода не разбирал это заново: 27.08
 * такой обход дал три настоящие находки и одну ложную тревогу — вот эту.
 */
export function useIssueToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => http.post<{ token: string }>("/settings/leads/token", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

export function useRevokeToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => http.del<{ configured: boolean }>("/settings/leads/token"),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    // Отказ молчал (проверка 24.09): человек считал старый токен отозванным.
    onError: (error) => showToast(describeError({ where: "Отзыв токена Автозаявок", error })),
  });
}

/**
 * Поля заявки у канала: источник, номер партнёра, ссылка на отзыв.
 *
 * ⚠ КОЛОНКИ БЫЛИ С МИГРАЦИИ 0040, А ЗАПОЛНИТЬ ИХ БЫЛО НЕЧЕМ. Заявка их читает
 * (`app/services/leads.py`), но ни ручка правки, ни экран о них не знали — оставалась
 * правка в базе руками. Владелец 14 августа: «я не могу указать источник, который
 * будет использовать при автосоздании заявки, и ссылку на отзыв».
 *
 * Отдельная мутация, а не одна с лид-центром: смена центра МЕНЯЕТ АДРЕСАТА заявок и
 * пишется отдельной строкой аудита, а это просто поля.
 */
export function useSetChannelLeadFields() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...поля
    }: {
      id: string;
      lead_origin?: string;
      lead_partner_number?: string;
      review_url?: string;
    }) => http.patch(`/avito-accounts/${id}`, поля),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.accounts });
      qc.invalidateQueries({ queryKey: KEY });
    },
  });
}

/** Выбор лид-центра у канала. Пустая строка — снять выбор. */
export function useSetChannelSrc() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, srcKey }: { id: string; srcKey: string }) =>
      http.patch(`/avito-accounts/${id}`, { lead_src_key: srcKey }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.accounts });
      qc.invalidateQueries({ queryKey: KEY });
    },
  });
}
