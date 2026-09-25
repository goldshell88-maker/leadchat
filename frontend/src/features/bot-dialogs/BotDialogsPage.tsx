import { useState } from "react";
import { Button, Loader, Select, Text } from "@mantine/core";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAvitoAccountsQuery } from "@/shared/api/reference";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { подписьКанала } from "@/shared/lib/channelLabel";
import { fetchBotDialogs, fetchBotDialogsLive, type BotDialogRow, type BotLiveRow } from "./api";
import "./bot-dialogs.css";
import { выбралСам } from "@/features/chats/выборДиалога";

/**
 * НАДЗОР ЗА БОТОМ (docs/45) — где бот сработал плохо.
 *
 * ЗАЧЕМ. Владелец ловил бота на плохих решениях поштучно, открывая диалоги
 * руками: 26 августа бот верно отказал по битой матрице — и через пять минут
 * сам же дожал «Ну что, расскажете, что случилось?». Находилось это случайно.
 * «Разбор диалогов» на вопрос «где бот навредил» не отвечает: там всё
 * вперемешку и нет колонки об исходе.
 *
 * ЭКРАН ТОЛЬКО СМОТРИТ. Ни выключить бота, ни переписать ответ отсюда нельзя —
 * это делается в настройках и в регламенте. Смешение просмотра и управления на
 * одном экране — ровно то, из-за чего открытие диалога однажды стало его
 * назначением.
 */

/** Период по умолчанию — тот же, что у «Разбора диалогов»: последние 30 дней. */
const ДНЕЙ_ПО_УМОЛЧАНИЮ = 30;

const ПЕРИОДЫ = [
  { value: "7", label: "7 дней" },
  { value: "30", label: "30 дней" },
  { value: "90", label: "90 дней" },
];

function периодОт(дней: number): string {
  const d = new Date();
  d.setDate(d.getDate() - дней);
  return d.toISOString().slice(0, 10);
}

function часыМинуты(sec: number | null): string {
  if (sec === null) return "—";
  const м = Math.round(sec / 60);
  if (м < 60) return `${м} м`;
  return `${Math.floor(м / 60)} ч ${м % 60} м`;
}

function Строка({ row, onOpen }: { row: BotDialogRow; onOpen: (id: string) => void }) {
  /*
   * Тёплый цвет ровно в одном месте — «не взял». Отказ бота сам по себе может
   * быть верным; отказ, после которого диалог никто не открыл, — потерянный
   * клиент. Красного на экране нет вовсе: он занят настоящими тревогами
   * (счётчик непрочитанных, критичные уведомления), и покрасить им ещё и это
   * значит обесценить оба.
   */
  const взяли = Boolean(row.assignee_name);
  return (
    <tr className="bot-row" onClick={() => onOpen(row.id)} tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(row.id);
        }
      }}
      role="link"
      aria-label={`Диалог: ${row.client_name ?? "клиент"}`}
    >
      <td>
        <div className="bot-row__name">{row.client_name ?? "Клиент"}</div>
        <div className="bot-row__hint">{row.account_title ?? "—"}</div>
      </td>
      <td>
        <span className="bot-row__outcome" title={row.outcome.label}>
          {row.outcome.label}
        </span>
      </td>
      <td className={взяли ? undefined : "bot-row__nobody"}>
        {взяли ? row.assignee_name : "не взял"}
      </td>
      <td className="bot-row__num">{row.messages_count}</td>
      <td className="bot-row__num">{часыМинуты(row.first_response_sec)}</td>
    </tr>
  );
}

/** Сколько минут идёт — по `since`, тем же полем, что и «Уже 22 мин» в карточке. */
function идёт(since: string | null): string {
  if (!since) return "—";
  const мин = Math.max(0, Math.round((Date.now() - new Date(since).getTime()) / 60000));
  if (мин < 60) return `${мин} мин`;
  return `${Math.floor(мин / 60)} ч ${мин % 60} мин`;
}

