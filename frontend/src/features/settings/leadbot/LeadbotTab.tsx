import { useEffect, useMemo, useState } from "react";
import { Button, Checkbox, Group, Select, Stack, Text, TextInput, Textarea } from "@mantine/core";

import { Link } from "react-router-dom";
import { ApiError } from "@/shared/api/http";
import { выбралСам } from "@/features/chats/выборДиалога";
import { PageHeader } from "@/shared/ui/PageHeader";
import {
  useLeadbot,
  useLeadbotCalls,
  useLeadbotSilence,
  useProbe,
  useResetConnection,
  useSaveConnection,
  useSaveLeadbot,
  useTestChat,
  type LeadbotCall,
} from "./leadbotApi";
import { showToast } from "@/shared/ui/toast";
import { подписьКанала } from "@/shared/lib/channelLabel";
import "./leadbot.css";

/**
 * Границы поля «Сколько реплик отправлять» — те же, что на сервере
 * (`app/services/leadbot_admin.py`). Держать два разных представления о
 * минимуме нельзя: расхождение здесь и было дефектом 27.08 — экран отправлял
 * ноль, а сервер молча приводил его к единице.
 */
const MIN_CONTEXT_MESSAGES = 2;
const MAX_CONTEXT_MESSAGES = 100;

/**
 * РАЗДЕЛ «ЛИД-БОТ» — отдельная система, а не один из ботов.
 *
 * Решение владельца от 12 августа: «сам LeadBot это не совсем бот, я хочу чтобы
 * у него была своя собственная вкладка и настройки». Плюс два уточнения: «api
 * внутри бота можно было менять» и «сделать с ботом тестовый разговор».
 *
 * ⚠ ЧТО БЫЛО ЗДЕСЬ ДО 05.09 И ЧЕГО ЭТО СТОИЛО.
 *
 * Пять одинаковых серых блоков в одну колонку, разделённых волосяной линией:
 * связь → тест → работа → молчание → журнал. Экран отвечал на вопрос «как это
 * настроить» и НИ ОДНОЙ СТРОКОЙ не отвечал на вопрос, с которым его
 * открывают, — «работает ли он сейчас и как хорошо». Ответ на него в старой
 * раскладке был размазан по трём блокам и трём экранам прокрутки: бейдж
 * «Работает» лежал внутри третьего блока (замер: ~1400 px от верха при окне
 * 900), число ведомых диалогов — в четвёртом, исходы — в пятом. Человек
 * листал сверху вниз и собирал состояние системы по кускам сам.
 *
 * ТРИ ЗОНЫ ВМЕСТО ПЯТИ БЛОКОВ, И РОЛЬ У КАЖДОЙ СВОЯ:
 *
 *  1. ШАПКА СОСТОЯНИЯ — «лицо» раздела. Включён или нет, в каком режиме, к
 *     скольким каналам подключён, сколько диалогов ведёт прямо сейчас, что
 *     пошло не так. Числа крупно, подписи мелко. Здесь же единственная кнопка,
 *     после которой чужой регламент начинает разговаривать с живыми клиентами.
 *  2. УПРАВЛЕНИЕ (левая колонка) — «как он работает» (режим, контекст,
 *     каналы) и, отдельной карточкой ниже, «чем он подключён» (адрес, токен,
 *     тестовый разговор). Второе трогают раз в полгода и держать его вровень с
 *     ежедневным нельзя.
 *  3. НАБЛЮДЕНИЕ (правая колонка) — что бот ведёт, чего не взял и что делал.
 *     Оба списка со СВОЕЙ прокруткой: журнал отдаёт до ста строк, и общей
 *     простынёй он уносил вниз всю страницу вместе с настройками.
 *
 * Раскладка: одна колонка до 1200, две от 1200, три от 1800 — на 27 дюймах
 * журнал и «что не взял» помещаются рядом, и листать не приходится вовсе.
 */

