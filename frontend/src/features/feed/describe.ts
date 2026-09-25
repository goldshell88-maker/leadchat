import { CLIENT_FALLBACK } from "@/features/templates/vars";
import type { MessageDto } from "@/shared/api/types";
import { statusLabel } from "@/shared/lib/conversationStatus";
import { plural } from "@/shared/lib/plural";
import type { WsInboxEvent } from "@/shared/realtime/applyWsEvent";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";
import {
  conversationFacts,
  personName,
  rememberConversation,
  rememberPatch,
  rememberPerson,
} from "./directory";

/**
 * КАДР WEBSOCKET → ОДНА РУССКАЯ СТРОКА ЖИВОЙ ЛЕНТЫ.
 *
 * ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ ОТДЕЛЬНО. Владелец просил «смотреть логи работы в
 * реальном времени», и первым побуждением было показать сами кадры — они уже
 * едут, их достаточно вывести. Так делать нельзя: `{"type":"message:status",
 * "data":{"conversation_id":"8f3c…","delivery_status":"failed"}}` читает
 * разработчик, а смотреть на это будет диспетчер и сам владелец. Строка
 * «Наталья: ответ не доставлен — Авито вернул 429» несёт ровно то же и не
 * требует знать, как устроен транспорт.
 *
 * ФОРМА СТРОКИ: «КТО: ЧТО СЛУЧИЛОСЬ». Подлежащее — клиент, потому что диалог
 * человек узнаёт по клиенту, а не по идентификатору и не по сотруднику.
 * Двоеточие выбрано вместо связного предложения из-за русских падежей: «ответ
 * Наталье не доставлен» требует дательного, «диалог взял Пётр у Натальи» —
 * родительного, а имена приходят из Авито как есть и склонять их нечем.
 * Через двоеточие имя всегда стоит в именительном и всегда верно.
 *
 * ЧЕГО ЗДЕСЬ НЕТ НАМЕРЕННО:
 *  - `control:*` — эти кадры наружу не уходят вовсе (`Hub.dispatch`), и ждать
 *    их здесь значило бы обещать то, чего не будет;
 *  - системные записи НАШЕГО же производства (`sender_type: "system"`) —
 *    «Статус: Новый → В работе. Иванов» приезжает и сообщением, и кадром
 *    `conversation:updated`, то есть строка в ленте была бы двойной;
 *  - `typing` — «печатает…» живёт полторы секунды и в журнале означает шум;
 *  - `notify` — у него есть свой экран (центр уведомлений, 14 §3), и второй
 *    список тех же строк рядом расходился бы с ним прочитанностью.
 *
 * ОТБОР ПО ПРАВАМ ЗДЕСЬ НЕ ДЕЛАЕТСЯ, И ЭТО ГЛАВНОЕ СВОЙСТВО ВСЕЙ ЗАТЕИ. Лента
 * питается кадрами, которые сокет УЖЕ принёс этому человеку, а хаб раздаёт их
 * по правам и по каналам: очередь сужена `eligible_operator_ids` (7.2),
 * заметки не уходят ролям без `notes:read`, `account:needs_reauth` — только
 * администратору, персональные кадры — только своим вкладкам. Повторять эти
 * правила здесь было бы второй копией матрицы прав, которая однажды разойдётся
 * с первой; не повторять — значит быть правым по построению.
 */

export type FeedGroup = "messages" | "delivery" | "queue" | "dialogs" | "channels" | "people";

/** Тон строки — им же красится метка слева. */
export type FeedTone = "neutral" | "good" | "warn" | "bad";

