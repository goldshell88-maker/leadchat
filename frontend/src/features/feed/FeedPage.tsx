import { useEffect, useMemo, useRef, type UIEvent } from "react";
import { Button, Select, Text } from "@mantine/core";
import { Link } from "react-router-dom";
import { plural } from "@/shared/lib/plural";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { feedLine, GROUP_OPTIONS, type FeedEntry, type FeedGroup } from "./describe";
import { FEED_LIMIT, matchesFilters, pendingCount, useFeedStore, visibleEntries } from "./store";
import { TraceSwitch } from "./TraceSwitch";
import "./feed.css";

/**
 * ЭКРАН «ЖИВАЯ ЛЕНТА» — что происходит в системе прямо сейчас, по-русски.
 *
 * КОМУ ВИДЕН. Тем же, кому видна статистика (`stats:all` — администратор и
 * руководитель); guard стоит в карте маршрутов рядом со «Статистикой» и
 * «Разбором диалогов». Оператору отдельный экран не нужен: всё, что лента ему
 * показала бы, у него и так перед глазами — очередь, его диалоги, его ответы.
 * Наблюдателю (`conversations:read` и больше ничего) он тоже не открывается,
 * и это осознанно: лента — управленческий взгляд, как и статистика.
 *
 * Дать ленту оператору было бы безопасно и без права: она собрана из кадров,
 * которые сокет ему УЖЕ принёс, а хаб раздаёт их по правам и по каналам. То
 * есть чужой очереди в ней не появится в принципе — не потому, что мы её
 * прячем, а потому что кадров с ней у вкладки нет.
 *
 * ПОЧЕМУ ЛЕНТА ИДЁТ СВЕРХУ ВНИЗ (свежее — вверху). Терминальный порядок
 * (свежее внизу) требует автопрокрутки, автопрокрутка воюет с чтением, и
 * дальше вся конструкция держится на угадывании «человек читает или смотрит».
 * Свежее сверху: не читаешь — смотришь на первую строку, читаешь — ставишь
 * паузу, и ничего под курсором не прыгает.
 */

