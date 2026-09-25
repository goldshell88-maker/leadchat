import { Suspense, lazy, useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Button,
  Group,
  Loader,
  Menu,
  Progress,
  Text,
  TextInput,
  Tooltip,
  VisuallyHidden,
} from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ApiError, http, request } from "@/shared/api/http";
import { fetchAvitoAccounts } from "@/shared/api/reference";
import { qk } from "@/shared/api/queryKeys";
import { usePermissions } from "@/shared/auth/usePermissions";
import type { AvitoAccountDto, ConnectUrlResponse } from "@/shared/api/types";
import { formatDate } from "@/shared/lib/formatTime";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import { EmptyState } from "@/shared/ui/EmptyState";
import { UserAvatar } from "@/shared/ui/UserAvatar";
import {
  describeHistorySize,
  fetchChannelHistorySize,
  OPERATOR_AVATARS_SHOWN,
  type ChannelHistorySize,
} from "./api";
import { ConnectChannelWizard } from "./ConnectChannelWizard";
import "./accounts.css";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";
import { IconChevronRight, IconMore, IconRefresh } from "@/shared/ui/Icon";
import { StatusDot, type DotTone } from "@/shared/ui/StatusDot";
import { TokenLine, WebhookLine } from "./ChannelHealth";
import { AddressDetectBlock } from "./AddressDetectBlock";
import { PhoneDetectBlock } from "./PhoneDetectBlock";
import { ThreadBlock } from "./ThreadBlock";
import { WeekBars } from "./WeekBars";
import { PageHeader } from "@/shared/ui/PageHeader";
import { подписьКанала } from "@/shared/lib/channelLabel";

/**
 * Экран назначения операторов (7.2) — отдельным чанком: одна роль, редкое
 * открытие, в стартовом бандле рабочего места ему делать нечего (03 §7).
 */
const AssignOperatorsModal = lazy(() =>
  import("./AssignOperatorsModal").then((m) => ({ default: m.AssignOperatorsModal })),
);

/** Подписи статусов канала: точка цветом смысл не несёт (16 §7). */
const STATUS_LABEL: Record<string, string> = {
  active: "работает",
  needs_reauth: "требует переподключения",
  disabled: "отключён",
};

/*
 * ЗДЕСЬ ЖИЛИ ДВА ПОРОГА, И ОБОИХ БОЛЬШЕ НЕТ.
 *
 * `TOKEN_EXPIRING_MS = 48 ч` красил строку токена жёлтым со знаком «⚠️», а
 * `WEBHOOK_SILENT_MS = 4 ч` объявлял канал молчащим. Первый горел ВСЕГДА:
 * токен Авито живёт сутки, двое суток из них не вычесть. Второй судил по
 * абсолютному времени тишины и потому вечно горел на редком канале, оставляя
 * «✓ в порядке» на канале с дневным потоком, — то есть мерил поток клиентов, а
 * не исправность.
 *
 * Оба вопроса — «пора ли тревожиться» — решает теперь сервер
 * (`app/services/channel_health.py`), и решает по данным, которых у экрана нет:
 * получалось ли автообновление, что ответил Авито на сверку подписки, каков
 * обычный ритм этого канала в рабочие часы. Экран рисует присланное состояние;
 * разбор — в шапке `ChannelHealth.tsx`.
 */

/**
 * Ход загрузки истории — то, что сервер отдаёт с 11 августа.
 *
 * ПОЧЕМУ ТИП ОПИСАН ЗДЕСЬ, А НЕ В shared/api/types. Общий тип правится в
 * соседней ветке, и разъехавшиеся правки одного файла стоят дороже, чем
 * пятнадцать строк здесь. Поля необязательные не для красоты: прогон,
 * начатый до выкатки, знает только `chats_offset`, и карточка обязана
 * пережить это молча — показать ход без знаменателя, а не «загружено 0».
 */
type BackfillProgress = {
  status: "running" | "idle" | "failed" | "stopped";
  /** census — идёт перепись чатов, loading — сама загрузка. */
  phase?: string | null;
  chats_offset?: number | null;
  loaded?: number | null;
  total?: number | null;
  failed_chats?: number | null;
  queued?: number | null;
  depth?: string | null;
};

/** «Загружено N из M» одной строкой; без M — просто N. */
function progressLabel(state: BackfillProgress): string {
  const done = state.loaded ?? state.chats_offset ?? 0;
  return state.total != null ? `${done} из ${state.total}` : `${done}`;
}

/**
 * Полоса ХОДА, а не бесконечная анимация.
 *
 * Раньше здесь всегда стояло `value={100}` со striped-анимацией: полоса
 * бежала одинаково и на первой минуте, и на последней. При девяти аккаунтах
 * по 300+ объявлений загрузка идёт часами, и единственный вопрос к этому
 * экрану — «сколько осталось». Пока идёт перепись, знаменателя ещё нет —
 * тогда и только тогда полоса снова бесконечная.
 */
function BackfillLine({ state }: { state: BackfillProgress }) {
  const counting = state.phase === "census" || state.total == null;
  // Полоса идёт по РАЗОБРАННЫМ чатам (`chats_offset`), а подпись называет
  // ЗАГРУЖЕННЫЕ диалоги. Разница честная: пустой чат и чат старее выбранной
  // глубины остаются позади, но диалогом не становятся — считай полосу по
  // диалогам, и она застряла бы, не дойдя до конца работы.
  const done = state.chats_offset ?? state.loaded ?? 0;
  const value = counting ? 100 : Math.min(100, Math.round((done / Math.max(1, state.total ?? 1)) * 100));
  return (
    <div className="account-card__backfill">
      <Progress
        value={value}
        striped={counting}
        animated={counting}
        size="sm"
        color="lp"
        aria-label="Загрузка истории"
      />
      <Text fz="xs" c="var(--lc-text-3)" role="status">
        {state.phase === "census"
          ? `Считаем чаты… нашли ${state.total ?? 0}`
          : `Загружаем историю… ${progressLabel(state)} диалогов`}
        {state.queued ? `, из них ${state.queued} в «Входящих»` : ""}
      </Text>
    </div>
  );
}


/** Что стало с выключенным каналом — фраза одна на строку и на подсказку. */
const ОТКЛЮЧЁН_ВРУЧНУЮ =
  "Отключён вручную, переписка удалена. Включить можно в любой момент — новые обращения снова начнут приходить.";

/** Что означает «Операторы: все» — на сервере это канал, открытый всем. */
const ВСЕ_ОПЕРАТОРЫ = "Никто не назначен — обращения канала видят все операторы";

