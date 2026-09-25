import type { NavigateFunction } from "react-router-dom";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import {
  requestClaim,
  requestClose,
  requestDecline,
  requestTransfer,
  requestTransferDecision,
} from "./actionBus";
import {
  requestComposerFocus,
  requestHotkeysHelp,
  requestSearchFocus,
} from "./focusBus";
import {
  ACTIONS,
  firesWhileTyping,
  действующие,
  type ActionId,
  type HotkeyAction,
} from "./catalog";
import { bindingOf, type Binding } from "./binding";
import { markKeyboardStep } from "@/features/chats/keyboardStepping";
import { выбралСам } from "@/features/chats/выборДиалога";
import { предзагрузитьДиалог } from "@/features/chats/предзагрузка";
import {
  догрузитьСтраницу,
  шагПоВидимому,
} from "@/features/chats/списокНаЭкране";

/**
 * РАЗБОР НАЖАТИЯ ПО ТАБЛИЦЕ — вместо лестницы `if`, где порядок веток и был приоритетом.
 *
 * ЧЕМ ПЛОХА БЫЛА ЛЕСТНИЦА. Три вещи, и все три чинились по одной:
 *
 *   1) сочетание нигде не существовало как значение — переназначать нечего;
 *   2) «работает ли при наборе» решалось ПОЗИЦИЕЙ ветки относительно проверки
 *      `typing`. Ctrl+Backspace однажды оказался выше неё, и диспетчер, стиравший
 *      слово в ответе клиенту, ОТКЛОНЯЛ диалог;
 *   3) русская раскладка перечислялась руками и неполно: у Ctrl+Shift+N не было
 *      «т», у Ctrl+K только строчная «л». Теперь разбор идёт по `e.code`, и
 *      кириллицу перечислять не нужно вовсе.
 *
 * ⚠ ПОРЯДОК ПОИСКА ВСЁ ЕЩЁ ЗНАЧИМ, НО ТЕПЕРЬ ОН ЯВНЫЙ. Одно сочетание может стоять
 * у двух действий (человек назначил); побеждает то, что выше в реестре. Это видно в
 * таблице, а не выводится из формы кода.
 */

/**
 * ГДЕ ЖИВЁТ КАЖДОЕ ДЕЙСТВИЕ. Сегодня это определялось точкой монтирования хука:
 * `useGlobalHotkeys` висит в раскладке приложения, `useChatHotkeys` — на экране чатов.
 * Область была не записана нигде и выводилась из того, какой файл где вызван.
 */
export const GLOBAL_ACTIONS = new Set<ActionId>(["search", "help"]);

export interface Bindings {
  /** Личные переназначения: действие → сочетания. Чего нет — берётся из реестра. */
  readonly [id: string]: readonly Binding[];
}

/** Какое действие вызвано этим нажатием — или ничего. */
export function actionFor(
  e: KeyboardEvent,
  typing: boolean,
  bindings: Bindings = {},
): HotkeyAction | null {
  const нажато = bindingOf(e);
  if (!нажато) return null;
  for (const a of ACTIONS) {
    const свои = действующие(a, bindings);
    if (!свои.includes(нажато)) continue;
    if (typing && !firesWhileTyping(a, нажато)) continue;
    return a;
  }
  return null;
}

/** Что делает каждое действие. Возвращает false, если делать было нечего. */
export interface RunContext {
  navigate: NavigateFunction;
  /** Строки списка в порядке отрисовки — из кэша той выдачи, что на экране. */
  rows: () => Array<{ id: string; unread_count: number }>;
  /** Проверка права: без него действие молчит (шпаргалка его и не покажет). */
  can: (p: string) => boolean;
}

import { inboxRows, nextInboxId } from "@/features/chats/inbox/api";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import {
  claimFromQueue,
  взятьПервогоССервера,
} from "@/features/chats/inbox/claimFromQueue";