/** «14:02:15» — местное время сотрудника. */
function clockWithSeconds(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  // Общий `formatClock` (10 §7.4) даёт минуты — их здесь мало: в час пик
  // десяток событий укладывается в одну минуту, и без секунд лента выглядит
  // как список с одинаковыми метками, по которому нельзя понять порядок.
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function FeedRow({ entry }: { entry: FeedEntry }) {
  const { channel, line } = feedLine(entry);
  const body = (
    <>
      <time className="lc-feed__time lc-num" dateTime={entry.at}>
        {clockWithSeconds(entry.at)}
      </time>
      <span className="lc-feed__text">{line}</span>
      {channel ? <span className="lc-feed__chan">{channel}</span> : null}
    </>
  );

  return (
    <li className="lc-feed__item" data-tone={entry.tone} data-gap={entry.gap || undefined}>
      {/*
        Строка про диалог — ссылка в этот диалог. Ради неё лента и открыта:
        увидел «ответ не доставлен» — открыл и переотправил, а не пошёл искать
        клиента по имени в списке. Событиям без диалога (канал отвалился,
        сотрудник отошёл, пропуск в записи) идти некуда, и они не ссылки:
        ссылка, которая никуда не ведёт, обманывает дважды.
      */}
      {entry.conversationId ? (
        <Link className="lc-feed__row" to={`/chats/${entry.conversationId}`}>
          {body}
        </Link>
      ) : (
        <div className="lc-feed__row">{body}</div>
      )}
    </li>
  );
}

export function FeedPage() {
  const entries = useFeedStore((s) => s.entries);
  const channels = useFeedStore((s) => s.channels);
  const filters = useFeedStore((s) => s.filters);
  const pausedAtSeq = useFeedStore((s) => s.pausedAtSeq);
  const setFilters = useFeedStore((s) => s.setFilters);
  const pause = useFeedStore((s) => s.pause);
  const resume = useFeedStore((s) => s.resume);
  const connection = useConnectionStore((s) => s.status);

  const scroller = useRef<HTMLDivElement>(null);

  const shown = useMemo(
    () => visibleEntries(entries, filters, pausedAtSeq),
    [entries, filters, pausedAtSeq],
  );
  const pending = pendingCount(entries, filters, pausedAtSeq);
  /*
   * ⚠ «ВИНОВАТ ОТБОР» — ЭТО УТВЕРЖДЕНИЕ, И ЕГО НАДО ПРОВЕРИТЬ.
   *
   * Раньше здесь стояло `entries.length > 0`: в буфере что-то есть — значит
   * прячет отбор. Но буфер может быть полон строк, которые прячет ПАУЗА, а не
   * отбор. Спрашиваем прямо: есть ли в буфере хоть одна строка, проходящая
   * отбор.
   */
  const отборПрячет = useMemo(
    () => entries.length > 0 && !entries.some((e) => matchesFilters(e, filters)),
    [entries, filters],
  );

  /*
   * ПРОКРУТКА ВНИЗ = «Я ЧИТАЮ» → ПАУЗА САМА.
   *
   * Кнопка паузы есть, но нажимать её человек вспоминает уже после того, как
   * строка уехала из-под глаз. Уход от верха ленты — это и есть намерение
   * читать; понимать его нажатием отдельной кнопки незачем. Обратно — только
   * руками: самоснятие паузы вернуло бы ровно ту беготню, от которой пауза и
   * защищает.
   */
  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    if (e.currentTarget.scrollTop > 24) pause();
  };

  // Сняли паузу — возвращаемся к свежим строкам. Иначе человек нажимает
  // «Продолжить», лента идёт, а он смотрит в середину и думает, что она встала.
  const backToTop = () => {
    resume();
    // Присваивание, а не `scrollTo`: последний в jsdom не реализован вовсе, а
    // нужен нам ровно один мгновенный переход к началу — плавность здесь
    // только мешала бы, лента за время анимации успевает уехать.
    if (scroller.current) scroller.current.scrollTop = 0;
  };

  // Уходя с экрана, паузу снимаем: она про чтение, а не про ленту. Оставь её —
  // и человек, вернувшийся через час, увидит позавчерашний экран и решит, что
  // за час не произошло ничего.
  useEffect(() => () => useFeedStore.getState().resume(), []);

  const offline = connection !== "open";

  return (
    <div className="lc-page lc-feed">
      <PageHeader
        title="Живая лента"
        description="Что происходит в системе прямо сейчас — сообщения, очередь, доставка, каналы"
        actions={
          /*
           * Предел памяти назван вслух: человек обязан знать, что лента — окно,
           * а не архив, и что за вчерашним надо идти в журнал.
           *
           * ⚠ НО ТОЛЬКО КОГДА ЕСТЬ ЧТО ОГРАНИЧИВАТЬ. Над пустой лентой — а она
           * пуста и при обрыве связи, и сразу после входа — обещание трёхсот
           * событий читалось как «триста уже показаны», и человек шёл искать
           * их глазами. Условие именно на `entries.length`, а не на `shown`:
           * при активном отборе окно на 300 по-прежнему правда, и молчать о
           * нём нельзя.
           */
          entries.length > 0 ? (
            <Text fz="sm" c="var(--lc-text-3)">
              Последние {FEED_LIMIT} событий
            </Text>
          ) : undefined
        }
      />

      <div className="lc-feed__filters">
        <Select
          size="xs"
          aria-label="Фильтр по каналу"
          placeholder="Канал: все"
          clearable
          data={channels.map((c) => ({ value: c.id, label: c.title }))}
          value={filters.accountId}
          onChange={(v) => setFilters({ accountId: v })}
          // Каналов в отборе нет, пока лента не увидела ни одного события: брать
          // их с сервера значило бы предлагать каналы, событий которых этот
          // человек не получает (см. directory.ts).
          disabled={channels.length === 0}
        />
        <Select
          size="xs"
          aria-label="Фильтр по виду события"
          placeholder="Все события"
          clearable
          data={GROUP_OPTIONS}
          value={filters.group}
          onChange={(v) => setFilters({ group: (v as FeedGroup | null) ?? null })}
        />

        <div className="lc-feed__spacer" />

        {pausedAtSeq === null ? (
          <Button size="xs" variant="default" onClick={pause}>
            Пауза
          </Button>
        ) : (
          <Button size="xs" variant="filled" onClick={backToTop}>
            {/* Окончание считается, а не прибивается: «1 новых» на кнопке,
                которую видят каждый день, читается как недоделка. */}
            {pending > 0
              ? `Продолжить · ${pending} ${plural(pending, "событие", "события", "событий")}`
              : "Продолжить"}
          </Button>
        )}
      </div>

      {/*
        Состояние ленты словами и в живой области — единственное, что здесь
        объявляется вслух. Сами строки не объявляются (см. `aria-live="off"`
        ниже): их бывает десяток в минуту.

        `data-state` — ОДИН признак на фразу и на тон. Раньше цвета у строки не
        было вовсе, и остановленная лента отличалась от идущей только словами
        12-м кеглем; красить её вторым таким же условием в CSS нельзя — две
        копии одного условия разъезжаются на первой правке текста.
      */}
      <p
        className="lc-feed__state"
        role="status"
        data-state={pausedAtSeq !== null ? "paused" : offline ? "offline" : "running"}
      >
        {pausedAtSeq !== null
          ? `Лента остановлена${pending > 0 ? `, за это время событий: ${pending}` : ""}`
          : offline
            ? "Нет связи с сервером — лента остановилась"
            : "Лента идёт"}
      </p>

      {/* Переключатель подробного следа — под строкой состояния и мелко: это
          не часть работы, а инструмент разбора. Видят его только те, кто может
          им пользоваться (`settings:manage`), см. `TraceSwitch`. */}
      <TraceSwitch />

      <div className="lc-feed__scroll" ref={scroller} onScroll={onScroll}>
        {shown.length === 0 ? (
          /*
            ПУСТО — ЭТО НОРМА, А НЕ ПОЛОМКА. Ночью и в выходные ничего не
            происходит, и экран обязан сказать это спокойно. Единственное
            исключение — нет связи: тогда молчание означает не тишину, а то,
            что мы ничего не слышим, и путать эти две вещи нельзя.
          */
          offline ? (
            <EmptyState
              illustration="error"
              title="Нет связи с сервером"
              description="Пока связь не вернётся, события до ленты не доходят. О пропуске лента скажет отдельной строкой"
              live="alert"
            />
          ) : отборПрячет ? (
            <EmptyState
              illustration="search"
              title="Под этот отбор ничего не попало"
              description="События идут, но не те, что вы выбрали — снимите отбор по каналу или виду события"
              live="status"
            />
          ) : pausedAtSeq !== null &&
            pausedAtSeq > 0 &&
            entries.length > 0 &&
            entries.every((e) => e.seq > pausedAtSeq) ? (
            /*
              ⚠ ОСТАНОВЛЕННАЯ ЛЕНТА ОПУСТОШАЛАСЬ САМА И ОБВИНЯЛА В ЭТОМ ОТБОР.
              Пауза хранит только НОМЕР, на котором остановились, а запись в
              буфер продолжается и режет самое старое (предел 300 строк). Через
              какое-то время ни одной строки «до паузы» в буфере не остаётся —
              и человек читал «снимите отбор по каналу», которого не ставил.
              Называем настоящую причину и настоящее действие.

              «Вытеснены» — только когда было что вытеснять и оно правда ушло:
              пауза на пустой ленте даёт порог 0, и одно пришедшее событие
              объявлялось вытеснившим строки, которых не было (проверка 24.09).
            */
            <EmptyState
              illustration="search"
              title="Строки, на которых вы остановились, вытеснены новыми"
              description="Лента на паузе, а событий пришло больше, чем она хранит. Нажмите «Продолжить», чтобы увидеть свежие"
              live="status"
            />
          ) : pausedAtSeq !== null && pending > 0 ? (
            <EmptyState
              illustration="done"
              title="Лента на паузе"
              description={`За паузой ${pending} ${plural(pending, "новое событие", "новых события", "новых событий")} — нажмите «Продолжить», чтобы их увидеть`}
              live="status"
            />
          ) : (
            <EmptyState
              illustration="done"
              title="Пока тихо"
              description="Событий нет — это нормально. Как только что-то произойдёт, строка появится здесь сама"
              live="status"
            />
          )
        ) : (
          /*
            `role="log"` описывает список правильно, но подразумевает
            объявление новых строк вслух. Для этой ленты это неприемлемо: в
            час пик она даёт десяток строк в минуту, и скринридер перестал бы
            читать всё остальное в приложении. Поэтому объявление выключено
            явно, а состояние ленты объявляется отдельной короткой строкой выше.
          */
          <ol className="lc-feed__list" role="log" aria-live="off" aria-label="События системы">
            {shown.map((e) => (
              <FeedRow key={e.seq} entry={e} />
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