/**
 * Состав канала: «Операторы: N» и лица (Jivo 15 §2.2).
 *
 * Пустой набор показывается как «Операторы: все» и никак иначе: на сервере
 * канал без назначенных доступен всем операторам, и карточка обязана говорить
 * ровно то же самое. «Операторы: 0» читалось бы как «канал закрыт» — это
 * противоположный смысл.
 *
 * ⚠ НАЖИМАЮТ НА САМ СОСТАВ, А НЕ НА ПОДПИСЬ РЯДОМ С НИМ (макет 04.09, п. 6).
 * Рядом со значением стояла кнопка «Назначить» — то же самое, от чего
 * отказались у переименования: подпись действия в каждой из тридцати пяти
 * строк ради работы, которую делают раз в месяц. Целью нажатия стали счёт и
 * лица, то есть ровно то, что действие меняет; имя действия осталось в
 * доступном имени кнопки и в подсказке при наведении.
 */
function OperatorsRow({ account, onAssign }: { account: AvitoAccountDto; onAssign(): void }) {
  const { can } = usePermissions();
  const count = account.operators?.count ?? 0;
  const preview = (account.operators?.preview ?? []).slice(0, OPERATOR_AVATARS_SHOWN);
  const hidden = count - preview.length;

  const состав = (
    <>
      {/*
        ⚠ ПОЯСНЕНИЕ ПРО «ВСЕ» — ПОДСКАЗКОЙ, А НЕ ВТОРОЙ ЛИНИЕЙ (макет 04.09,
        п. 10). Оно занимало вторую линию в каждой открытой всем строке и
        объясняло слово, которое само себя объясняет. По тому же правилу
        свёрнутая строка уже прячет пояснения состояния канала.

        Читалке с экрана фраза остаётся ТЕКСТОМ: `title` произносят не все и не
        всегда, а пояснение здесь — единственное место, где сказано, что
        «все» значит «канал открыт», а не «канал не настроен».
      */}
      <Text
        span
        fz="sm"
        c="var(--lc-text-2)"
        className="account-card__operators-label"
        title={count === 0 ? ВСЕ_ОПЕРАТОРЫ : undefined}
      >
        Операторы: <b>{count === 0 ? "все" : count}</b>
      </Text>
      {count === 0 && <VisuallyHidden data-sr-note>{ВСЕ_ОПЕРАТОРЫ}</VisuallyHidden>}
      {preview.length > 0 && (
        <>
          <span className="account-card__operators-faces" aria-hidden="true">
            {preview.map((u) => (
              // Подсказка над аватаркой — с отделом (04.09): инициалы «О.К.»
              // сами по себе не различают двух Ольг из разных отделов.
              // Само изображение остаётся по имени: инициалы отдела не берут.
              <Tooltip key={u.id} label={подписьСотрудника(u)}>
                <span>
                  <UserAvatar name={u.full_name} size={24} />
                </span>
              </Tooltip>
            ))}
          </span>
          {hidden > 0 && (
            <Text span fz="xs" c="var(--lc-text-3)" className="account-card__operators-more">
              {`+${hidden}`}
            </Text>
          )}
          {/* Аватарки — инициалы и тултип по наведению: и то, и другое мимо
              скринридера. Имена дублируем текстом, иначе состав канала
              доступен только глазами. */}
          <VisuallyHidden>
            {`Назначены: ${preview.map((u) => подписьСотрудника(u)).join(", ")}`}
            {hidden > 0 ? ` и ещё ${hidden}` : ""}
          </VisuallyHidden>
        </>
      )}
    </>
  );

  /* Право `accounts:manage` — только админ; руководитель видит состав, но
     нажимать ему не на что (11 §4.1). Обычный текст об этом и говорит: без
     подсветки под курсором и без цели нажатия. */
  if (!can("accounts:manage")) {
    return <div className="account-card__operators">{состав}</div>;
  }

  return (
    // aria-label с названием канала: карточек на экране девять, и девять
    // безымянных целей подряд с точки зрения скринридера неразличимы.
    <button
      type="button"
      className="account-card__operators"
      title="Нажмите, чтобы назначить операторов"
      aria-label={`Назначить операторов на аккаунт ${account.title}`}
      onClick={onAssign}
    >
      {состав}
    </button>
  );
}