export interface FeedEntry {
  /** Порядковый номер в этой вкладке: ключ строки и точка «где я остановился». */
  seq: number;
  /** ISO времени события — берётся из кадра (`ts`), а не из момента отрисовки. */
  at: string;
  group: FeedGroup;
  tone: FeedTone;
  /**
   * ИМЯ КЛИЕНТА ХРАНИТСЯ ОТДЕЛЬНО ОТ ТЕКСТА, И ЭТО НЕ КРАСОТА.
   *
   * НАЙДЕНО НА СТЕНДЕ 12 августа. Кадр `message:new` несёт ДЕЛЬТУ диалога
   * (01 §11.3) — непрочитанные, время, статус, — и ни имени, ни канала в нём
   * нет намеренно: это самый частый кадр в системе. У диалога, который только
   * что завёлся, справочник ещё пуст, и строка запекалась навсегда безымянной:
   * «Клиент: новое сообщение» — прямо над строкой очереди, где та же Ольга
   * названа и по имени, и по каналу. Выглядит поломкой, хотя всё работает.
   *
   * Имя приезжает следующим кадром (`inbox:new`) через миллисекунды. Поэтому
   * `text` держит только сказуемое, а подлежащее подставляется В МОМЕНТ
   * ОТРИСОВКИ: лента перерисовывается на каждом событии, и строка сама
   * подхватывает имя, как только оно стало известно. `null` — событие не про
   * клиента (канал, сотрудник, обрыв записи).
   */
  who: string | null;
  /** Одна строка по-русски — БЕЗ имени клиента, оно живёт в `who`. */
  text: string;
  accountId: string | null;
  accountTitle: string | null;
  /** Диалог, если событие про диалог: по нажатию на строку туда и уходим. */
  conversationId: string | null;
  /**
   * Строка о ПРОПУСКЕ в записи (обрыв связи), а не о событии.
   *
   * Отдельный признак, потому что такие строки не подчиняются отбору: см.
   * `matchesFilters` в store.ts.
   */
  gap?: boolean;
}

/** То, что умеет описать один кадр. `seq` проставляет хранилище. */
export type FeedDraft = Omit<FeedEntry, "seq">;

export const GROUP_LABELS: Record<FeedGroup, string> = {
  messages: "Сообщения",
  delivery: "Доставка ответов",
  queue: "Очередь обращений",
  dialogs: "Диалоги",
  channels: "Каналы и связь",
  people: "Сотрудники",
};

/** Пункты отбора «Что показывать ▾» — собираются из словаря, а не выписываются рядом. */
export const GROUP_OPTIONS: Array<{ value: FeedGroup; label: string }> = (
  Object.keys(GROUP_LABELS) as FeedGroup[]
).map((value) => ({ value, label: GROUP_LABELS[value] }));

/**
 * «12 с», «7 мин», «1 ч 20 мин».
 *
 * Свой форматтер, а не `shared/lib/waiting`: тот считает от метки времени до
 * «сейчас» и до минуты вообще молчит (свежее обращение — норма, счётчик был бы
 * шумом). Здесь величина другая — законченный отрезок, который уже случился, и
 * «взял через 12 секунд» это лучшая новость смены, которую нельзя округлять до
 * нуля.
 */