/** «12 августа, 21:03:56» — журнал читают по времени, и секунды здесь нужны. */
function moment(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", {
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Тон строки журнала.
 *
 * ПРАВИЛО ВЛАДЕЛЬЦА: жёлтым и красным помечаем только то, что требует действия
 * человека. «Ответ ушёл клиенту» и «легло подсказкой» — штатная работа, и
 * красить её нечем. Красное — «лид-бот не ответил»: клиент не получил ничего.
 * Жёлтое — «ответ отброшен за уверенность»: ответ был, и он мог быть хорошим,
 * но клиент его не увидел, а это стоит посмотреть глазами.
 */
function toneOf(row: LeadbotCall): "bad" | "warn" | undefined {
  if (row.outcome === "unavailable") return "bad";
  if (row.outcome === "low_confidence") return "warn";
  return undefined;
}

interface СводЖурнала {
  всего: number;
  тревог: number;
  заявок: number;
}

/**
 * Числа шапки — по ТОМУ ЖЕ словарю исходов, что красит строки журнала.
 *
 * Плитка «Пошло не так» и цветная полоска у строки обязаны считать одно и то
 * же: разойдись они — и человек увидит в шапке ноль, а в списке две красных
 * строки, после чего перестанет верить обоим. Поэтому здесь зовётся `toneOf`,
 * а не переписывается его условие второй раз.
 *
 * `undefined` на входе означает «журнал не прочитан» и даёт `null` на выходе:
 * пустой массив и непрочитанный журнал — РАЗНЫЕ вещи, и в шапке они обязаны
 * выглядеть по-разному (тот же дефект уже разбирался ниже, у пустого состояния
 * списка).
 */
function сводЖурнала(rows: LeadbotCall[] | undefined): СводЖурнала | null {
  if (!rows) return null;
  let тревог = 0;
  let заявок = 0;
  for (const row of rows) {
    if (toneOf(row)) тревог += 1;
    if (row.lead_ready) заявок += 1;
  }
  return { всего: rows.length, тревог, заявок };
}

/**
 * ШАПКА СОСТОЯНИЯ.
 *
 * ⚠ ЖУРНАЛ СЮДА БЕРЁТСЯ БЕЗ ОТБОРОВ, И ЭТО НАМЕРЕННО. Отбор в журнале ниже
 * сужает список — «только канал Дамира», «только беда». Возьми шапка тот же
 * отбор, и общее число обращений менялось бы от нажатия на фильтр: показатель,
 * который зависит от того, как на него смотрят, не показатель.
 *
 * ⚠ ЧЕРТА ВМЕСТО НУЛЯ, КОГДА ИСТОЧНИК МОЛЧИТ. Отказ запроса нельзя показывать
 * нулём: «Пошло не так: 0» на упавшем журнале — это экран, который врёт ровно
 * в тот час, когда его открывают из-за поломки (тот же разбор, что у пустого
 * состояния списка ниже, NOTIF-01).
 */
function StateHeader({
  onlyTrouble,
  setOnlyTrouble,
}: {
  onlyTrouble: boolean;
  setOnlyTrouble: (v: boolean) => void;
}) {
  const overview = useLeadbot();
  const silence = useLeadbotSilence();
  const calls = useLeadbotCalls({ onlyTrouble: false, accountId: null });
  const save = useSaveLeadbot();
  const [error, setError] = useState<string | null>(null);

  const свод = useMemo(() => сводЖурнала(calls.data?.items), [calls.data]);
  const loaded = overview.data;
  if (!loaded) return null;

  // `null` — «ручка ещё не ответила или отказала», и в плитке он станет чертой.
  const ведомые = silence.data?.in_progress;
  const ведёт = ведомые ? ведомые.length : null;
  const включён = loaded.enabled;
  // Включён и ни одного канала — состояние, которое бейдж «Работает» скрывал
  // целиком: бот жив, настроен и не увидит ни одного клиента.
  const негдеРаботать = включён && loaded.account_ids.length === 0;

  const переключить = async () => {
    setError(null);
    try {
      await save.mutateAsync({ enabled: !включён });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось сохранить");
    }
  };

  return (
    <section className="lc-card lb-card lb-state" aria-label="Состояние лид-бота">
      <div className="lb-state__line" data-on={включён || undefined}>
        <span className="lb-state__dot" />
        <span className="lb-state__word">{включён ? "Работает" : "Выключен"}</span>
        <span className="lb-state__facts">
          {loaded.mode === "auto" ? "ответ уходит клиенту" : "ответ ложится подсказкой оператору"}
          {" · "}
          каналов {loaded.account_ids.length} из {loaded.accounts.length}
        </span>
        <Button
          className="lc-btn lb-state__switch"
          variant={включён ? "default" : "filled"}
          onClick={переключить}
          loading={save.isPending}
          disabled={!loaded.is_ready && !включён}
        >
          {включён ? "Выключить лид-бота" : "Включить лид-бота"}
        </Button>
      </div>

      {!loaded.is_ready && !включён && (
        <p className="lb-state__hint">Сначала задайте адрес и токен</p>
      )}
      {негдеРаботать && (
        <p className="lb-state__alarm">
          Включён, но не подключён ни к одному каналу — отвечать ему негде.
        </p>
      )}
      {error && (
        <p className="lb-state__alarm" role="alert">
          {error}
        </p>
      )}

      <div className="lb-kpi">
        <div className="lb-kpi__cell">
          <span className="lb-kpi__cap">Ведёт сейчас</span>
          <span className="lb-kpi__num lc-num">{ведёт ?? "—"}</span>
        </div>
        {/*
          Плитка беды — кнопка, а не число: увидев «7», человек тут же хочет
          посмотреть эти семь, а отбор журнала стоит колонкой правее и на
          телефоне — экраном ниже.
        */}
        <button
          type="button"
          className="lb-kpi__cell lb-kpi__cell--act"
          data-active={onlyTrouble || undefined}
          aria-pressed={onlyTrouble}
          onClick={() => setOnlyTrouble(!onlyTrouble)}
        >
          <span className="lb-kpi__cap">Пошло не так</span>
          <span className="lb-kpi__num lc-num">{свод ? свод.тревог : "—"}</span>
        </button>
        <div className="lb-kpi__cell">
          <span className="lb-kpi__cap">Заявок собрано</span>
          <span className="lb-kpi__num lc-num">{свод ? свод.заявок : "—"}</span>
        </div>
        <div className="lb-kpi__cell">
          <span className="lb-kpi__cap">Каналов у бота</span>
          <span className="lb-kpi__num lc-num">
            {loaded.account_ids.length} из {loaded.accounts.length}
          </span>
        </div>
      </div>

      {calls.isError ? (
        <p className="lb-kpi__source lb-kpi__source--bad">
          Журнал не прочитан — «пошло не так» и «заявок» посчитать не из чего.
        </p>
      ) : свод ? (
        <p className="lb-kpi__source">
          «Пошло не так» и «заявок» — по последним {свод.всего} обращениям из журнала
        </p>
      ) : (
        <p className="lb-kpi__source">Читаем журнал…</p>
      )}
    </section>
  );
}

function ConnectionBlock() {
  const overview = useLeadbot();
  const save = useSaveConnection();
  const reset = useResetConnection();
  const probe = useProbe();

  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  const loaded = overview.data;
  /*
   * ⚠ ЗАВИСИМОСТЬ — АДРЕС, А НЕ ВЕСЬ ОТВЕТ.
   *
   * Раньше здесь стояло `[loaded]`, то есть объект целиком. Любое сохранение
   * НА ЭТОЙ ЖЕ странице — число сообщений контекста, режим — гасит запрос
   * обзора, тот приходит заново, и объект получается ДРУГОЙ даже при тех же
   * данных. Эффект срабатывал и затирал адрес, который в этот момент правили:
   * человек набирал новый, трогал соседний блок и получал обратно старый — без
   * единого сообщения. Сравнение по строке чинит это само: тот же адрес —
   * та же зависимость, эффект молчит.
   */
  const адресСервера = loaded?.connection.url;
  useEffect(() => {
    if (адресСервера !== undefined) setUrl(адресСервера);
  }, [адресСервера]);

  if (!loaded) return null;
  const { connection } = loaded;
  const changed = url.trim() !== connection.url || token.trim() !== "";

  const submit = async () => {
    setError(null);
    try {
      // Токен уходит ТОЛЬКО когда его набрали. Пустое поле означает «оставить
      // прежний»: показать нынешний экран не может, и трактуй пустоту как
      // «убрать» — исправление опечатки в адресе разорвало бы связь молча.
      await save.mutateAsync(token.trim() ? { url: url.trim(), token: token.trim() } : { url: url.trim() });
      setToken("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось сохранить");
    }
  };

  return (
    <section className="lb-part">
      <h3 className="lb-part__title">Связь</h3>
      <p className="lb-part__lead">
        Адрес его ручки и служебный токен. Лид-бот живёт на своём сервере: сюда приходят
        только ответы, регламент и цены остаются у него.
      </p>

      <Stack gap="sm" className="lb-form">
        <TextInput
          className="lc-field"
          label="Адрес"
          placeholder="http://10.10.0.2:8790"
          description="Начинается с http:// или https://"
          value={url}
          onChange={(e) => setUrl(e.currentTarget.value)}
        />
        <TextInput
          className="lc-field"
          label="Токен"
          type="password"
          placeholder={connection.token_set ? "Задан — оставьте пустым, чтобы не менять" : "Не задан"}
          description="Только латиница и цифры: заголовки запроса кириллицу не переносят"
          value={token}
          onChange={(e) => setToken(e.currentTarget.value)}
        />
        {error && (
          <Text fz="sm" c="var(--lc-danger-text)" role="alert">
            {error}
          </Text>
        )}
        <Group gap="sm">
          <Button className="lc-btn" onClick={submit} loading={save.isPending} disabled={!changed}>
            Сохранить
          </Button>
          <Button
            className="lc-btn"
            variant="default"
            onClick={() => probe.mutate()}
            loading={probe.isPending}
            disabled={!loaded.is_ready}
          >
            Проверить связь
          </Button>
          {connection.source === "db" && (
            <Button
              className="lc-btn"
              variant="subtle"
              onClick={() => reset.mutate()}
              loading={reset.isPending}
            >
              Вернуть как при установке
            </Button>
          )}
        </Group>

        {probe.data && (
          <div className="lb-probe" data-ok={probe.data.ok || undefined} role="status">
            {probe.data.ok ? (
              <>
                <strong>Связь есть.</strong> Ответил за {probe.data.ms} мс
                {probe.data.layer ? ` — ${probe.data.layer}` : ""}.
                {probe.data.reply && <div className="lb-probe__reply">«{probe.data.reply}»</div>}
              </>
            ) : (
              <>
                <strong>Связи нет.</strong> {probe.data.error}
              </>
            )}
          </div>
        )}
      </Stack>
    </section>
  );
}

/**
 * ТЕСТОВЫЙ РАЗГОВОР.
 *
 * ⚠ НИ ОДНО СООБЩЕНИЕ ОТСЮДА НЕ УХОДИТ КЛИЕНТУ. Здесь нет диалога, нет канала
 * и нет доставки — только вызов чужой ручки и показ того, что она вернула.
 * Ради этого свойства он и заводится: посмотреть, что бот отвечает, ДО того
 * как пустить его к живым людям. В журнал работы это тоже не попадает — иначе
 * журнал перестал бы отвечать на вопрос «как бот вёл себя в бою».
 */
function TestChatBlock() {
  const overview = useLeadbot();
  const test = useTestChat();
  const [text, setText] = useState("Здравствуйте! Не морозит холодильник Indesit, что делать?");
  const [item, setItem] = useState("");

  const result = test.data;

  return (
    <section className="lb-part">
      <h3 className="lb-part__title">Тестовый разговор</h3>
      <p className="lb-part__lead">
        Спросите так, как спросил бы клиент. Никто ничего не получит: сообщение никуда не
        отправляется, диалог не заводится, в журнал работы это не попадает.
      </p>

      <Stack gap="sm" className="lb-form">
        <Textarea
          className="lc-field"
          label="Сообщение клиента"
          autosize
          minRows={2}
          maxRows={6}
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
        />
        <TextInput
          className="lc-field"
          label="Объявление"
          description="Необязательно. От него зависит, что бот считает предметом разговора"
          placeholder="Ремонт холодильников на дому"
          value={item}
          onChange={(e) => setItem(e.currentTarget.value)}
        />
        <Group gap="sm">
          <Button
            className="lc-btn"
            onClick={() =>
              test.mutate({
                dialog: [{ role: "user", content: text }],
                item_title: item.trim() || undefined,
              })
            }
            loading={test.isPending}
            disabled={!text.trim() || !overview.data?.is_ready}
          >
            Спросить лид-бота
          </Button>
          {!overview.data?.is_ready && (
            <Text fz="sm" c="var(--lc-text-3)">
              Сначала задайте адрес и токен
            </Text>
          )}
        </Group>

        {result && (
          <div className="lb-answer" data-ok={result.ok || undefined}>
            {result.ok ? (
              <>
                <div className="lb-answer__reply">{result.reply || "— пустой ответ —"}</div>
                {/*
                  ПОЧЕМУ ЗДЕСЬ ПОКАЗАНО БОЛЬШЕ, ЧЕМ ВИДИТ ДВИЖОК. Главный вопрос
                  владельца к любому ответу — «это регламент или он сам
                  придумал». Ответ на него в слое: «роутер» значит сработало
                  детерминированное правило, «модель» — думала модель.
                */}
                <dl className="lb-facts">
                  <div>
                    <dt>Кто ответил</dt>
                    <dd>{result.layer || "—"}</dd>
                  </div>
                  <div>
                    <dt>Уверенность</dt>
                    <dd>{result.confidence != null ? result.confidence.toFixed(2) : "—"}</dd>
                  </div>
                  <div>
                    <dt>Нужен человек</dt>
                    <dd>{result.needs_operator ? "да" : "нет"}</dd>
                  </div>
                  <div>
                    <dt>Ответил за</dt>
                    <dd>{result.ms} мс</dd>
                  </div>
                </dl>
                {result.escalation?.label && (
                  <div className="lb-answer__note">
                    Передал бы человеку: {result.escalation.label}
                    {result.escalation.deadline_min != null &&
                      ` — ответить за ${result.escalation.deadline_min} мин`}
                  </div>
                )}
                {result.lead_ready && <div className="lb-answer__note">Заявка собрана</div>}
                {(result.warnings ?? []).map((w) => (
                  <div className="lb-answer__warn" key={w}>
                    {w}
                  </div>
                ))}
              </>
            ) : (
              <div className="lb-answer__error">Лид-бот не ответил: {result.error}</div>
            )}
          </div>
        )}
      </Stack>
    </section>
  );
}

/**
 * КАК БОТ РАБОТАЕТ — то, что трогают, когда бот уже подключён.
 *
 * Кнопки включения здесь БОЛЬШЕ НЕТ: она переехала в шапку состояния. Прежний
 * довод («включение последним, когда оба вопроса уже отвечены») никуда не
 * делся, но он про порядок ЧТЕНИЯ, а не про место на странице: в шапке рядом с
 * кнопкой стоят и режим, и число каналов, то есть ответы на оба вопроса видны
 * в момент нажатия, а не тремя экранами выше.
 */
function WorkBlock() {
  const overview = useLeadbot();
  const save = useSaveLeadbot();
  const [error, setError] = useState<string | null>(null);
  const loaded = overview.data;

  /*
   * ⚠ КАНАЛЫ УХОДЯТ ПОЛНЫМ СПИСКОМ, ПОЭТОМУ ПО ОДНОМУ ЗАПРОСУ ЗА РАЗ (проверка
   * 24.09). Два быстрых щелчка по разным каналам собирали второй список из
   * ещё не обновлённых данных — без первого канала, и сервер его отвязывал.
   * Пока запрос в пути, флажки заперты, а нажатый уже показан отмеченным:
   * неподвижный флажок провоцировал щёлкнуть ещё раз.
   */
  const [pendingIds, setPendingIds] = useState<string[] | null>(null);

  const patch = async (body: Parameters<typeof save.mutateAsync>[0]) => {
    setError(null);
    try {
      await save.mutateAsync(body);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось сохранить");
    }
  };

  if (!loaded) return null;
  const shownIds = pendingIds ?? loaded.account_ids;

  return (
    <section className="lc-card lb-card">
      <h2 className="lb-card__title">Как бот работает</h2>
      <p className="lb-card__lead">
        Что делать с ответом, сколько разговора отдавать боту и в какие каналы его пускать.
      </p>

      <Stack gap="sm" className="lb-form">
        <Select
          className="lc-field"
          label="Что делать с ответом"
          description="«Подсказка» — ответ видит только оператор. «Автоответ» — текст уходит клиенту"
          value={loaded.mode}
          onChange={(v) => v && patch({ mode: v as "suggest" | "auto" })}
          data={[
            { value: "suggest", label: "Положить подсказкой оператору" },
            { value: "auto", label: "Отправить клиенту" },
          ]}
          allowDeselect={false}
        />
        {/*
          ⚠ ПУСТОЕ ПОЛЕ — ЭТО «НЕ ТРОГАЛИ», А НЕ НОЛЬ. Здесь стояло
          `Number(e.currentTarget.value)` без проверки на пустоту, а `Number("")`
          равен НУЛЮ, и `Number.isFinite(0)` его пропускал. Стоило очистить поле,
          чтобы набрать заново, — и по уходу фокуса уезжал ноль. Сервер молча
          приводил его к единице (`max(1, …)`), лид-бот получал контекст из одной
          реплики и терял весь разговор. Ровно это владелец и видел 23.08:
          «context_messages сбит с 12 на 1». Ошибки не было, в журнале — обычное
          сохранение, и связать одно с другим было не с чем.

          Теперь пустое поле возвращается к сохранённому значению, а не
          отправляется: человек очистил его, чтобы набрать другое, и ушёл —
          настройка обязана остаться прежней.
        */}
        <TextInput
          className="lc-field"
          label="Сколько реплик отправлять"
          description="Лид-бот ведёт разговор целиком, и на коротком контексте теряет его начало"
          type="number"
          min={MIN_CONTEXT_MESSAGES}
          max={MAX_CONTEXT_MESSAGES}
          defaultValue={loaded.context_messages}
          onBlur={(e) => {
            const raw = e.currentTarget.value.trim();
            if (raw === "") {
              e.currentTarget.value = String(loaded.context_messages);
              return;
            }
            const value = Number(raw);
            if (!Number.isInteger(value) || value < MIN_CONTEXT_MESSAGES || value > MAX_CONTEXT_MESSAGES) {
              // Границы те же, что на сервере. Возвращаем прежнее значение:
              // оставить на экране число, которого нет в настройках, — значит
              // соврать о состоянии системы.
              e.currentTarget.value = String(loaded.context_messages);
              showToast({
                title: "Не сохранилось",
                message: `Сколько реплик отправлять: от ${MIN_CONTEXT_MESSAGES} до ${MAX_CONTEXT_MESSAGES}`,
                color: "red",
              });
              return;
            }
            if (value !== loaded.context_messages) {
              patch({ context_messages: value });
            }
          }}
        />

        <div>
          <Text fz="sm" fw={500}>
            Каналы
          </Text>
          <Text fz="xs" c="var(--lc-text-3)">
            Обращения этих каналов уходят лид-боту
          </Text>
          {/*
            ⚠ СПИСОК СО СВОЕЙ ПРОКРУТКОЙ. Владелец присылал снимок именно этого
            места: «два десятка строк» флажков, за которыми настройка режима и
            контекста уезжала за нижний край. Каналов у мастерской двадцать с
            лишним и будет больше, а вопрос «в какие каналы пускать» — не тот,
            ради которого стоит терять из виду остальную форму.
          */}
          <Stack gap={4} mt={8} className="lb-channels">
            {loaded.accounts.map((account) => (
              <Checkbox
                key={account.id}
                className="lb-channels__item"
                label={
                  <span>
                    {подписьКанала(account)}
                    {account.busy_with_other_bot && (
                      <Text component="span" fz="xs" c="var(--lc-text-3)">
                        {" "}
                        — занят другим ботом
                      </Text>
                    )}
                  </span>
                }
                checked={shownIds.includes(account.id)}
                disabled={save.isPending}
                onChange={(e) => {
                  const next = e.currentTarget.checked
                    ? [...loaded.account_ids, account.id]
                    : loaded.account_ids.filter((id) => id !== account.id);
                  setPendingIds(next);
                  void patch({ account_ids: next }).finally(() => setPendingIds(null));
                }}
              />
            ))}
            {loaded.accounts.length === 0 && (
              <Text fz="sm" c="var(--lc-text-3)">
                Каналов пока нет
              </Text>
            )}
          </Stack>
        </div>

        {error && (
          <Text fz="sm" c="var(--lc-danger-text)" role="alert">
            {error}
          </Text>
        )}
      </Stack>
    </section>
  );
}

function JournalBlock({
  onlyTrouble,
  setOnlyTrouble,
}: {
  onlyTrouble: boolean;
  setOnlyTrouble: (v: boolean) => void;
}) {
  const overview = useLeadbot();
  const [accountId, setAccountId] = useState<string | null>(null);
  const calls = useLeadbotCalls({ onlyTrouble, accountId });

  const channels = useMemo(
    () => [
      { value: "", label: "Канал: все" },
      ...(overview.data?.accounts ?? []).map((a) => ({ value: a.id, label: подписьКанала(a) })),
    ],
    [overview.data],
  );

  const rows = calls.data?.items ?? [];

  return (
    <section className="lc-card lb-card">
      <h2 className="lb-card__title">Что делал лид-бот</h2>
      <p className="lb-card__lead">
        По строке на каждое обращение: что спросил клиент, что ответил бот, почему именно
        так и дошёл ли ответ. Полная переписка — в «Чатах», здесь только решения.
      </p>

      <Group gap="sm" className="lb-filters">
        <Select
          className="lc-field"
          data={channels}
          value={accountId ?? ""}
          onChange={(v) => setAccountId(v || null)}
          allowDeselect={false}
          aria-label="Отбор по каналу"
        />
        <Checkbox
          className="lb-filters__flag"
          label="Только там, где что-то пошло не так"
          checked={onlyTrouble}
          onChange={(e) => setOnlyTrouble(e.currentTarget.checked)}
        />
      </Group>

      {/*
        ⚠ ПУСТО И «НЕ СМОГЛИ ЗАГРУЗИТЬ» — РАЗНЫЕ ВЕЩИ (28.08).
        Здесь стояло одно условие `rows.length === 0`, а `rows` берётся как
        `calls.data?.items ?? []`: при отказе запроса `data` остаётся undefined,
        и экран печатал «Ничего не сломалось». Владелец открывает журнал ровно
        тогда, когда что-то ломается, — в момент выкатки или сбоя базы, — и
        получает ответ «всё в порядке». Отказ живёт долго: две попытки, потом
        `refetchInterval` каждые 30 секунд падает заново.

        Тот же дефект уже разобран в центре уведомлений (NOTIF-01: «пустой стор
        и стор, который не смогли наполнить, — РАЗНЫЕ вещи»); здесь признака не
        было. Спрашиваем состояние запроса, а не длину массива.
      */}
      {calls.isError ? (
        <Text fz="sm" c="var(--lc-danger)" className="lb-empty">
          Не удалось загрузить журнал — это сбой связи, а не тишина бота.
          Обновите страницу; если повторится, посмотрите доступность сервера.
        </Text>
      ) : calls.isPending ? (
        <Text fz="sm" c="var(--lc-text-3)" className="lb-empty">
          Загружаем…
        </Text>
      ) : rows.length === 0 ? (
        <Text fz="sm" c="var(--lc-text-3)" className="lb-empty">
          {onlyTrouble ? "Ничего не сломалось" : "Лид-бот пока не отвечал"}
        </Text>
      ) : (
        /*
          ⚠ ПРОКРУТКА У СПИСКА, А НЕ У СТРАНИЦЫ. Сервер отдаёт до ста строк, в
          каждой вопрос клиента и ответ бота целиком — замер на боевых данных:
          лента вырастала до нескольких тысяч пикселей и уносила вниз всё, что
          лежало выше, вместе с отбором по каналу. Чтобы сменить канал, человек
          гнал страницу обратно вверх. Своя прокрутка оставляет отбор и шапку
          состояния на месте — тем же приёмом и по тому же доводу, что и
          `.settings-section` (прокрутка живёт у содержимого, а не у каркаса).
        */
        <div className="lb-journal__scroll">
          <ul className="lb-log">
            {rows.map((row) => (
              <li className="lb-log__item" key={row.id} data-tone={toneOf(row)}>
                <div className="lb-log__head">
                  <time dateTime={row.at}>{moment(row.at)}</time>
                  <span className="lb-log__outcome">{row.outcome_label}</span>
                  {row.layer && <span className="lb-log__layer">{row.layer}</span>}
                  {row.ms != null && <span className="lb-log__ms lc-num">{row.ms} мс</span>}
                </div>
                {row.question && <div className="lb-log__q">Клиент: {row.question}</div>}
                {row.reply && <div className="lb-log__a">Бот: {row.reply}</div>}
                {row.error && <div className="lb-log__err">{row.error}</div>}
                {row.escalation?.label && (
                  <div className="lb-log__note">
                    Передал человеку: {row.escalation.label}
                    {row.escalation.deadline_min != null &&
                      ` — ответить за ${row.escalation.deadline_min} мин`}
                  </div>
                )}
                {row.lead_ready && <div className="lb-log__note">Заявка собрана</div>}
                {row.warnings.map((w) => (
                  <div className="lb-log__note" key={w}>
                    {w}
                  </div>
                ))}
                {row.conversation_id && (
                  // Переход роутером, а не `<a href>` (проверка 24.09): обычная
                  // ссылка перезагружала всё приложение — refresh, /auth/me,
                  // сокет, списки — вместо мгновенного перехода.
                  <Link
                    className="lb-log__link"
                    to={`/chats/${row.conversation_id}`}
                    onClick={() => выбралСам(row.conversation_id!)}
                  >
                    Открыть диалог
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/**
 * Почему бот молчит — блок, которого не хватало (жалоба владельца 19.08).
 *
 * ЧТО БЫЛО. Владелец включил бота, тот ничего не взял; включил подсказки —
 * тоже ничего. Бот при этом вёл себя ПРАВИЛЬНО: он входит только в свежий
 * диалог и только туда, где человек ещё не отвечал, а новых диалогов в те
 * часы не было. Но система об этом молчала: журнал обращений пуст, и пустота
 * читается как поломка. Человек остаётся один на один с вопросом «оно
 * сломано или так задумано?» — и это худшее состояние интерфейса.
 *
 * Причину считает тот же код, который решает пускать бота, — разъехаться им
 * негде.
 *
 * ⚠ 05.09: ДВА СПИСКА СТАЛИ СПИСКАМИ. Раньше и «ведёт», и «не взял»
 * печатались вереницей одинаковых абзацев `<Text fz="sm">` без счётчиков и
 * границ: двадцать строк подряд, по которым не понять, где кончается один
 * список и начинается другой. Теперь у каждого своё имя, своё число и своя
 * прокрутка.
 */
function SilenceBlock() {
  const данные = useLeadbotSilence();
  const каналы = данные.data?.channels ?? [];
  const ведёт = данные.data?.in_progress ?? [];
  const неВзял = данные.data?.not_taken ?? [];
  const безБота = каналы.filter((к) => !к.bot_attached);

  return (
    <section className="lc-card lb-card">
      <h2 className="lb-card__title">Что бот ведёт и чего не взял</h2>
      <p className="lb-card__lead">
        Бот входит только в новый диалог и только туда, где ещё не отвечал человек. Если он
        молчит — здесь написано, почему именно.
      </p>

      {/*
        Отказ запроса нельзя молчаливо превращать в пустые списки: «ведёт 0,
        не взял 0» на упавшей ручке читается как «бот стоит», а он в этот
        момент может работать. Тот же довод, что у пустого состояния журнала.
      */}
      {данные.isError && (
        <p className="lb-watch__warn" role="alert">
          Не удалось прочитать, что бот ведёт, — это сбой связи, а не его молчание.
        </p>
      )}

      {безБота.length > 0 && (
        <p className="lb-watch__warn">
          Каналы без бота: {безБота.map(подписьКанала).join(", ")} — он туда не войдёт вовсе.
        </p>
      )}

      <div className="lb-watch__group">
        <div className="lb-watch__head">
          <span className="lb-watch__name">Ведёт сейчас</span>
          <span className="lb-watch__count lc-num">{данные.data ? ведёт.length : "—"}</span>
        </div>
        <ul className="lb-watch__list">
          {ведёт.map((д) => (
            <li className="lb-watch__row" key={д.conversation_id}>
              <span className="lb-watch__id">{д.conversation_id.slice(0, 8)}</span>
              <span className="lb-watch__why">{д.status}</span>
            </li>
          ))}
          {данные.data && ведёт.length === 0 && (
            <li className="lb-watch__row lb-watch__row--none">Ни одного диалога прямо сейчас</li>
          )}
        </ul>
      </div>

      <div className="lb-watch__group">
        <div className="lb-watch__head">
          <span className="lb-watch__name">Не взял</span>
          <span className="lb-watch__count lc-num">{данные.data ? неВзял.length : "—"}</span>
        </div>
        <ul className="lb-watch__list">
          {неВзял.map((д) => (
            <li className="lb-watch__row" key={д.conversation_id}>
              <span className="lb-watch__id">{д.conversation_id.slice(0, 8)}</span>
              <span className="lb-watch__why">{д.reason_label}</span>
            </li>
          ))}
          {данные.data && неВзял.length === 0 && (
            <li className="lb-watch__row lb-watch__row--none">Диалогов пока нет</li>
          )}
        </ul>
      </div>
    </section>
  );
}

export function LeadbotTab() {
  const overview = useLeadbot();
  /*
   * Отбор беды поднят сюда: его включает и флажок в журнале, и плитка «Пошло
   * не так» в шапке. Держи каждый свой — и человек, нажав плитку, увидел бы
   * рядом невключённый флажок, то есть два разных ответа на один вопрос.
   */
  const [onlyTrouble, setOnlyTrouble] = useState(false);

  return (
    <div className="settings-section lb-page">
      <PageHeader
        title="Лид-бот"
        description="Отдельная система на своём сервере: отвечает клиентам по регламенту мастерской"
      />
      {overview.isError && (
        <Text c="var(--lc-danger-text)" role="alert">
          Не удалось загрузить настройки
        </Text>
      )}

      <StateHeader onlyTrouble={onlyTrouble} setOnlyTrouble={setOnlyTrouble} />

      <div className="lb-cols">
        <div className="lb-col lb-col--control">
          <WorkBlock />
          <section className="lc-card lb-card">
            <h2 className="lb-card__title">Подключение и проверка</h2>
            <p className="lb-card__lead">
              Настраивают один раз при установке и возвращаются сюда, только когда бот
              переехал на другой сервер или перестал отвечать.
            </p>
            <ConnectionBlock />
            <TestChatBlock />
          </section>
        </div>

        <div className="lb-col lb-col--watch">
          <SilenceBlock />
        </div>

        <div className="lb-col lb-col--log">
          <JournalBlock onlyTrouble={onlyTrouble} setOnlyTrouble={setOnlyTrouble} />
        </div>
      </div>
    </div>
  );
}