function AccountCard({ account }: { account: AvitoAccountDto }) {
  const [assigning, setAssigning] = useState(false);
  const [правимИмя, setПравимИмя] = useState(false);
  /** Escape закрыл поле — уход фокуса после него сохранять не должен. */
  const отмененоИмя = useRef(false);
  const [draftTitle, setDraftTitle] = useState(account.title);
  /*
   * СПИСКОМ, А НЕ ПЛИТКАМИ (решение владельца 27.08, со скриншотом таблицы).
   *
   * Плитки в сетке по 320 пикселей заставляли пролистывать девять экранов,
   * чтобы сравнить каналы между собой: у каждой карточки своя высота, и
   * одноимённые поля стоят на разной высоте у соседей. Сравнивать так нельзя —
   * а именно это с аккаунтами и делают: где источник не проставлен, у кого
   * токен просрочен, кто вообще молчит.
   *
   * Поэтому строка: название, статус, id, партнёр и источник в одном ряду и
   * на одной высоте у всех. Всё остальное — неделя, токены, вебхуки, история,
   * действия — раскрывается по нажатию. Ничего не удалено: то, что было в
   * карточке, лежит под строкой и открывается там, где понадобилось.
   */
  const [развёрнут, setРазвёрнут] = useState(false);
  const { can } = usePermissions();
  const reconnect = useMutation({
    mutationFn: () => http.post<ConnectUrlResponse>(`/avito-accounts/${account.id}/reconnect`),
    onSuccess: ({ url }) => {
      window.location.href = url; // тот же OAuth-флоу, callback обновит токены (01 §4.4)
    },
    onError: (err) => {
      showToast(describeError({ where: "Начать переподключение", error: err, fallback: "Попробуйте ещё раз" }));
    },
  });

  /*
   * «Обновить токен» — не то же, что «Переподключить» (блок 8.3).
   *
   * Переподключение уводит в Авито: вход под учёткой канала и подтверждение
   * доступа — минуты и чужой пароль под рукой. Обновление токена не требует
   * ни того, ни другого: у нас есть refresh_token, и всё занимает секунду.
   * Разница важна ровно тогда, когда канал внезапно замолчал.
   *
   * Плановое обновление делает планировщик за два часа до истечения. Кнопка
   * нужна, когда ждать нельзя.
   */
  /*
   * Четыре действия над каналом, которых до сих пор не было ни одного.
   *
   * Сервер умел всё это с самого начала, но кнопок не было, и любое из них
   * требовало разработчика с доступом к серверу. Именно поэтому подключать
   * каналы самостоятельно было нельзя: ошибиться можно за секунду, а
   * исправить — только через меня.
   */
  const disable = useMutation({
    mutationFn: () => http.post<AvitoAccountDto>(`/avito-accounts/${account.id}/disable`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({
        title: "Аккаунт отключён",
        message: "Приём остановлен, переписка удалена",
        color: "lp",
      });
    },
    onError: (err) =>
        showToast(describeError({ where: "Отключить", error: err, fallback: "Попробуйте ещё раз" })),
  });

  const startBackfill = useMutation({
    mutationFn: () => http.post(`/avito-accounts/${account.id}/backfill`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({
        title: "Загрузка истории запущена",
        message: "Диалоги будут появляться по мере загрузки — страницу можно не обновлять",
        color: "lp",
      });
    },
    onError: (err) =>
        showToast(describeError({ where: "Запустить", error: err, fallback: "Попробуйте ещё раз" })),
  });

  const enable = useMutation({
    mutationFn: () => http.post<AvitoAccountDto>(`/avito-accounts/${account.id}/enable`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({ title: "Аккаунт включён", color: "lp" });
    },
    onError: (e) =>
      showToast({
        title: "Не получилось включить",
        // Причину называет сервер по исходу обновления (проверка 24.09): на
        // своих ключах «нужно переподключение» было неправдой — Авито просто
        // не ответил, и помогал повтор через минуту.
        message: e instanceof ApiError && e.message ? e.message : "Попробуйте ещё раз",
        color: "red",
      }),
  });

  const registerWebhook = useMutation({
    mutationFn: () => http.post<AvitoAccountDto>(`/avito-accounts/${account.id}/register-webhook`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({ title: "Подписка на входящие обновлена", color: "lp" });
    },
    onError: (e) => {
      const reason = e instanceof ApiError ? e.details?.reason : undefined;
      showToast({
        title: "Авито не принял подписку",
        message:
          reason === "not_active"
            ? "Сначала включите аккаунт"
            : "Попробуйте ещё раз через минуту",
        color: "red",
      });
    },
  });

  /*
   * Переименование канала. Имя приходит из Авито как есть — «! Парт - 7 /
   * Ист - В43 МНЧ !» — и в списках, фильтрах и статистике читается плохо.
   * У Jivo этого нет вовсе, и заказчик живёт с такими строками; повторять
   * чужое отсутствие функции незачем. На стороне Авито ничего не меняется.
   */
  // ⚠ ПАРТНЁР И ИСТОЧНИК — ЗДЕСЬ, А НЕ ТОЛЬКО В «АВТОЗАЯВКАХ» (просьба
  // владельца 19.08). Поля жили в отдельном разделе, до которого не доходят
  // руки, и владелец вписывал их прямо в НАЗВАНИЕ канала — «ПАРТ 7 ИСТ С78».
  // Название для этого не предназначено: оно же в списках, отчётах и
  // уведомлениях. Правим там, где человек и так управляет каналом.
  const [партнёр, setПартнёр] = useState(account.lead_partner_number ?? "");
  // ⚠ «ИСТОЧНИК» — ЭТО lead_origin (код вроде «В95»), А НЕ ЛИД-ЦЕНТР.
  // Первая редакция писала его в lead_src_key, где живёт выбор bt/kp/mnc, и
  // сервер честно отвечал «Неизвестный лид-центр». Два разных поля с похожими
  // именами: лид-центр решает, В КАКУЮ базу уедет заявка, источник — какой код
  // в ней проставить.
  const [источник, setИсточник] = useState(account.lead_origin ?? "");
  const сохранитьПоле = useMutation({
    mutationFn: (поле: { lead_partner_number?: string; lead_origin?: string }) =>
      request<AvitoAccountDto>(`/avito-accounts/${account.id}`, {
        method: "PATCH",
        body: поле,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({ title: "Сохранено", color: "lp" });
    },
    onError: (err, поле) => {
      /*
       * ⚠ ОТКАТ ОБЯЗАТЕЛЕН: ПОЛЕ НЕ ДОЛЖНО ПОКАЗЫВАТЬ НЕСОХРАНЁННОЕ.
       *
       * Значение живёт в локальном состоянии, а на сервер уходит по уходу
       * курсора. При отказе всплывало «Не сохранилось», но в поле оставалось
       * набранное — и дальше оно выглядело как сохранённое: тост уехал, а
       * «В95» на экране стоит. Человек уходит с настройки, заявки уезжают со
       * старым кодом, и узнают об этом в лид-центре, а не здесь. Возвращаем
       * то, что действительно лежит на сервере.
       */
      if (поле.lead_partner_number !== undefined) setПартнёр(account.lead_partner_number ?? "");
      if (поле.lead_origin !== undefined) setИсточник(account.lead_origin ?? "");
      showToast(
        describeError({ where: "Не сохранилось", error: err, fallback: "Попробуйте ещё раз" }),
      );
    },
  });

  const rename = useMutation({
    mutationFn: (title: string) =>
      request<AvitoAccountDto>(`/avito-accounts/${account.id}`, {
        method: "PATCH",
        body: { title },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({ title: "Аккаунт переименован", color: "lp" });
    },
    onError: (err) =>
        showToast(describeError({ where: "Не переименовалось", error: err, fallback: "Попробуйте ещё раз" })),
  });

  const remove = useMutation({
    mutationFn: () => request<void>(`/avito-accounts/${account.id}`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({ title: "Аккаунт удалён", color: "lp" });
    },
    onError: (e) => {
      // Текст ошибки берём у сервера, а не сочиняем свой: причин отказа
      // осталось немного (канал уже удалён кем-то другим, база недоступна),
      // и все они формулируются там. Прежний комментарий обещал здесь отказ
      // «в канале есть диалоги» — такого отказа нет с 8 августа.
      showToast({
        title: "Аккаунт не удалён",
        message: e instanceof ApiError ? e.message : "Попробуйте ещё раз",
        color: "red",
      });
    },
  });

  /**
   * Подтверждение необратимого действия, называющее РАЗМЕР потери.
   *
   * Оба действия внизу карточки стирают переписку, и оба спрашивали «все
   * диалоги и сообщения будут удалены» — формулировка, за которой одинаково
   * прячутся пустая ошибочно подключённая строка и год работы живого канала.
   * Число берём с сервера перед показом окна; не ответил — так и говорим, а
   * не подставляем ноль (см. describeHistorySize).
   *
   * Числа спрашиваются на КАЖДОЕ нажатие и не кэшируются: между двумя
   * подходами к кнопке канал успевает наработать ещё переписки, а показать
   * устаревшее число в подтверждении необратимого действия — хуже, чем
   * подождать лишние полсекунды.
   */
  const confirmWipe = async (question: string, tail: string, run: () => void) => {
    let size: ChannelHistorySize | null = null;
    try {
      size = await fetchChannelHistorySize(account.id);
    } catch {
      size = null;
    }
    if (window.confirm(`${question}\n\n${describeHistorySize(size)} Отменить нельзя.\n\n${tail}`)) {
      run();
    }
  };

  const refreshToken = useMutation({
    mutationFn: () => http.post<AvitoAccountDto>(`/avito-accounts/${account.id}/refresh-token`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({ title: "Токен обновлён", color: "lp" });
    },
    onError: (e) =>
      showToast({
        title: "Не получилось обновить",
        // Слова сервера: он различает «уже обновляется», «Авито не ответил» и
        // «доступ отозван / ключи не приняты» (проверка 24.09).
        message: e instanceof ApiError && e.message ? e.message : "Попробуйте ещё раз",
        color: "red",
      }),
  });

  const tone: DotTone =
    account.status === "active" ? "online" : account.status === "needs_reauth" ? "danger" : "muted";
  // Общий тип ответа знает о загрузке меньше, чем присылает сервер (см.
  // BackfillProgress выше) — сужаем здесь, в единственном месте показа.
  const backfill = account.backfill as BackfillProgress | null | undefined;
  const backfillRunning = backfill?.status === "running";

  /*
   * СЛУЖЕБНАЯ ЗАГЛУШКА — НЕ КАНАЛ, И КАРТОЧКА У НЕЁ СВОЯ.
   *
   * `SMOKE-ACCOUNT` заводит `app/cli.py seed-smoke` ради одного: у служебного
   * диалога `SMOKE-CONV` поле `conversations.account_id` объявлено NOT NULL.
   * Токена Авито у заглушки нет вовсе — в поле лежит строка-заполнитель, а
   * срок действия проставлен на десять лет вперёд.
   *
   * И ровно поэтому обычная карточка показывала про неё одну неправду за
   * другой: зелёная точка «работает», «Токен: активен» (срок-то в 2036 году) и
   * красное «Webhook: сбой» — след неизбежно провалившейся попытки подписаться
   * строкой вместо токена. Владелец увидел этот набор и справедливо решил, что
   * канал сломан. Чинить там нечего: заглушка работает как задумана.
   *
   * Поэтому здесь ранний выход: одна честная строка вместо шести полей,
   * которые к заглушке не относятся. Кнопок тоже нет — включать, обновлять
   * токен и подписываться сервер ей теперь отказывает
   * (`_assert_not_a_stub`, app/api/routes/avito_connect.py), и рисовать
   * кнопку, которая обязана отказать, значит звать нажать её.
   *
   * СКРЫТЬ СОВСЕМ БЫЛО БЫ ХУЖЕ: строка в списке существует, её видно в базе и
   * в отчётах, и «канал, о котором интерфейс молчит» — это следующий вопрос
   * без ответа.
   */
  if (account.is_service) {
    return (
      <article
        className="account-card"
        data-status="service"
        aria-label={`Служебная заглушка ${account.title}`}
      >
        <header className="account-card__header">
          <Text fw={600} fz="md" c="var(--lc-text-2)" truncate>
            {подписьКанала(account)}
          </Text>
          <StatusDot tone="muted" size={10} label="Статус: служебная заглушка" />
        </header>
        <Text fz="sm" c="var(--lc-text-3)">
          id {account.avito_user_id}
        </Text>
        <Text fz="sm" c="var(--lc-text-2)">
          Служебная заглушка проверочного набора, а не канал Авито. Токена у неё
          нет, обращения через неё не идут и идти не могут. Тревог она не
          поднимает, включать и удалять её не нужно.
        </Text>
      </article>
    );
  }

  return (
    <article
      className="account-card"
      data-status={account.status}
      data-open={развёрнут || undefined}
      aria-label={`Аккаунт ${account.title}`}
    >
      <header className="account-card__header">
        {/* Кнопка — на всю шапку: цель нажатия должна быть крупной, а не
            треугольником в шесть пикселей. Строка при этом остаётся строкой,
            а не превращается в ряд отдельных кнопок. */}
        {/*
          ⚠ РАСКРЫТИЕ И ИМЯ — РАЗНЫЕ ЦЕЛИ НАЖАТИЯ (просьба владельца 04.09:
          «мне не нравится, что есть лишняя кнопка для переименования», и до
          того — «сделай по аналогии с именем клиента»).

          Имя клиента правится нажатием НА САМО ИМЯ: ни карандаша, ни ссылки
          «изменить» рядом (ClientNameField.tsx, решение владельца 28.08). Здесь
          сначала появилась кнопка-карандаш — то есть ровно то, от чего в
          карточке клиента отказались. Теперь так же: имя это кнопка правки, а
          раскрытие переехало на треугольник.

          ⚠ ТРЕУГОЛЬНИК ОБЯЗАН БЫТЬ КРУПНОЙ ЦЕЛЬЮ. Прежняя кнопка занимала всю
          шапку именно поэтому — «цель нажатия должна быть крупной, а не
          треугольником в шесть пикселей». Значок остался шестью пикселями,
          кнопка вокруг него — 28 на 28 (`.account-row__toggle`), то есть
          крупнее пальца на телефоне.
        */}
        <button
          type="button"
          className="account-row__toggle"
          aria-expanded={развёрнут}
          aria-label={развёрнут ? `Свернуть ${account.title}` : `Развернуть ${account.title}`}
          onClick={() => setРазвёрнут((v) => !v)}
        >
          <span className="account-row__chevron" data-open={развёрнут || undefined} aria-hidden="true">
            <IconChevronRight size={14} />
          </span>
        </button>
        {/*
          ⚠ ТОЧКА СОСТОЯНИЯ — МЕЖДУ ТРЕУГОЛЬНИКОМ И ИМЕНЕМ (макет 04.09, п. 7).

          Она стояла ПОСЛЕ имени, то есть у каждой строки на своём иксе: имена
          разной длины, и тридцать пять состояний приходилось искать глазами по
          концам строк. Перед именем все точки встают на одну вертикаль, и
          «какой канал молчит» читается одним движением сверху вниз — ради
          этого вопроса экран и открывают.
        */}
        <StatusDot tone={tone} size={10} label={`Статус: ${STATUS_LABEL[account.status]}`} />
        {can("accounts:manage") && !правимИмя ? (
          <button
            type="button"
            className="account-row__name"
            title={`${account.title} — нажмите, чтобы переименовать`}
            aria-label={`Переименовать ${account.title}`}
            onClick={() => {
              setDraftTitle(account.title);
              setПравимИмя(true);
            }}
          >
            <Text fw={600} fz="md" c="var(--lc-text-1)" truncate>
              {подписьКанала(account)}
            </Text>
          </button>
        ) : правимИмя ? null : (
          // Без права правки имя остаётся обычным текстом: подсветка под
          // курсором обещала бы действие, которого не будет.
          //
          // ⚠ ПОДСКАЗКА ОБЯЗАТЕЛЬНА ИМЕННО ЗДЕСЬ. У кнопки правки полное имя
          // лежит в `title` («… — нажмите, чтобы переименовать»), а у этого
          // текста не лежало нигде: руководитель видел «Бригада Андрея Вл…» и
          // прочесть остаток не мог ничем.
          <Text fw={600} fz="md" c="var(--lc-text-1)" title={подписьКанала(account)} truncate>
            {подписьКанала(account)}
          </Text>
        )}
        {/*
          ⚠ ИМЯ ПРАВИТСЯ НА МЕСТЕ, КАК ИМЯ КЛИЕНТА (просьба владельца 04.09:
          «убери кнопку переименовать и сделай по аналогии с именем клиента»).

          Кнопка «Переименовать» занимала место в каждой из тридцати пяти строк
          ради действия, которое совершают раз в месяц, — и вместе с меню
          переносилась на вторую линию, отчего список и «ехал». Правка имени
          это одно короткое поле: Enter сохраняет, уход из поля сохраняет,
          Escape отменяет — те же три жеста, что у имени клиента, и искать
          кнопку глазами не нужно.
        */}
        {правимИмя && (
          <TextInput
            className="account-row__rename-field"
            size="xs"
            autoFocus
            value={draftTitle}
            maxLength={80}
            aria-label="Название аккаунта"
            onChange={(e) => setDraftTitle(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                (e.currentTarget as HTMLInputElement).blur();
              }
              if (e.key === "Escape") {
                e.preventDefault();
                отмененоИмя.current = true;
                setПравимИмя(false);
              }
            }}
            onBlur={() => {
              if (отмененоИмя.current) {
                отмененоИмя.current = false;
                return;
              }
              const v = draftTitle.trim();
              setПравимИмя(false);
              // Пустое или прежнее — молча закрываем: тост на «ничего не
              // изменилось» приучает не читать тосты.
              if (v && v !== account.title) rename.mutate(v);
            }}
          />
        )}
        {/*
          Вторая линия под именем вместо двух полей ввода в ряду. Пометка о
          незаданном источнике — это та самая работа, ради которой поля стояли
          в строке: по списку видно, у какого канала он не проставлен.
        */}
        <Text fz="xs" c="var(--lc-text-3)" className="account-row__id">
          id {account.avito_user_id}
          {!account.lead_origin && " · источник не задан"}
          {!account.lead_partner_number && " · партнёр не задан"}
        </Text>
      </header>

      {/*
        ⚠ ПРАВИТЬ ЭТИ ПОЛЯ МОЖЕТ ТОЛЬКО АДМИНИСТРАТОР.
        Ручка `PATCH /avito-accounts/{id}` требует `accounts:manage`, а это
        право есть лишь у него: у руководителя — `accounts:read`. Раньше поля
        рисовались всем, кто видит страницу, и у руководителя набранное молча
        улетало в 403 — экран предлагал работу, которую заведомо не примут.
        Смотреть значения ему нужно (по ним видно, у какого канала не проставлен
        источник), поэтому не прячем, а показываем как есть — текстом.
      */}
      {/*
        ⚠ ПОЛЯ ПРАВКИ УШЛИ ИЗ СТРОКИ (просьба владельца 04.09: «сделай так же,
        как было на Jivo, сейчас всё криво и косо»).

        В свёрнутом ряду стояли два поля ввода с СНЯТЫМИ подписями и шириной в
        восемь знаков — два безымянных окошка посреди строки. Работа, ради
        которой они там были («видно, у какого канала не проставлен источник»),
        никуда не делась: она стоит пометкой под названием канала, а править
        значения по-прежнему можно — здесь, под «Настроить».
      */}
      {развёрнут &&
      can("accounts:manage") ? (
        <Group gap="xs" wrap="nowrap" className="acc-lead-fields">
          <TextInput
            className="lc-field"
            size="xs"
            label="Партнёр"
            placeholder="7"
            value={партнёр}
            onChange={(e) => setПартнёр(e.currentTarget.value)}
            onBlur={() => {
              const v = партнёр.trim();
              if (v !== (account.lead_partner_number ?? "")) {
                сохранитьПоле.mutate({ lead_partner_number: v });
              }
            }}
          />
          <TextInput
            className="lc-field"
            size="xs"
            label="Источник"
            placeholder="В95"
            value={источник}
            onChange={(e) => setИсточник(e.currentTarget.value)}
            onBlur={() => {
              const v = источник.trim();
              if (v !== (account.lead_origin ?? "")) {
                сохранитьПоле.mutate({ lead_origin: v });
              }
            }}
          />
        </Group>
      ) : развёрнут ? (
        <Group gap="lg" wrap="nowrap" className="acc-lead-fields">
          <Text fz="xs" c="var(--lc-text-3)">
            Партнёр: {account.lead_partner_number || "—"}
          </Text>
          <Text fz="xs" c="var(--lc-text-3)">
            Источник: {account.lead_origin || "—"}
          </Text>
        </Group>
      ) : null}

      {/* Неделя канала — СРАЗУ под названием, до токенов и вебхуков. Порядок
          отвечает на порядок вопросов: сначала «что этот канал мне приносит»,
          и только если ответ плохой — «почему», а это уже техника ниже.

          Сводки может не быть (свежая база, не прошла миграция статистики) —
          тогда блока просто нет и карточка выглядит как раньше. Полезная
          мелочь не должна уметь ломать рабочий экран. */}
      {/*
        ⚠ НЕДЕЛЯ И ЛЮДИ — В САМОЙ СТРОКЕ, А НЕ ПОД РАСКРЫТИЕМ (просьба
        владельца 04.09: «сделай так же, как было на Jivo, сейчас всё криво и
        косо»).

        Оба блока были написаны давно и приезжают в первом же ответе списка —
        их держал один флаг `развёрнут`. Ровно из-за него строка отвечала на
        вопросы «какой токен» и «что с вебхуком», но не отвечала на два
        главных: кто здесь работает и что канал приносит. В Jivo это первое,
        что видно, и владелец показал ту строку как эталон.
      */}
      {account.stats && <WeekBars stats={account.stats} />}

      {/* Состав операторов показываем при любом статусе: отключённый канал
          когда-нибудь включат, и назначения к тому моменту должны быть на месте. */}
      <OperatorsRow account={account} onAssign={() => setAssigning(true)} />

      {assigning && (
        <Suspense fallback={null}>
          <AssignOperatorsModal account={account} onClose={() => setAssigning(false)} />
        </Suspense>
      )}


      {account.status === "active" && (
        <>
          {/* ⚠ СТРОКИ СОСТОЯНИЯ НЕ ПРЯЧУТСЯ ЗА РАСКРЫТИЕМ. Первая редакция
              списка убрала под него всё, кроме названия, — и сломанный канал
              стало видно, только раскрыв его. Ровно ради этого вопроса экран и
              открывают: «какой канал молчит». Прячем отчётное (неделя,
              операторы, история загрузки), сигнальное оставляем в строке.

              ОБЕ СТРОКИ СОСТОЯНИЯ — ВЫВОД СЕРВЕРА, а не расчёт по одной дате;
              разбор и цена прежнего расчёта — в шапке `ChannelHealth.tsx`.
              Кнопка появляется в них ТОЛЬКО когда сервер назвал действие: в
              штатном состоянии «Обновить токен» живёт в меню «…» ниже. */}
          <TokenLine
            account={account}
            onRefresh={() => refreshToken.mutate()}
            busy={refreshToken.isPending}
            canManage={can("accounts:manage")}
          />
          <WebhookLine
            account={account}
            onRewebhook={() => registerWebhook.mutate()}
            busy={registerWebhook.isPending}
            canManage={can("accounts:manage")}
          />
          {развёрнут && backfillRunning && backfill && <BackfillLine state={backfill} />}
          {/*
            ОСТАНОВЛЕННАЯ ЗАГРУЗКА — НЕ СОРВАВШАЯСЯ И НЕ ИДУЩАЯ.
            Останов делает человек, и отвечать ему после этого «Загружаем
            историю…» нельзя: он решит, что кнопка не сработала, и нажмёт ещё
            раз. Называем числа: по ним видно, что продолжение пойдёт дальше,
            а не начнёт заново.
          */}
          {развёрнут && backfill?.status === "stopped" && (
            <Group gap="xs" align="center">
              <Text fz="xs" c="var(--lc-text-3)" role="status">
                Загрузка истории остановлена: {progressLabel(backfill)} диалогов.
              </Text>
              {can("accounts:manage") && (
                <Button
                  size="compact-xs"
                  variant="default"
                  className="lc-btn"
                  onClick={() => startBackfill.mutate()}
                  loading={startBackfill.isPending}
                >
                  Продолжить с необработанных
                </Button>
              )}
            </Group>
          )}
          {/*
            СОРВАВШАЯСЯ ЗАГРУЗКА ГОВОРИТ ОБ ЭТОМ ВСЛУХ.

            Раньше состояний было два: «нет ключа» и «идёт». Но ключ остаётся и
            когда задача упала, и карточка СЕМЬ СУТОК показывала «Загружаем
            историю…» у загрузки, которой давно нет. Человек ждал — вместо того
            чтобы нажать «Повторить».

            Число загруженного называем и здесь: оно объясняет, почему повтор
            не начнёт с нуля, и снимает страх «сейчас всё задвоится».

            ⚠ ТЕКСТ ССЫЛАЛСЯ НА КНОПКУ, КОТОРОЙ НЕТ (найдено 13.08 при разборе
            янтарных абзацев; в самом разборе этого пункта не было).

            Стояло «повтор продолжит с этого места», и комментарий выше говорит
            «вместо того чтобы нажать «Повторить»». Кнопки «Повторить» в карточке
            нет: грепом по features/settings/accounts её нет вовсе, а на сервере
            `enqueue_backfill` зовётся ровно из одного места — app/cli.py. HTTP-ручки
            для перезапуска не существует, сама не перезапускается тоже: задача
            одноразовая, упавшая остаётся упавшей.

            То есть человек читал обещание действия, которого не мог совершить, и
            искал кнопку, которой нет, — ровно та беда, ради которой это сообщение и
            заводили («ждал вместо того, чтобы нажать»).

            Обещание убрано, важное сохранено: «ничего не задвоится» — это и есть
            ответ на страх, ради которого называется число. Кто перезапускает,
            сказано прямо.

            Правильное решение — ручка и кнопка, но это не правка текста: heavy-job
            по кнопке требует и прав, и защиты от повторного запуска. Вынесено
            отдельным вопросом, а не залатано здесь.
          */}
          {развёрнут && backfill?.status === "failed" && (
            <Group gap="xs" align="center">
              <Text fz="xs" c="var(--lc-warning-text)" role="status">
                Загрузка истории сорвалась на {progressLabel(backfill)} диалогов.
                Уже загруженное на месте, ничего не задвоится.
              </Text>
              {can("accounts:manage") && (
                <Button
                  size="compact-xs"
                  variant="default"
                  className="lc-btn"
                  onClick={() => startBackfill.mutate()}
                  loading={startBackfill.isPending}
                >
                  Повторить
                </Button>
              )}
            </Group>
          )}
          {развёрнут && (!backfill || backfill.status === "idle") && (
            /* каналы, подключённые до возврата загрузки истории (17.08):
               Jivo-эпоха лежит в Авито и одним нажатием приезжает сюда.
               ⚠ сервер при отсутствии загрузки отдаёт {status:"idle"}, а не
               null — первое условие «!backfill» кнопку не показывало никогда */
            can("accounts:manage") && (
              <Button
                size="compact-xs"
                variant="default"
                className="lc-btn"
                onClick={() => startBackfill.mutate()}
                loading={startBackfill.isPending}
              >
                Загрузить историю переписки
              </Button>
            )
          )}
          {развёрнут && (
            <Text fz="xs" c="var(--lc-text-3)">
              Подключён {formatDate(account.created_at)}
            </Text>
          )}
        </>
      )}

      {/*
        ⚠ СЛОМАННЫЙ КАНАЛ НЕ ЛОМАЕТ СТРОКУ СПИСКА (04.09). Весь этот блок —
        два абзаца, состояние подписки и кнопка во всю ширину — рисовался БЕЗ
        оглядки на раскрытие, то есть вставал прямо в горизонтальный ряд. Один
        сломанный канал перекашивал геометрию всего списка: соседние строки
        переставали совпадать по колонкам, и колонки, ради которых список и
        делали, исчезали.
        В свёрнутой строке о беде говорит красная точка статуса и подпись
        состояния; подробности и кнопка — здесь, под «Настроить».
      */}
      {развёрнут && account.status === "needs_reauth" && (
        <>
          <Text fz="sm" fw={600} c="var(--lc-danger-text)">
            {account.own_keys ? "Не удалось получить токен" : "Требует переподключения"}
          </Text>
          {/* ЧТО ИМЕННО СЛОМАЛОСЬ — ЧЕСТНО, ПО СПОСОБУ ПОДКЛЮЧЕНИЯ.
              Прежний текст говорил «токен отозван, приём сообщений остановлен»
              и был неверен дважды. Приём НЕ останавливается: вебхуки Авито
              нашего токена не требуют, обращения приходят и видны — не уходят
              только ответы. А «отозван» для канала на своих ключах вообще не
              бывает: ключи выдаются один раз и не меняются. */}
          <Text fz="sm" c="var(--lc-text-2)">
            {account.own_keys
              ? "Обращения приходят, но ответы не уходят. Ключи постоянные — чаще всего помогает повтор."
              : "Авито отозвал доступ. Обращения приходят, но ответы не уходят."}
          </Text>
          {/* Состояние подписки — ЗДЕСЬ ЖЕ, до кнопки (дефект аудита 13). Обе
              беды канала независимы: доступ отозван и подписка сорвана — это
              разные починки, и знать про вторую надо ДО того, как обрадуешься
              успеху первой. */}
          <WebhookLine
            account={account}
            onRewebhook={() => registerWebhook.mutate()}
            busy={registerWebhook.isPending}
            canManage={can("accounts:manage")}
          />
          <Button
            fullWidth
            mt="var(--lc-space-2)"
            loading={account.own_keys ? refreshToken.isPending : reconnect.isPending}
            /*
              ЗНАЧОК — В `leftSection`, И ЗАЗОР ЗАДАН ЯВНО (дефект аудита 15).
              Он стоял первым символом подписи: «🔄 Переподключить». Замер на
              стенде: между глифом и «П» ровно 3,48 px — ширина обычного пробела
              при кегле 13. Рядом с плотным цветным квадратом это читается как
              слипшееся, и правка «положить в leftSection» сама по себе беде не
              помогает: Mantine у этого размера кнопки даёт 4 px, то есть
              полпикселя разницы. Поэтому 8 px названы здесь вслух.

              Второй смысл переезда — доступность: значок помечен aria-hidden,
              и скринридер перестал читать «стрелки по кругу Переподключить».
            */
            leftSection={account.own_keys ? undefined : <IconRefresh size={16} />}
            styles={{ section: { marginInlineEnd: "var(--lc-space-2)" } }}
            onClick={() => (account.own_keys ? refreshToken.mutate() : reconnect.mutate())}
          >
            {account.own_keys ? "Повторить сейчас" : "Переподключить"}
          </Button>
          {account.own_keys && (
            <Text fz="xs" c="var(--lc-text-3)" mt="var(--lc-space-1)">
              Не помогло — проверьте в кабинете Авито, не удалено ли и не отключено ли приложение.
            </Text>
          )}
        </>
      )}

      {account.status === "disabled" && (
        <>
          {/* Подсказка — не украшение: в свёрнутой строке фраза обрезается
              многоточием, чтобы ряд не стал выше соседних, и прочесть её
              целиком можно только по наведению либо раскрыв строку. */}
          <Text
            fz="sm"
            c="var(--lc-text-3)"
            className="account-card__disabled-note"
            title={ОТКЛЮЧЁН_ВРУЧНУЮ}
          >
            {ОТКЛЮЧЁН_ВРУЧНУЮ}
          </Text>
          {/* Единственная новость, ради которой у выключенного канала вообще
              рисуется строка приёма: «подписку снять не удалось». Сама
              WebhookLine и решает, молчать ей или нет, — см. её шапку. */}
          <WebhookLine
            account={account}
            onRewebhook={() => registerWebhook.mutate()}
            busy={registerWebhook.isPending}
            canManage={can("accounts:manage")}
          />
        </>
      )}

      {/* Действия внизу карточки и отделены линией: выше — состояние канала,
          ниже — что с ним можно сделать. Смешивать одно с другим значит
          заставлять искать кнопку среди строк отчёта.

          Право `accounts:manage` — только администратор: руководитель видит
          состояние канала, но не управляет им (11 §4.1). */}
      {can("accounts:manage") && (
        <div className="account-card__actions">
          {account.status === "disabled" && (
            <Button
              variant="default"
              size="compact-xs"
              loading={enable.isPending}
              onClick={() => enable.mutate()}
            >
              Включить
            </Button>
          )}

          {/*
            ОБСЛУЖИВАНИЕ И РАЗРУШЕНИЕ — В ОДНОМ МЕНЮ «…», НО ЗА ЧЕРТОЙ.

            История места в два шага. Сначала «Обновить токен» и «Обновить
            подписку» уехали в меню: они стояли под вечно жёлтой строкой
            токена, и люди жали их каждый день, приняв штатное сообщение за
            аварию (разбор в ChannelHealth.tsx). Наверх обе поднимаются РОВНО
            ТОГДА, когда сервер сказал, что действие нужно.

            15 августа за ними уехали «Отключить и стереть» и «Удалить» — по
            п. 22 отчёта тестирования. Два необратимых действия стояли
            ОБЫЧНЫМИ КНОПКАМИ в ряду с «Переименовать»: ряд читается слева
            направо как ряд одинаково безобидных, а колонка карточки ужимается
            до 280px — и на ней четыре контрола переносились на вторую строку,
            ставя «Удалить» ровно туда, куда падает взгляд. Курок, до которого
            один клик, не должен лежать на столе.

            Черта между группами — не косметика: выше неё то, что чинит канал,
            ниже — то, что его останавливает или стирает. `data-danger` красит
            пункты в тот же красный, которым в карточке написано «Требует
            переподключения»; подтверждения с числом переписки остались как
            были (`confirmWipe`).
          */}
          <Menu position="bottom-end" withArrow>
            <Menu.Target>
              <ActionIcon
                variant="subtle"
                size="lg"
                c="var(--lc-text-2)"
                // Карточек на экране девять, и девять безымянных «…» подряд
                // с точки зрения скринридера неразличимы.
                aria-label={`Обслуживание канала ${account.title}`}
              >
                <IconMore size={18} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              {account.status === "active" && (
                <>
                  <Menu.Item
                    disabled={refreshToken.isPending}
                    onClick={() => refreshToken.mutate()}
                  >
                    Обновить токен
                  </Menu.Item>
                  <Menu.Item
                    disabled={registerWebhook.isPending}
                    onClick={() => registerWebhook.mutate()}
                  >
                    Обновить подписку
                  </Menu.Item>
                  <Menu.Divider />
                  <Menu.Item
                    data-danger
                    disabled={disable.isPending}
                    onClick={() => {
                      // Подтверждение здесь не формальность: это единственное
                      // действие, которое останавливает приём обращений от
                      // клиентов, и с 8 августа оно НЕОБРАТИМО для переписки.
                      // Последняя строка — про отличие от соседнего «Удалить»:
                      // два необратимых действия с одинаковым исходом для
                      // переписки, и разница («канал остаётся» / «канала не
                      // будет») обязана быть написана в момент решения.
                      void confirmWipe(
                        `Отключить аккаунт «${account.title}» и стереть его переписку?`,
                        "Приём новых обращений остановится, подписка на стороне Авито снимется.\n" +
                          "Сам канал останется в списке отключённым — включить его можно одним нажатием, " +
                          "но переписку это не вернёт.",
                        () => disable.mutate(),
                      );
                    }}
                  >
                    Отключить и стереть
                  </Menu.Item>
                </>
              )}
              {/* «Удалить» — убрать канал из списка совсем. Доступно при любом
                  состоянии: ошибочно подключённую строку удаляют, не включая.
                  Отличие от «Отключить и стереть» — только в судьбе самого
                  канала, и подтверждение обязано его назвать: там строка
                  остаётся и включается нажатием, здесь её не будет, и вернуть
                  канал можно только заново пройдя согласие в Авито. */}
              <Menu.Item
                data-danger
                disabled={remove.isPending}
                onClick={() => {
                  void confirmWipe(
                    `Удалить аккаунт «${account.title}» из системы?`,
                    "Канал исчезнет из списка совсем: чтобы вернуть его, придётся заново подключать через Авито.\n" +
                      "Если нужно только остановить приём — есть «Отключить и стереть», там канал остаётся.",
                    () => remove.mutate(),
                  );
                }}
              >
                Удалить
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </div>
      )}
    </article>
  );
}

/**
 * /settings/accounts (11 §4.1): карточки аккаунтов Авито, статусы
 * 🟢 active / 🔴 needs_reauth (CTA «Переподключить») / ⚪ disabled,
 * webhook-статус, прогресс backfill (поллинг 5 с, пока идёт), OAuth-подключение
 * через GET /avito/connect-url; возврат callback'а — тосты по query (01 §4.3).
 */
export function AccountsPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  // Возврат с avito.ru: ?connected=1 / ?error=… → тост, query чистим (11 §4.1).
  useEffect(() => {
    const connected = searchParams.get("connected");
    const error = searchParams.get("error");
    if (!connected && !error) return;
    if (connected === "1") {
      showToast({
        title: "Аккаунт подключён",
        message: "Загружаем историю чатов…",
        color: "lp",
      });
    } else if (error === "account_mismatch") {
      showToast({
        title: "Переподключение отменено",
        message: "Вы авторизовали другой аккаунт Авито",
        color: "red",
        autoClose: false,
      });
    } else if (error) {
      showToast({
        title: "Не получилось: подключение аккаунта",
        // код ошибки приходит от Авито в адресе возврата — называем его: по
        // нему видно, отказал ли пользователь в правах или упала площадка
        message: `Авито вернул отказ: ${error}. Попробуйте подключить заново`,
        color: "red",
        autoClose: false,
      });
    }
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  const q = useQuery({
    queryKey: qk.accounts,
    queryFn: fetchAvitoAccounts,
    staleTime: 5 * 60_000, // 03 §2.4
    // Пока идёт backfill — поллинг каждые 5 с, прогресс живой.
    refetchInterval: (query) =>
      query.state.data?.items.some((a) => a.backfill?.status === "running") ? 5000 : false,
  });

  const items = q.data?.items ?? [];
  const { can } = usePermissions();
  const [wizard, setWizard] = useState(false);
  const appState = useQuery({
    queryKey: ["avito-app"],
    queryFn: () => http.get<{ live: boolean }>("/avito/app"),
    enabled: can("accounts:manage"),
  });

  return (
    <div className="lc-page accounts-page">
      <PageHeader
        title="Аккаунты Авито"
        description="Каналы, обращения которых приходят в «Чаты»"
        actions={
          /*
            ОДНА кнопка, и она открывает мастер, а не уводит сразу на Авито.
            Раньше рядом стояла панель ключей, и заказчик справедливо спросил,
            что из этого подключение. Теперь ключи живут внутри мастера.

            Гаснет ТОЛЬКО в ветке успешной пустоты: там ту же кнопку предлагает
            пустое состояние, и две подряд выглядят как ошибка. Простое
            «список пуст» было бы неверным — при ошибке загрузки список тоже
            пуст, но пустое состояние предлагает лишь «Повторить», и экран
            остался бы вообще без входа в мастер. Во время скелетонов кнопка на
            месте: иначе шапка перестраивается на глазах.
          */
          can("accounts:manage") && !(!q.isPending && !q.isError && items.length === 0) ? (
            <Button onClick={() => setWizard(true)}>Подключить аккаунт</Button>
          ) : undefined
        }
      />

      {/* ГЛАВНОЕ ПРЕДУПРЕЖДЕНИЕ ЭКРАНА, и оно сверху.
          Пока система смотрит на встроенный имитатор, всё выглядит рабочим:
          аккаунты подключаются, карточки зелёные, в сводке числа. Только
          обращений не будет никогда — а понять это по экрану было нельзя. */}
      {appState.data && !appState.data.live && (
        <div className="accounts-page__notice" role="status">
          <div>
            <b>Это не настоящий Авито.</b> Система работает на встроенном имитаторе: он примет
            любые ключи и покажет выдуманный аккаунт. Настоящие обращения приходить не будут.
          </div>
          {/*
            ⚠ АКЦЕНТ НА ЭКРАНЕ ОДИН, И ЭТО «Подключить аккаунт».
            Здесь стояла вторая сплошная зелёная кнопка, и две одинаково
            громких цели в верхних двухстах пикселях спорили между собой:
            зелёный переставал означать «главное действие страницы» и начинал
            означать просто «кнопка». Сигнал этой плашки несёт янтарная рамка и
            жирное «Это не настоящий Авито» — кнопке внутри неё кричать не
            нужно, её и так читают следом за фразой.
          */}
          <Button variant="default" size="compact-sm" onClick={() => setWizard(true)}>
            Переключить
          </Button>
        </div>
      )}

      {q.isPending ? (
        <div className="accounts-page__grid" aria-hidden="true">
          <div className="account-card account-card--skeleton" />
          <div className="account-card account-card--skeleton" />
        </div>
      ) : q.isError ? (
        <EmptyState
          illustration="error"
          title="Не получилось загрузить"
          action={
            <Button variant="outline" size="xs" onClick={() => void q.refetch()}>
              Повторить
            </Button>
          }
        />
      ) : items.length === 0 ? (
        <EmptyState
          illustration="plug"
          title="Подключите первый аккаунт Авито"
          /* Обещания «через минуту» здесь больше нет: первая загрузка истории
             по тысячам диалогов минутой не измеряется, а обещание, которое
             система не держит, читается как поломка. */
          description={
            can("accounts:manage")
              ? "Нужны Client ID и Client Secret из кабинета разработчика того аккаунта — и обращения по всем его объявлениям пойдут в «Чаты»"
              : "Аккаунты подключает администратор"
          }
          action={
            can("accounts:manage") ? (
              <Button onClick={() => setWizard(true)}>Подключить аккаунт</Button>
            ) : undefined
          }
        />
      ) : (
        <div className="accounts-page__grid">
          {/*
            ПОДПИСИ КОЛОНОК (макет 04.09, п. 9). Колонок шесть, и ни одна не
            была названа: «что это за число» выяснялось наведением, а список
            читают всю смену. Шестая — обслуживание, у неё подписи нет и на
            макете: значок «…» называет себя сам.

            `aria-hidden` не оговорка, а следствие: читалка с экрана получает
            каждое значение уже названным («Операторы: 5», «Токен активен»,
            «Статус: работает»), и пять слов без табличной разметки добавили бы
            ей шум, а не смысл.
          */}
          <div className="accounts-page__head" aria-hidden="true">
            <span>Канал</span>
            <span>Неделя</span>
            <span>Операторы</span>
            <span>Токен</span>
            <span>Приём событий</span>
          </div>
          {items.map((a) => (
            <AccountCard key={a.id} account={a} />
          ))}
          {q.isFetching && (
            <div className="accounts-page__refreshing" aria-hidden="true">
              <Loader size="xs" color="lp" />
            </div>
          )}
        </div>
      )}

      {/* Что система делает с телефонами в тексте входящих — тут же, под
          каналами, по которым эти сообщения и приходят. Спрашивают об этом
          именно так: «откуда в карточке взялся телефон». Право `settings:manage`
          — администратор; руководителю блок не показывается вовсе, потому что
          сервер ему всё равно откажет. */}
      {can("settings:manage") && <PhoneDetectBlock />}
      {can("settings:manage") && <AddressDetectBlock />}

      {/* Служебные записи Авито — соседний вопрос про то же самое: что система
          делает с приходящим по каналу. Требование владельца от 14 августа
          («в самой Jivo их нет»). Право то же, и по той же причине. */}
      {can("settings:manage") && <ThreadBlock />}

      <ConnectChannelWizard opened={wizard} onClose={() => setWizard(false)} />
    </div>
  );
}