export function runAction(id: ActionId, ctx: RunContext): boolean {
  const store = useChatUiStore.getState();
  switch (id) {
    case "claim": {
      /*
       * ПРИЁМ РАБОТАЕТ ИЗ ЛЮБОЙ ВКЛАДКИ — решение владельца, подтверждённое
       * дважды (28.08 «чтобы диалоги можно было брать без клика мыши» и 30.08
       * «диалог должен браться через комбинацию клавиш в любой вкладке»).
       *
       * Порядок разбора:
       *  1. Открытый диалог сам ждёт решения — принимаем ЕГО. Признак тот же,
       *     что у панели решения (useIsQueued): слово сервера `in_inbox` на
       *     детали, затем состав очереди из стора. Кэш REST-выдачи `/inbox`
       *     здесь НЕ спрашивается: его может не быть вовсе, и ровно эта
       *     зависимость 30.08 превращала Ctrl+R в перезагрузку страницы.
       *  2. Открытый уже взят, поле пустое — берём ПЕРВОГО ждущего и уходим к
       *     нему. Утром этот переход был закрыт совсем (разбор жалобы «чат сам
       *     переключается»), но владелец решение переподтвердил — а настоящая
       *     беда того разбора закрывается пунктом 3.
       *  3. НЕДОПИСАННЫЙ ОТВЕТ ДЕРЖИТ. Единственный замок: прыжок посреди
       *     набора и был тем «сообщения уходят не тем людям». Дописал или
       *     стёр — клавиша снова берёт следующего.
       */
      const активный = store.activeConversationId;
      if (активный) {
        const деталь = queryClient.getQueryData<{
          in_inbox?: boolean;
          assignee?: { id: string } | null;
          transfer?: { to: { id: string } } | null;
        }>(qk.conversations.detail(активный));
        // Открытый диалог передают мне — «Принять диалог» принимает передачу, а
        // не первого из очереди (им часто оказывался чужой клиент).
        const я = useSessionStore.getState().user?.id;
        if (я && деталь?.transfer?.to.id === я) {
          requestTransferDecision(активный, "accept");
          return true;
        }
        // Слово сервера `in_inbox` перевешивает всё; без него — состав очереди
        // из стора, но не для диалога, который уже мой по `assignee`: клавиша,
        // нажатая второй раз по привычке (замер 15.09), давала 409 «вы уже
        // приняли» на каждое нажатие. Только `assignee`: `claimed_by` — история
        // «кто вынул из очереди», возвраты в очередь его не чистят (ревью 15.09).
        const мой = Boolean(я) && деталь?.assignee?.id === я;
        const вОчереди =
          typeof деталь?.in_inbox === "boolean"
            ? деталь.in_inbox
            : !мой && Boolean(useInboxStore.getState().ids[активный]);
        if (вОчереди) {
          requestClaim();
          return true;
        }
        if ((store.drafts[активный]?.text ?? "").trim()) return true;
      }
      // Строка предложения передачи во «Входящих» — не очередь: взять её
      // «из очереди» нельзя, сервер ответит 409.
      const первый = inboxRows().find((row) => row.in_inbox !== false)?.id;
      if (первый) {
        claimFromQueue(первый, ctx.navigate);
        return true;
      }
      // Кэша выдачи /inbox нет — вкладку «Входящие» не открывали. Спрашиваем
      // очередь у сервера сами: просьба владельца — принимать «не заходя во
      // вкладку входящие», и до 30.08 ровно здесь она обрывалась.
      void взятьПервогоССервера(ctx.navigate);
      return true;
    }
    case "close":
      if (!store.activeConversationId) return false;
      // Адрес просьбы — тот диалог, что открыт СЕЙЧАС. Без него нажатие,
      // попавшее в промежуток между двумя диалогами, доставалось бы не тому.
      requestClose(store.activeConversationId);
      return true;
    case "transfer":
      if (!store.activeConversationId) return false;
      requestTransfer();
      return true;
    case "decline":
      if (!store.activeConversationId) return false;
      requestDecline();
      return true;
    case "listNext":
      return шагПоСписку(ctx, true, false);
    case "listPrev":
      return шагПоСписку(ctx, false, false);
    case "unreadNext":
      return шагПоСписку(ctx, true, true);
    case "unreadPrev":
      return шагПоСписку(ctx, false, true);
    case "search":
      if (!window.location.pathname.startsWith("/chats"))
        ctx.navigate("/chats");
      requestSearchFocus();
      return true;
    case "inbox":
      store.setInboxOpen(true);
      return true;
    case "queueNext": {
      // Следующий по очереди — тот, кто ждёт дольше всех из оставшихся
      // (`offered_at ASC` на сервере, кольцевой обход в `nextInboxId`).
      // Открытого диалога нет — открываем первый ждущий, а не молчим:
      // «взять следующий» осмысленно и с пустого экрана.
      const текущий = store.activeConversationId ?? "";
      const следующий = nextInboxId(текущий);
      if (!следующий) return false;
      выбралСам(следующий);
      ctx.navigate(`/chats/${следующий}`);
      return true;
    }
    case "tabMine":
      store.setFilters({ tab: "mine", status: undefined });
      return true;
    case "tabAll":
      store.setFilters({ tab: "all", status: undefined });
      return true;
    case "statusNew":
      store.setFilters({ tab: "all", status: "new" });
      return true;
    case "statusClosed":
      store.setFilters({ tab: "all", status: "closed" });
      return true;
    case "focusComposer":
      if (!store.activeConversationId) return false;
      requestComposerFocus();
      return true;
    case "noteMode": {
      const convId = store.activeConversationId;
      if (!convId) return false;
      const current = store.drafts[convId]?.isNote ?? false;
      store.patchDraft(convId, { isNote: !current });
      requestComposerFocus();
      return true;
    }
    case "help":
      requestHotkeysHelp();
      return true;
    case "escape":
      return луковица();
  }
}