export function humanSeconds(total: number): string {
  const s = Math.max(0, Math.round(total));
  if (s < 60) return `${s} ${plural(s, "секунду", "секунды", "секунд")}`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} ${plural(m, "минуту", "минуты", "минут")}`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  const hours = `${h} ${plural(h, "час", "часа", "часов")}`;
  return rest === 0 ? hours : `${hours} ${rest} ${plural(rest, "минуту", "минуты", "минут")}`;
}

/** Кто написал/ответил, когда сервер прислал автора. */
function senderName(msg: MessageDto): string | null {
  return msg.sender?.full_name ?? null;
}

/** Строка про сообщение: кто его отправил и в какую сторону. */
function describeMessage(msg: MessageDto): { text: string; tone: FeedTone } | null {
  if (msg.direction === "in") return { text: "новое сообщение", tone: "neutral" };

  if (msg.direction === "note") {
    const who = senderName(msg);
    return {
      text: who ? `заметку оставил ${who}` : "добавлена заметка",
      tone: "neutral",
    };
  }

  if (msg.direction === "system") {
    // Наша собственная запись («Статус: Новый → В работе. Иванов») приезжает
    // ещё и кадром `conversation:updated` — показать обе значило бы удвоить
    // ленту на каждом действии. Показываем только чужие слова — от площадки.
    if (msg.sender_type !== "avito") return null;
    return { text: "служебное сообщение от Авито", tone: "neutral" };
  }

  // direction === "out"
  // Единственный писатель `('out','system')` — workers/address_ask.py (вопрос об
  // адресе). Не «ответ отправлен» с тоном good: клиенту никто не ответил.
  if (msg.sender_type === "system") return { text: "система спросила адрес", tone: "neutral" };
  if (msg.sender_type === "bot") return { text: "ответил бот", tone: "good" };
  const who = senderName(msg);
  return {
    text: who ? `ответил ${who}` : "ответ отправлен",
    tone: "good",
  };
}

/**
 * Готовая строка: подлежащее + сказуемое + канал.
 *
 * ИМЯ И КАНАЛ ПОДСТАВЛЯЮТСЯ ЗДЕСЬ, А НЕ ПРИ ЗАХВАТЕ КАДРА. Причина — в шапке
 * поля `who`: `message:new` приходит первым и дельту диалога несёт без имени,
 * поэтому у только что заведённого диалога строка запекалась безымянной
 * навсегда. Спрашиваем справочник на каждой отрисовке — лента перерисовывается
 * на каждом событии, и пропуск закрывается сам через миллисекунды.
 *
 * ЖИВЁТ РЯДОМ СО СЛОВАМИ, А НЕ В ЭКРАНЕ. Экран рисует то, что вернули отсюда,
 * и проверки читают отсюда же. Разложи склейку по двум местам — и однажды на
 * экране будет одно, а в проверке другое.
 */
export function feedLine(entry: FeedEntry | FeedDraft): {
  who: string | null;
  channel: string | null;
  line: string;
} {
  const known = entry.conversationId ? conversationFacts(entry.conversationId) : null;
  const who = entry.who ?? known?.clientName ?? null;
  const channel = entry.accountTitle ?? known?.accountTitle ?? null;
  /*
   * «ПРО КЛИЕНТА ЛИ ЭТО СОБЫТИЕ» РЕШАЕТ НАЛИЧИЕ ДИАЛОГА, А НЕ ПУСТОТА ИМЕНИ.
   * `who === null` бывает по двум разным причинам: событие вообще не про
   * клиента (канал отвалился, сотрудник отошёл, обрыв записи) ЛИБО клиент
   * есть, а имени мы не знаем — вебхук Авито его не несёт, и это норма.
   * Судили бы по имени — у безымянного клиента осталось бы сказуемое без
   * подлежащего: «новое сообщение», обрубок.
   */
  const line = entry.conversationId ? `${who ?? CLIENT_FALLBACK}: ${entry.text}` : entry.text;
  return { who, channel, line };
}

/**
 * Описать кадр. `null` — кадр в ленту не идёт (см. шапку файла: шум и дубли).
 *
 * ЗАОДНО НАПОЛНЯЕТ СПРАВОЧНИК ИМЁН, и это не побочный эффект «за компанию»:
 * порядок здесь существенный. Строка о смене статуса — это РАЗНИЦА между
 * прежним значением и новым, а значит прежнее надо прочитать ДО того, как
 * кадр его перезапишет. Разнеси чтение и запись по разным местам — и они
 * однажды поменяются местами.
 */
/** Исходы предложения передачи — строкой ленты (кадр `conversation:updated`). */
const TRANSFER_OUTCOME: Record<
  "accepted" | "declined" | "cancelled" | "expired",
  { tone: FeedTone; text: (actor: string) => string }
> = {
  accepted: { tone: "good", text: (actor) => `${actor} принял(а) переданный диалог` },
  declined: {
    tone: "warn",
    text: (actor) => `${actor} отказался(ась) от передачи — диалог остаётся у прежнего`,
  },
  cancelled: { tone: "neutral", text: (actor) => `${actor} отменил(а) передачу` },
  expired: {
    tone: "warn",
    text: () => "передачу никто не принял за 15 минут — диалог остался у прежнего",
  },
};

export function describeFrame(e: WsServerEvent | WsInboxEvent): FeedDraft | null {
  switch (e.type) {
    case "message:new": {
      const convId = e.data.conversation_id;
      rememberPatch(convId, e.data.conversation_patch);
      rememberPerson(e.data.message.sender);
      const facts = conversationFacts(convId);
      const line = describeMessage(e.data.message);
      if (!line) return null;
      return {
        who: facts.clientName,
        at: e.ts,
        group: "messages",
        tone: line.tone,
        text: line.text,
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    case "message:status": {
      const convId = e.data.conversation_id;
      rememberPatch(convId, e.data.conversation_patch);
      const facts = conversationFacts(convId);
            const failed = e.data.delivery_status === "failed";
      // Причину Авито называет не всегда. Молчание про причину — это не
      // «доставлено», и подменять его бодрым многоточием нельзя: человек
      // должен понимать, что идти за подробностями некуда.
      const reason = e.data.error?.trim();
      if (e.data.delivery_status === "dismissed") {
        // `dismissed` приезжает ДВУМЯ путями, а `sender_type` кадр не несёт:
        //  - воркер доставки (`deliver._fail`) — вопрос системы об адресе не
        //    дошёл до клиента (`services/messages.py::undelivered_status`);
        //    причина в `error` есть всегда, все девять вызовов `_fail` её несут;
        //  - ручка «Снять» оператора (`POST /messages/{id}/dismiss`) — человек
        //    снял с учёта своё неотправленное; `error` не передаётся.
        // Ни то, ни другое не «ответ доставлен» с тоном good.
        return {
          who: facts.clientName,
          at: e.ts,
          group: "delivery",
          tone: "neutral",
          text: reason ? `вопрос системы не доставлен — ${reason}` : "неотправленное снято с учёта",
          accountId: facts.accountId,
          accountTitle: facts.accountTitle,
          conversationId: convId,
        };
      }
      return {
        who: facts.clientName,
        at: e.ts,
        group: "delivery",
        tone: failed ? "bad" : "good",
        text: failed
          ? `ответ не доставлен — ${reason || "причину Авито не назвал"}`
          : "ответ доставлен",
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    case "conversation:updated": {
      const convId = e.data.conversation_id;
      const before = conversationFacts(convId).status;
      rememberPatch(convId, e.data.patch);
      if (e.data.transfer_outcome) {
        rememberPerson(e.data.transfer_actor);
        const facts = conversationFacts(convId);
        const outcome = TRANSFER_OUTCOME[e.data.transfer_outcome];
        return {
          who: facts.clientName,
          at: e.ts,
          group: "dialogs",
          tone: outcome.tone,
          text: outcome.text(e.data.transfer_actor?.full_name ?? "Сотрудник"),
          accountId: facts.accountId,
          accountTitle: facts.accountTitle,
          conversationId: convId,
        };
      }
      const next = e.data.patch.status;
      // Кадр везёт статус на каждое действие — от заметки до закрепления.
      // В ленту идёт только настоящая перемена (см. `status` в directory.ts).
      if (!next || next === before) return null;
      const facts = conversationFacts(convId);
      return {
        who: facts.clientName,
        at: e.ts,
        group: "dialogs",
        tone: next === "closed" ? "good" : "neutral",
        text: `диалог — «${statusLabel(next)}»`,
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    case "conversation:assigned": {
      const convId = e.data.conversation_id;
      rememberPerson(e.data.assignee);
      rememberPerson(e.data.assigned_by);
      const facts = conversationFacts(convId);
      const why = e.data.comment?.trim();
      // ⚠ ПРЕДЛОЖЕНИЕ — ЕЩЁ НЕ ПЕРЕДАЧА (проверка 24.09). Диалог остаётся у
      // прежнего, пока получатель не примет; «теперь ведёт» здесь врало, и
      // руководитель считал клиента чужим, хотя его вёл прежний сотрудник.
      return {
        who: facts.clientName,
        at: e.ts,
        group: "dialogs",
        tone: e.data.offer ? "warn" : "neutral",
        text:
          (e.data.offer
            ? `${e.data.assigned_by.full_name} предлагает диалог ${e.data.assignee.full_name} — ждёт ответа`
            : `${e.data.assigned_by.full_name} передал диалог — теперь ведёт ${e.data.assignee.full_name}`) +
          (why ? ` («${why}»)` : ""),
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    case "conversation:viewers": {
      const convId = e.data.conversation_id;
      for (const v of e.data.viewers) rememberPerson(v);
      // Один зритель — это тот, кто диалог и открыл; новостью такое не
      // является. Лента говорит о том, ради чего кадр вообще придуман: двое в
      // одном диалоге, и клиент вот-вот получит два ответа (SCEN-48).
      if (e.data.viewers.length < 2) return null;
      const facts = conversationFacts(convId);
      return {
        who: facts.clientName,
        at: e.ts,
        group: "people",
        tone: "warn",
        text: `диалог открыт сразу у нескольких — ${e.data.viewers
          .map((v) => v.full_name)
          .join(", ")}`,
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    case "presence:online": {
      const name = personName(e.data.user_id);
      // Имени не знаем — строки не будет. «Сотрудник вышел на связь» без
      // сотрудника не сообщает ничего и занимает место настоящего события.
      if (!name) return null;
      const what =
        e.data.status === "online" ? "на месте" : e.data.status === "away" ? "отошёл" : "не в сети";
      return {
        who: null,
        at: e.ts,
        group: "people",
        tone: e.data.status === "online" ? "good" : "neutral",
        text: `${name} — ${what}`,
        accountId: null,
        accountTitle: null,
        conversationId: null,
      };
    }

    case "account:needs_reauth": {
      return {
        who: null,
        at: e.ts,
        group: "channels",
        tone: "bad",
        text: `Канал «${e.data.title}» требует переподключения: ответы не уходят, обращения приходят`,
        accountId: e.data.account_id,
        accountTitle: e.data.title,
        conversationId: null,
      };
    }

    case "inbox:new": {
      rememberConversation(e.data.conversation);
      const facts = conversationFacts(e.data.conversation_id);
      return {
        who: facts.clientName,
        at: e.ts,
        group: "queue",
        tone: "warn",
        text: `обращение встало в очередь — ждёт, кто возьмёт`,
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: e.data.conversation_id,
      };
    }

    case "inbox:claimed": {
      const convId = e.data.conversation_id;
      rememberPerson(e.data.claimed_by);
      rememberPatch(convId, e.data.conversation_patch);
      const facts = conversationFacts(convId);
      const waited =
        typeof e.data.waited_seconds === "number" && e.data.waited_seconds >= 0
          ? ` (ждал ${humanSeconds(e.data.waited_seconds)})`
          : "";
      return {
        who: facts.clientName,
        at: e.ts,
        group: "queue",
        tone: "good",
        // Без принявшего кадр тоже приходит: так `inbound` сообщает, что
        // коллега ответил клиенту прямо из приложения Авито (аудит 30.08).
        // Живая лента обязана назвать это своими словами, а не именем, которого
        // в кадре нет.
        text: e.data.claimed_by
          ? `диалог взял ${e.data.claimed_by.full_name}${waited}`
          : "диалог ушёл из очереди — ответили из Авито",
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    case "inbox:released": {
      rememberConversation(e.data.conversation);
      rememberPerson(e.data.released_by);
      rememberPatch(e.data.conversation_id, e.data.conversation_patch);
      const facts = conversationFacts(e.data.conversation_id);
      return {
        who: facts.clientName,
        at: e.ts,
        group: "queue",
        tone: "warn",
        text: `${e.data.released_by.full_name} вернул диалог в очередь`,
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: e.data.conversation_id,
      };
    }

    case "inbox:declined": {
      const convId = e.data.conversation_id;
      rememberPerson(e.data.declined_by);
      const facts = conversationFacts(convId);
      const why = e.data.reason?.trim();
      const escalated = e.data.escalated === true;
      return {
        who: facts.clientName,
        at: e.ts,
        group: "queue",
        // «Отказались все» — это уже не отказ одного, а обращение, за которое
        // никто не взялся: тон другой, потому что и действие другое.
        tone: escalated ? "bad" : "warn",
        text:
          `${e.data.declined_by.full_name} отказался` +
          (why ? ` («${why}»)` : "") +
          (escalated ? " — отказались все, обращение осталось без хозяина" : ""),
        accountId: facts.accountId,
        accountTitle: facts.accountTitle,
        conversationId: convId,
      };
    }

    default:
      // pong / typing / notify и всё, чего мы ещё не знаем: неизвестный тип
      // молча игнорируется (01 §11.2, forward-совместимость).
      return null;
  }
}

/**
 * Строка о ПРОПУСКЕ: связь пропадала, и что происходило — лента не знает.
 *
 * Отдельная функция, а не кадр, потому что события такого не бывает: пропуск
 * — это отсутствие событий, и заметить его может только клиент, у которого
 * рвётся сокет. Молчать о нём нельзя: лента без этой строки выглядит ровно
 * так же, как лента спокойной ночи, и человек делает вывод «было тихо» из
 * того, что мы просто не слышали.
 */
export function describeGap(from: number, to: number): FeedDraft {
  const seconds = Math.max(1, Math.round((to - from) / 1000));
  return {
    who: null,
    at: new Date(to).toISOString(),
    group: "channels",
    tone: "warn",
    text: `Связь с сервером пропадала на ${humanSeconds(seconds)} — что было в это время, лента не знает`,
    accountId: null,
    accountTitle: null,
    conversationId: null,
    gap: true,
  };
}