/**
 * ПОЛОСА «СЕЙЧАС» — кто у бота в руках сию минуту.
 *
 * ⚠ ОБНОВЛЯЕТСЯ ТОЧНЫМ ЗАПРОСОМ РАЗ В ДЕСЯТЬ СЕКУНД, А НЕ АРИФМЕТИКОЙ ПО
 * ЖИВЫМ КАДРАМ. Счётчик «Входящих» в приложении двигается по кадрам на ±1 —
 * но там иначе нельзя: очередь у каждого своя, и общего числа сервер не знает.
 * Здесь число общее, а `bot_active` покрыт индексом: точный ответ стоит копейки.
 * Пропущенный кадр в схеме «±1» уводит счётчик тихо, и заметить это нечем.
 */
/*
 * ⚠ ИМЯ ЛАТИНСКОЕ, И ЭТО НЕ ПРИХОТЬ. Правило `react-hooks/rules-of-hooks`
 * узнаёт компонент по ЗАГЛАВНОЙ ЛАТИНСКОЙ букве; кириллическая «С» для него
 * строчная, и вызов хука внутри читается как хук вне компонента. Соседний
 * `Строка` по-русски проходит только потому, что хуков не зовёт.
 */
function LiveNow({ onOpen }: { onOpen: (id: string) => void }) {
  const живое = useQuery({
    queryKey: ["bot-dialogs", "live"],
    queryFn: fetchBotDialogsLive,
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
  });

  const данные = живое.data;
  if (живое.isError && !данные) {
    // Полоса не исчезает молча (проверка 24.09): «прямо сейчас никого» и «не
    // знаем» — разные ответы.
    return (
      <section className="bot-live" aria-label="Диалоги бота прямо сейчас">
        <div className="bot-live__idle" role="alert">
          Не получилось узнать, кого бот ведёт сейчас — повторим через 10 секунд
        </div>
      </section>
    );
  }
  if (!данные) return null;

  return (
    <section className="bot-live" aria-label="Диалоги бота прямо сейчас">
      <div className="bot-live__head">
        <span className="bot-live__title">Сейчас у бота</span>
        <span className="bot-live__count">{данные.count}</span>
        <span className="bot-live__hint">обновляется само</span>
      </div>
      {данные.items.length === 0 ? (
        <div className="bot-live__idle">Прямо сейчас бот никого не ведёт</div>
      ) : (
        <ul className="bot-live__list">
          {данные.items.map((r: BotLiveRow) => (
            <li key={r.id}>
              <button type="button" className="bot-live__row" onClick={() => onOpen(r.id)}>
                {/*
                  `title` — потому что имя теперь обрезается многоточием
                  (bot-dialogs.css, замер 08.09: на телефоне колонке достаётся
                  17px при 86 нужных). Обрезка видима, но текст обязан
                  оставаться доступным — так же сделано у «что сказал» ниже.
                */}
                <span className="bot-live__who" title={r.client_name ?? "Клиент"}>
                  {r.client_name ?? "Клиент"}
                </span>
                <span className="bot-live__meta">{r.account_title ?? "—"}</span>
                <span className="bot-live__meta">{идёт(r.since)}</span>
                <span className="bot-live__meta">{r.bot_replies} реплик</span>
                <span className="bot-live__said" title={r.last_bot_text ?? ""}>
                  {r.last_bot_text ? `«${r.last_bot_text}»` : "—"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function BotDialogsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [дней, setДней] = useState(String(ДНЕЙ_ПО_УМОЛЧАНИЮ));

  const outcome = params.get("outcome") ?? undefined;
  const accountId = params.get("account_id") ?? undefined;
  // Страница — в адресе, как у «Разбора диалогов»: вернувшись из переписки,
  // человек попадает на ту же страницу, а не на первую.
  const offset = Math.max(0, Number(params.get("offset")) || 0);

  const accounts = useAvitoAccountsQuery();
  const запрос = useQuery({
    queryKey: ["bot-dialogs", { outcome, accountId, дней, offset }],
    queryFn: () =>
      fetchBotDialogs({ outcome, accountId, dateFrom: периодОт(Number(дней)), offset }),
    // Листание не мигает пустотой: прежняя страница стоит, пока едет новая.
    placeholderData: keepPreviousData,
  });

  /** Правка отбора — всегда с первой страницы: прежний сдвиг к новому отбору не относится. */
  const отобрать = (правка: (p: URLSearchParams) => void) => {
    const p = new URLSearchParams(params);
    правка(p);
    p.delete("offset");
    setParams(p, { replace: true });
  };

  const выбрать = (группа: string | null) =>
    отобрать((p) => {
      if (группа === null || группа === outcome) p.delete("outcome");
      else p.set("outcome", группа);
    });

  const листать = (к: number) => {
    const p = new URLSearchParams(params);
    if (к <= 0) p.delete("offset");
    else p.set("offset", String(к));
    setParams(p, { replace: true });
  };

  const данные = запрос.data;
  const плитки = данные?.counters ?? [];

  return (
    <div className="lc-page bot-dialogs">
      <PageHeader
        title="Диалоги бота"
        description="Что бот вёл сам и чем это кончилось"
      />

      <LiveNow
        onOpen={(id) => {
          выбралСам(id);
          navigate(`/chats/${id}`);
        }}
      />

      <div className="bot-dialogs__filters">
        <Select
          className="lc-field"
          size="xs"
          w={130}
          aria-label="Период"
          data={ПЕРИОДЫ}
          value={дней}
          allowDeselect={false}
          onChange={(v) => {
            if (!v) return;
            setДней(v);
            отобрать(() => {});
          }}
        />
        <Select
          className="lc-field"
          size="xs"
          w={220}
          aria-label="Канал"
          placeholder="Канал: все"
          clearable
          data={(accounts.data?.items ?? []).map((a) => ({ value: a.id, label: подписьКанала(a) }))}
          value={accountId ?? null}
          onChange={(v) =>
            отобрать((p) => {
              if (v) p.set("account_id", v);
              else p.delete("account_id");
            })
          }
        />
      </div>

      {/*
        Порядок плиток задан сервером (`handoff.OUTCOME_ORDER`) и НЕ по величине
        счётчика: «Бот закрыл сам» стоит первой, даже когда её число меньше
        соседнего. Сортировка по числу подняла бы наверх самый частый сбой
        вместо самого дорогого.
      */}
      <div className="bot-tiles">
        {плитки.filter((п) => п.kind === "failure").map((п) => (
          <button
            key={п.group}
            type="button"
            className="bot-tile"
            data-active={п.group === outcome || undefined}
            aria-pressed={п.group === outcome}
            onClick={() => выбрать(п.group)}
          >
            <span className="bot-tile__label">{п.label}</span>
            <span className="bot-tile__count">{п.count}</span>
          </button>
        ))}
      </div>

      {/*
        «Не смог сам» стоит ОТДЕЛЬНОЙ строкой под рядом сбоев, а не плиткой в
        нём. Это не дефект, а потолок возможностей бота: он честно позвал
        человека вместо того, чтобы гадать. Самая частая причина передачи — и
        поставь её в общий ряд, экран сказал бы «210 сбоев» про правильную
        работу.
      */}
      {плитки
        .filter((п) => п.kind === "capacity")
        .map((п) => (
          <button
            key={п.group}
            type="button"
            className="bot-capacity"
            data-active={п.group === outcome || undefined}
            aria-pressed={п.group === outcome}
            onClick={() => выбрать(п.group)}
          >
            <span className="bot-capacity__label">{п.label}</span>
            <span className="bot-capacity__count">{п.count}</span>
            <span className="bot-capacity__hint">бот сработал верно, но не потянул</span>
          </button>
        ))}

      {запрос.isPending ? (
        <div className="bot-dialogs__state">
          <Loader size="sm" color="lp" />
        </div>
      ) : запрос.isError && !данные ? (
        /*
          Отказ сервера — словами и с «Повторить» (проверка 24.09): здесь
          рисовалась пустая таблица с заголовками, не отличимая ни от «данных
          нет», ни от загрузки.
        */
        <EmptyState
          illustration="error"
          live="alert"
          title="Не получилось загрузить диалоги бота"
          description={
            запрос.error instanceof ApiError && запрос.error.message
              ? запрос.error.message
              : "Нет связи с сервером — попробуйте ещё раз"
          }
          action={
            <Button variant="outline" size="xs" onClick={() => void запрос.refetch()}>
              Повторить
            </Button>
          }
        />
      ) : данные && данные.items.length === 0 ? (
        <EmptyState
          illustration="done"
          title={outcome ? "За период таких диалогов нет" : "Бот пока не работал"}
          description={
            outcome
              ? "Снимите отбор, чтобы увидеть остальные диалоги бота"
              : "Как только бот начнёт отвечать клиентам, его диалоги появятся здесь"
          }
        />
      ) : (
        <div className="bot-table">
          <table className="lc-table">
            <thead>
              <tr>
                <th>Клиент</th>
                <th>Чем кончилось</th>
                <th>Человек</th>
                {/*
                  ⚠ БЫЛО «РЕПЛИК» — ТЕМ ЖЕ СЛОВОМ, ЧТО И ПОЛОСА ВЫШЕ, ПРИ
                  ДРУГОМ ЧИСЛЕ (найдено разбором 08.09). В живой полосе «Сейчас
                  у бота» печатается `bot_replies` — реплики БОТА
                  (`bot_dialogs.py:_реплики_бота`, `sender_type = 'bot'`), а
                  здесь `messages_count` из общей таблицы диалогов — ВСЯ
                  переписка целиком, вместе с сообщениями клиента и оператора
                  (`conversation_table.py`, `direction in ('in','out')`).
                  Одно слово над двумя разными величинами на одном экране
                  читается как ошибка счёта.

                  Имя взято у соседнего экрана: то же поле в «Разборе диалогов»
                  называется «Сообщений» (TablePage.tsx). Две подписи для
                  одного поля разъехались бы при первой же правке.
                */}
                <th className="bot-row__num">Сообщений</th>
                <th className="bot-row__num">Первый ответ</th>
              </tr>
            </thead>
            <tbody>
              {(данные?.items ?? []).map((r) => (
                <Строка
                  key={r.id}
                  row={r}
                  onOpen={(id) => {
                    выбралСам(id);
                    navigate(`/chats/${id}`);
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/*
        ⚠ СПИСОК ОБРЕЗАЛСЯ МОЛЧА (проверка 24.09): сервер отдаёт 50 строк, а в
        бою диалогов бота 209 за 30 дней — остальные открыть было нельзя, и
        экран об этом не говорил. Теперь «X–Y из N» и листание.
      */}
      {данные && данные.page.total > данные.page.limit && (
        <div className="bot-dialogs__pager">
          <Button
            variant="default"
            size="xs"
            disabled={offset === 0}
            onClick={() => листать(offset - данные.page.limit)}
          >
            Назад
          </Button>
          <Text fz="xs" c="var(--lc-text-3)" className="lc-num">
            {offset + 1}–{Math.min(offset + данные.page.limit, данные.page.total)} из{" "}
            {данные.page.total}
          </Text>
          <Button
            variant="default"
            size="xs"
            disabled={offset + данные.page.limit >= данные.page.total}
            onClick={() => листать(offset + данные.page.limit)}
          >
            Дальше
          </Button>
        </div>
      )}

      <Text fz="xs" c="var(--lc-text-3)" mt="var(--lc-space-2)">
        Строка открывает переписку целиком — принимать диалог для этого не нужно
      </Text>
    </div>
  );
}