/**
 * Esc закрывает ПО ОДНОМУ СЛОЮ ЗА НАЖАТИЕ — карточку, потом заметку, потом поиск.
 *
 * Меню и модальные окна гасят событие раньше, до нас оно не доходит.
 */
function луковица(): boolean {
  const store = useChatUiStore.getState();
  if (store.clientCardOpen) {
    store.setClientCardOpen(false);
    return true;
  }
  const convId = store.activeConversationId;
  if (convId && store.drafts[convId]?.isNote) {
    store.patchDraft(convId, { isNote: false });
    return true;
  }
  if (store.filters.q) {
    store.setFilters({ q: undefined });
    return true;
  }
  return false;
}

/**
 * Шаг по списку.
 *
 * ⚠ ПО НЕПРОЧИТАННЫМ ХОДИМ ЦИКЛИЧЕСКИ, ПО ВСЕМУ СПИСКУ — С ЗАЖИМОМ НА КРАЯХ. Так
 * было и до таблицы, и это не случайность: «следующий непрочитанный» — обход всех
 * ждущих ответа, и упереться в конец там значит бросить хвост; обычное же движение
 * по списку с переносом на первую строку читалось бы как сбой.
 *
 * ⚠ ОТКРЫТЫЙ ДИАЛОГ, КОТОРОГО НЕТ В СПИСКЕ, — ЭТО НЕ «НАЧНИ СНАЧАЛА». Так выглядит
 * приход из «Разбора диалогов»: там свои фильтры, и открытый оттуда диалог в левую
 * колонку часто не входит. Прыжок на первую строку законен ровно когда не открыто
 * ничего.
 */
/**
 * Уход к соседу тем же порядком, что и щелчок по строке списка.
 *
 * `выбралСам` обязателен: без отметки замок «с недописанного ответа уводит
 * только человек» откатит переход назад и обвинит в консоли неизвестного
 * виновника.
 *
 * `предзагрузитьДиалог` спрашивает СЛЕДУЮЩЕГО в ту же сторону: у клавиатуры нет
 * форы наведения, которую мышь даёт сама (разбор — ниже, у обхода
 * непрочитанных). Спрашиваем ровно одну строку вперёд.
 */
function уйтиКлавишей(
  ctx: RunContext,
  id: string,
  следом: string | null,
): void {
  markKeyboardStep();
  выбралСам(id);
  ctx.navigate(`/chats/${id}`);
  предзагрузитьДиалог(следом ?? "");
}

/*
 * ⚠ «КТО СЛЕДУЮЩИЙ» СЧИТАЕТСЯ ОДИН РАЗ НА ВЕСЬ ЭКРАН (09.09).
 *
 * ЧТО БЫЛО. Ответов было два. Кнопки-шевроны в шапке ленты звали
 * `шагПоВидимому` из `chats/списокНаЭкране`, а клавиши считали шаг тут же
 * своей арифметикой — и расходились в двух местах:
 *   * НА КРАЮ СПИСКА. `Math.min(at + 1, rows.length - 1)` на последней строке
 *     даёт ту же строку: клавиша «уходила» в уже открытый диалог, возвращала
 *     true и ГАСИЛА нажатие. Человек жал вниз, ничего не происходило, и
 *     браузер тоже ничего не делал — клавишу мы съели;
 *   * НА ГРАНИЦЕ ЗАГРУЖЕННОГО. Кнопка догружала следующую страницу и шла
 *     дальше, клавиша упиралась. На широком экране это прикрывала колонка
 *     списка (она тянет страницу сама, когда до лоадера доезжает прокрутка),
 *     а ниже 768px колонки нет вовсе — там стена настоящая.
 *
 * Шевроны 09.09 убраны по просьбе владельца, и оба их умения обязаны были
 * переехать сюда, иначе правка отняла бы способность вместо того, чтобы
 * сменить ей вид.
 *
 * ⚠ ГОНКИ ДВУХ ДОГРУЗОК ЗДЕСЬ НЕТ, И ЭТО НЕ ДОГАДКА. `догрузитьСтраницу`
 * сверяет смещение ПЕРЕД записью и выбрасывает свою страницу, если колонка
 * успела раньше (разбор — в шапке самой функции). Замок внутри неё, а не у
 * вызывающего, поэтому второй вызывающий её не ломает.
 *
 * НЕПРОЧИТАННЫЕ ХОДЯТ СВОИМ ОБХОДОМ — кольцом и по подмножеству строк. Это
 * другой вопрос («обойти всех ждущих»), и упереться в конец там значит
 * бросить хвост, поэтому ветка осталась отдельной.
 */
function шагПоСписку(
  ctx: RunContext,
  forward: boolean,
  onlyUnread: boolean,
): boolean {
  const store = useChatUiStore.getState();
  if (onlyUnread) return шагПоНепрочитанным(ctx, forward);

  const открытый = store.activeConversationId;
  if (!открытый) {
    // Не открыто ничего — начать с первой строки законно, и только здесь.
    const rows = ctx.rows();
    if (rows.length === 0) return false;
    уйтиКлавишей(ctx, rows[0].id, rows[1]?.id ?? null);
    return true;
  }

  const шаг = шагПоВидимому(открытый, forward);
  if (шаг.куда) {
    уйтиКлавишей(ctx, шаг.куда, шаг.следом);
    return true;
  }
  /*
   * Идти некуда и список кончился: НЕ гасим нажатие. Клавиша, съеденная
   * впустую, отнимает у стрелки её обычную работу и не даёт человеку ни
   * одного признака края — у кнопки этот признак был, она гасла.
   */
  if (!шаг.догрузить) return false;
  void догрузитьСтраницу()
    .then(() => {
      const после = шагПоВидимому(открытый, true);
      if (после.куда) уйтиКлавишей(ctx, после.куда, после.следом);
    })
    /*
     * ⚠ БЕЗ ЭТОГО `catch` СОРВАВШАЯСЯ СТРАНИЦА — НЕОБРАБОТАННЫЙ ОТКАЗ ПРОМИСА.
     * Прежний вызывающий (кнопка) его тоже не имел: связь моргнула — и в
     * консоли красное, а человек не знает, почему остался на месте. Молчать
     * тут нечем, но и падать не за что: остаёмся на месте, повод — в журнал.
     */
    .catch(() => {
      console.warn("[чат] листание: следующая страница списка не приехала");
    });
  return true;
}

/**
 * Кольцевой обход по непрочитанным — своё правило, см. разбор выше.
 *
 * ⚠ МЕСТО ОТКРЫТОГО ДИАЛОГА ИЩЕТСЯ ВО ВСЁМ СПИСКЕ, А НЕ СРЕДИ НЕПРОЧИТАННЫХ
 * (проверка 24.09). Открытие само отмечает диалог прочитанным, и его счётчик
 * в списке тут же становится нулём. Прежний поиск «где я среди непрочитанных»
 * после первого же шага не находил открытого и молчал: Alt+↓ срабатывал один
 * раз, дальше обход стоял.
 */
function шагПоНепрочитанным(ctx: RunContext, forward: boolean): boolean {
  const active = useChatUiStore.getState().activeConversationId;
  const rows = ctx.rows();
  const at = active ? rows.findIndex((r) => r.id === active) : -1;
  // Открытый диалог вне списка (пришли из «Разбора») — не уводим, как и Ctrl+↓.
  if (at === -1 && active) return false;
  const ring = at === -1 ? rows : ringAfter(rows, at, forward);
  const unread = ring.filter((r) => r.unread_count > 0 && r.id !== active);
  if (unread.length === 0) return false;
  markKeyboardStep();
  выбралСам(unread[0].id);
  ctx.navigate(`/chats/${unread[0].id}`);
  /*
   * ⚠ И СРАЗУ СПРАШИВАЕМ СЛЕДУЮЩИЙ В ТУ ЖЕ СТОРОНУ (просьба владельца 05.09
   * про «моментальную подгрузку»).
   *
   * Мышь даёт нам фору сама: курсор стоит на строке за 250–400 мс до нажатия,
   * и предзагрузка по наведению живёт этим временем. У клавиатуры такой форы
   * НЕТ — `listNext` открывает диалог в тот же миг, и каждое нажатие стрелки
   * стоит полного круга до сервера (замер боя: деталь 55 мс, лента 42 мс в
   * среднем). А разбор списка стрелками — это как раз серия нажатий подряд, то
   * есть серия ожиданий.
   *
   * Форой становится сам шаг: раз человек пошёл вниз, следующим он с большой
   * вероятностью нажмёт вниз ещё раз. Спрашиваем РОВНО ОДНУ строку вперёд, а не
   * окно из нескольких: цена ошибки прогноза — два лишних запроса, и умножать
   * её на ширину окна незачем.
   */
  предзагрузитьДиалог(unread[1]?.id ?? "");
  return true;
}

/** Строки по кругу после `at` (без неё самой) в сторону шага. */
function ringAfter<T>(rows: readonly T[], at: number, forward: boolean): T[] {
  const n = rows.length;
  return Array.from({ length: n - 1 }, (_, i) => rows[(at + (forward ? i + 1 : -(i + 1)) + n) % n]);
}
