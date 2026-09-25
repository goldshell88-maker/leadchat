import { useEffect, useState } from "react";
import { ActionIcon, Alert, Button, Checkbox, Group, Menu, Modal, Select, Stack, Switch, Text, TextInput } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import { qk, type TeamFilters } from "@/shared/api/queryKeys";
import type { InviteIssued, TeamUserDto } from "@/shared/api/types";
import type { Role } from "@/shared/auth/usePermissions";
import { useInlineSize } from "@/shared/lib/useInlineSize";
import { useSessionStore } from "@/shared/stores/sessionStore";
import {
  TEAM_PAGE_SIZE,
  activateUser,
  deactivateUser,
  deleteUser,
  fetchTeamUsers,
  issueUserLink,
  setUserPassword,
  updateUser,
} from "./api";
import { InviteModal } from "./InviteModal";
import { OneTimeLinkModal } from "./OneTimeLinkModal";
import { Pager } from "./Pager";
import { canAnswer, ROLE_HINTS, ROLE_LABELS, ROLE_OPTIONS, ROLE_ORDER } from "./roles";
import { IconChevronDown, IconPlus, IconSearch } from "@/shared/ui/Icon";
import { UserAvatar } from "@/shared/ui/UserAvatar";
import { StatusDot, type DotTone } from "@/shared/ui/StatusDot";
import { EmptyState } from "@/shared/ui/EmptyState";
import { IconMore } from "@/shared/ui/Icon";
import { showToast } from "@/shared/ui/toast";

/**
 * Вкладка «Сотрудники» (11 §4.2, данные — 01 §3). Право `users:manage` — только
 * admin: руководителю на этой странице остаётся журнал аудита.
 *
 * Отключение здесь — не «галочка в базе»: сервер кладёт refresh-токены в denylist
 * и `user.id` в `revoked_users`, WebSocket-хаб рвёт сессию (01 §3.5). Диалоги за
 * отключённым сохраняются — переназначает их руководитель, чтобы не терять контекст.
 */

type Pending =
  | { kind: "role"; user: TeamUserDto; role: Role }
  | { kind: "deactivate"; user: TeamUserDto }
  | { kind: "activate"; user: TeamUserDto }
  // Удаление стоит рядом с отключением намеренно: их легко перепутать, и
  // разницу должен объяснять один и тот же диалог, а не память человека.
  | { kind: "delete"; user: TeamUserDto }
  /** Сброс пароля / перевыпуск ссылки — тоже опасное действие (обход 31.08). */
  | { kind: "reissue"; user: TeamUserDto };

/**
 * Что показывать в колонке «Статус». Состояний три, и молчит колонка только
 * тогда, когда сервер не прислал признаков вовсе.
 *
 * ПОЧЕМУ КОЛОНКА ЕСТЬ, ХОТЯ РЯДОМ СТОИТ ЧЕКБОКС «ПОКАЗЫВАТЬ ОТКЛЮЧЁННЫХ».
 * Чекбокс управляет только одним состоянием — он решает, попадут отключённые в
 * выдачу или нет (`is_active=true` в запросе, api.ts). «Ждёт пароля» —
 * сотрудника пригласили, а ссылку он так и не открыл — чекбоксом не выражается
 * ни при каком его положении, и это ровно тот случай, ради которого колонку и
 * открывают: администратор ищет, кто из приглашённых не завёлся.
 *
 * ПОЧЕМУ ШТАТНОЕ СОСТОЯНИЕ СНОВА НАЗЫВАЕТСЯ СЛОВОМ, ХОТЯ ЕГО УБИРАЛИ НАРОЧНО.
 * Убирали с рассуждением: «активен» тринадцать раз подряд — столбец одинаковых
 * слов, в котором нечего искать. Рассуждение верное, а вывод из него оказался
 * неверным, и это видно на живых данных. У заказчика все сотрудники активны и с
 * паролями — то есть колонка «Статус» пуста ВСЕГДА, при любом положении
 * галочки. Владелец так её и описал: заголовок есть, под ним пусто у всех.
 *
 * Пустая ячейка не читается как «всё в порядке». Она читается как «данные не
 * доехали» — ровно так же, как выглядела бы настоящая поломка выдачи, и
 * отличить одно от другого на экране нечем. Цена ошибки несимметрична:
 * «работает» лишний раз прочитать дёшево, а не заметить, что колонка сломалась,
 * дорого — именно в ней ищут не завёдшихся приглашённых.
 *
 * Опасение «столбец одинаковых слов» снимается не молчанием, а весом: штатное
 * состояние написано самым тихим цветом (`--lc-text-3`), нештатные — заметными.
 * Глаз всё так же цепляется за исключение, но теперь видно и то, что колонка
 * жива.
 */
function statusLabel(u: TeamUserDto): string | null {
  // Признака нет вовсе (старый сервер, обрезанный ответ) — молчим. «Отключён»
  // на месте пропуска писать нельзя тем более: `!undefined` это true, и
  // человек, которого просто не описали, выглядел бы уволенным.
  const active: unknown = u.is_active;
  if (typeof active !== "boolean") return null;
  if (!active) return "отключён";
  if (u.invite_pending) return "ждёт пароля";
  return "работает";
}

/**
 * Имена полей запроса → подписи на экране. Нужно ровно для одного: назвать
 * человеку поле, которое сервер не принял. Ключи — имена из схемы
 * `app/schemas/users.py` (`UserPatchIn`, `SetPasswordIn`); незнакомое имя
 * просто не попадёт в текст, и человек увидит общую фразу вместо выдуманной.
 */
const FIELD_LABELS: Record<string, string> = {
  department: "Отдел",
  color: "Цвет",
  full_name: "Сотрудник",
  email: "Почта",
  role: "Роль",
  password: "Новый пароль",
};

/**
 * Длина отдела с запасом до серверного предела (`MAX_DEPARTMENT = 100`,
 * app/schemas/users.py:33). Дублирование числа осознанное: без него поле
 * молча принимает сто первый знак, а отказ приходит уже от сервера — общим
 * `400 validation_error` после того, как человек ушёл фокусом и забыл, что
 * печатал. Ограничение на вводе дешевле любого разбора ошибки.
 */
// 10 различимых цветов, работают на тёмном фоне (17.08)
const STAFF_COLORS = [
  "#22c55e", "#3b82f6", "#a855f7", "#f97316", "#eab308",
  "#14b8a6", "#ec4899", "#94a3b8", "#ef4444", "#8b5cf6",
] as const;

const MAX_DEPARTMENT = 100;

/**
 * ПОРОГ СПРАВКИ О РОЛЯХ — ОДНО ЧИСЛО НА CSS И НА РАЗМЕТКУ.
 *
 * От этой ширины и выше справка стоит СБОКУ от таблицы, в пустой трети
 * широкого экрана (расчёт порога — в team.css у `.team-members__layout`). Ниже
 * она оказывалась второй строкой сетки и забирала высоту у списка: при
 * 1440×900 из тринадцати сотрудников оставалось видно пять, при 1024 — два, на
 * телефоне — ни одного. Поэтому ниже порога та же справка рисуется свёрнутой:
 * заголовок-строка, содержимое по нажатию.
 *
 * ⚠ ЭТО ШИРИНА САМОЙ СЕТКИ, А НЕ ОКНА (правка 08.09). Раньше здесь стояло
 * 1560 и `useMediaQuery` по окну — та же ошибка, что и в CSS: рельса
 * приложения развёрнута по умолчанию (218px, `railStore`), содержимому
 * достаётся окно минус 486, и справка появлялась на 146 пикселей раньше, чем
 * под неё есть место. Оба слоя теперь спрашивают ОДНУ величину: CSS —
 * контейнерным запросом `@container team-members (min-width: 1220px)`, React —
 * замером той же сетки (`useInlineSize`). Расхождение ловит сторож в
 * `test/TeamAdaptive0809.test.tsx`.
 */
const ROLES_ASIDE_MIN_LAYOUT = 1220;

/**
 * Копия карты без указанного ключа. Когда ключа и не было, возвращается та же
 * карта — новая ссылка на равный объект заставила бы React перерисовать
 * таблицу на пустом месте.
 */
function withoutKey<T>(map: Record<string, T>, key: string): Record<string, T> {
  if (!(key in map)) return map;
  const next = { ...map };
  delete next[key];
  return next;
}

function presence(u: TeamUserDto): { tone: DotTone; label: string } {
  // У отключённого presence не бывает по определению (01 §3.5) — не дублируем статус.
  if (!u.is_active) return { tone: "muted", label: "—" };
  /*
   * «Отошёл» — ТРЕТЬЕ состояние, а не отсутствие второго (#34).
   *
   * Человек за столом: видит диалоги, отвечает, ему можно передать. Показать
   * его «не в сети» значило бы соврать — руководитель перестал бы ему
   * передавать. Не показать состояние вовсе — соврать иначе: он отдал бы
   * диалог обедающему и ждал ответа.
   *
   * Цвет не единственный носитель смысла: подпись стоит рядом всегда.
   */
  if (u.presence === "away") return { tone: "away" as const, label: "отошёл" };
  return u.is_online
    ? { tone: "online" as const, label: "в сети" }
    : { tone: "offline" as const, label: "не в сети" };
}

export function TeamMembersTab() {
  const qc = useQueryClient();
  const meId = useSessionStore((s) => s.user?.id);

  /*
   * Замер идёт в фазе фиксации, до кадра (см. `useInlineSize`), — иначе
   * справка успевает мигнуть развёрнутой и подвинуть список под курсором.
   *
   * `null` («ещё не мерили») читается как «широко»: там, где мерить нечем,
   * человек получает ПРЕЖНИЙ вид со справкой, а не свёрнутый. То же умолчание,
   * что стояло у прежнего `useMediaQuery`.
   */
  const [layoutRef, layoutWidth] = useInlineSize();
  /* Ноль — это не «очень узко», а «раскладки нет вовсе»: блок ещё не в
     документе, скрыт или мерян средой без раскладки (jsdom в сторожах).
     Считать его узким значило бы свернуть справку там, где о ширине ничего не
     известно. */
  const foldRoles = layoutWidth !== null && layoutWidth > 0 && layoutWidth < ROLES_ASIDE_MIN_LAYOUT;

  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [role, setRole] = useState<Role | null>(null);
  const [includeInactive, setIncludeInactive] = useState(false);
  const [offset, setOffset] = useState(0);

  const [inviteOpen, setInviteOpen] = useState(false);
  const [issued, setIssued] = useState<InviteIssued | null>(null);
  /** Кому задаём пароль голосом; null — окно закрыто. */
  const [passwordFor, setPasswordFor] = useState<TeamUserDto | null>(null);
  const [newPassword, setNewPassword] = useState("");
  // Правка карточки: имя и почта. Отдел, цвет, роль и «ведёт диалоги»
  // правятся прямо в строке — их сюда не дублируем, иначе одно и то же поле
  // редактируется в двух местах и расходится.
  const [editFor, setEditFor] = useState<TeamUserDto | null>(null);
  const [editName, setEditName] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Поиск попадает в ключ кэша только после паузы — иначе на каждую букву запрос.
  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebouncedSearch(search.trim());
      setOffset(0);
    }, 300);
    return () => window.clearTimeout(id);
  }, [search]);

  const filters: TeamFilters = {
    q: debouncedSearch || undefined,
    role: role ?? undefined,
    includeInactive,
    offset,
  };
  const q = useQuery({ queryKey: qk.users.list(filters), queryFn: () => fetchTeamUsers(filters) });

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.users.root });

  /*
   * ОШИБКУ НАЗЫВАЕТ СЕРВЕР, А НЕ ЭКРАН.
   *
   * Было: `err.details.reason === "last_admin" || err.status === 422` →
   * «Нельзя оставить систему без администратора» (TEAM-02). Второе условие
   * не попадало в цель НИ РАЗУ, и это проверено по коду, а не по названию:
   *
   * — настоящий «последний администратор» приходит с кодом 409, а не 422
   *   (`app/services/users.py:151-158`, `_assert_admin_remains`);
   * — а все 422 этого экрана — про другое, и у каждой свой точный текст:
   *   «Свой пароль меняют в профиле — с вводом текущего» (:487-491),
   *   «Пароль уже установлен — нужна не новая ссылка, а сброс пароля»
   *   (:279-283), «Нельзя …: сотрудник отключён» (:642-646).
   *
   * То есть ветка работала ровно наоборот задуманного: единственный случай,
   * ради которого её писали, в неё не попадал, а попадали только чужие — и
   * каждый из них терял свой текст. Самый злой пример: администратор задаёт
   * пароль себе, сервер честно отвечает «свой пароль меняют в профиле», а
   * экран печатает про администратора, которого нельзя оставить без.
   *
   * Теперь текст берётся из ответа: сервер уже пишет по-русски и точнее, чем
   * может угадать таблица. Свой текст оставлен только там, где серверного нет.
   */
  const describeError = (err: unknown): string => {
    if (!(err instanceof ApiError)) {
      return "Не получилось. Проверьте соединение и повторите действие";
    }
    /*
     * 400 validation_error: текст сервера уже русский и точный — правила
     * pydantic переведены (`app/core/errors.py`, `_RULE_MESSAGES`), а текст
     * валидатора поля («Некорректный адрес почты») отдаётся как есть. Здесь
     * стояла своя фраза «сервер не принял значение», и человек не узнавал,
     * ЧТО не так (проверка 24.09). Имя поля приставляем, когда оно известно.
     */
    if (err.code === "validation_error") {
      const fields = err.details?.["fields"];
      const first = Array.isArray(fields) ? (fields[0] as { field?: string } | undefined) : undefined;
      const label = first?.field ? FIELD_LABELS[first.field] : undefined;
      const reason = err.message || "сервер не принял значение";
      return label ? `Не сохранилось: «${label}» — ${reason}` : `Не сохранилось: ${reason}`;
    }
    return err.message || "Не получилось выполнить действие. Попробуйте ещё раз";
  };

  const roleChange = useMutation({
    mutationFn: (p: { id: string; role: Role }) => updateUser(p.id, { role: p.role }),
    onSuccess: () => {
      void invalidate();
      setPending(null);
    },
    onError: (err) => setActionError(describeError(err)),
  });

  /**
   * Отдел и участие в диалогах сохраняются сразу, без подтверждения.
   *
   * Смена роли спрашивает подтверждение, а эти — нет, и разница не в
   * небрежности: роль меняет ПРАВА (человек теряет доступ к разделам), а
   * здесь меняется только состав раздачи, и обратный щелчок возвращает всё
   * как было. Диалог подтверждения на каждый такой щелчок читался бы как
   * «система не уверена», а перенастройка отдела из тринадцати человек
   * превратилась бы в тринадцать модальных окон.
   *
   * `await invalidate()` — не украшение. Дальше по коду `mutate` вызывается со
   * своим `onSuccess`, который гасит местную подсветку строки; react-query
   * дожидается промиса ЭТОГО обработчика, прежде чем позвать тот. Без await
   * подсветка снималась бы до прихода свежего списка, и тумблер на полсекунды
   * отскакивал бы в старое положение — ровно то мигание, от которого здесь и
   * лечимся.
   */
  const save = useMutation({
    mutationFn: (p: {
      id: string;
      body: { handles_conversations?: boolean; department?: string; color?: string };
    }) => updateUser(p.id, p.body),
    onSuccess: async () => {
      await invalidate();
    },
    onError: (err) => setActionError(describeError(err)),
  });

  /*
   * ПОЧЕМУ У ОТДЕЛА И ТУМБЛЕРА ЕСТЬ МЕСТНОЕ СОСТОЯНИЕ (TEAM-03, TEAM-07).
   *
   * Обе колонки рисовались прямо из ответа сервера: `checked={u.handles_...}`
   * и `defaultValue={u.department}`. Отсюда две разные беды.
   *
   * Тумблер не двигался от щелчка. Он ждал ответа и перерисовки всего списка,
   * то есть на неспешной сети полсекунды-секунду оставался в прежнем
   * положении — при том что мишень нажата и палец уже убран. Читается это
   * однозначно: «не сработало», и человек щёлкает второй раз. Второй щелчок
   * шлёт обратное значение, и в раздаче остаётся не то, что хотели.
   *
   * Поле отдела было неуправляемым и сохранялось по уходу фокуса. Когда
   * сохранение не удавалось, ошибка всплывала наверху страницы, а в ячейке
   * оставалось НОВОЕ значение: на экране «СТАРШИЕ - ЧАТЫ», на сервере пусто.
   * Никакого признака расхождения, и правка исчезала при следующей загрузке.
   *
   * Отсюда две карты по id строки: чего человек хочет (показываем сразу) и
   * что сервер ещё не подтвердил (строка помечена как сохраняющаяся). Успех —
   * запись из карты уходит, значение приезжает уже из ответа. Неудача —
   * запись уходит тоже, и ячейка ВОЗВРАЩАЕТСЯ к серверному значению рядом с
   * объяснением: пусть лучше правка видимо откатится, чем тихо разойдётся.
   */
  const [draftDept, setDraftDept] = useState<Record<string, string>>({});
  const [draftAnswers, setDraftAnswers] = useState<Record<string, boolean>>({});
  const [savingIds, setSavingIds] = useState<Record<string, true>>({});

  const forget = (id: string) => {
    setDraftDept((m) => withoutKey(m, id));
    setDraftAnswers((m) => withoutKey(m, id));
    setSavingIds((m) => withoutKey(m, id));
  };

  const saveField = (
    id: string,
    body: { handles_conversations?: boolean; department?: string; color?: string },
    done?: () => void,
  ) => {
    setActionError(null);
    // ⚠ 18.08: одна мутация на всю таблицу — второй вызов подряд по ЛЮБОЙ
    // строке терял колбэки первого: строка оставалась заблокированной
    // навсегда, а неверное значение не откатывалось. Пока строка сохраняется,
    // второй запрос по ней не пускаем.
    if (savingIds[id]) return;
    setSavingIds((m) => ({ ...m, [id]: true }));
    /*
     * ⚠ ЗАМОК ПОМОГАЛ ТОЛЬКО ОДНОЙ СТРОКЕ (аудит 19.08). Он не пускает второй
     * запрос по ТОЙ ЖЕ строке — а колбэки терялись между РАЗНЫМИ: мутация одна
     * на всю таблицу, и обработчики, переданные в `mutate`, react-query зовёт
     * только у последнего вызова. Правишь отдел Иванову, через секунду цвет
     * Петрову — строка Иванова остаётся заблокированной навсегда, до
     * перезагрузки страницы.
     *
     * `mutateAsync` возвращает промис КОНКРЕТНОГО вызова: каждая строка ждёт
     * свой ответ и снимает свою блокировку сама. `finally` снимает её в любом
     * исходе — иначе ошибка сети снова оставила бы строку висеть.
     * Причину показывает `onError` самой мутации, здесь её глотать нечем.
     */
    save
      .mutateAsync({ id, body })
      .then(() => done?.())
      .catch(() => undefined)
      .finally(() => forget(id));
  };

  const deactivate = useMutation({
    mutationFn: (id: string) => deactivateUser(id),
    onSuccess: () => {
      void invalidate();
      setPending(null);
    },
    onError: (err) => setActionError(describeError(err)),
  });

  const activate = useMutation({
    mutationFn: (id: string) => activateUser(id),
    onSuccess: () => {
      void invalidate();
      setPending(null);
    },
    onError: (err) => setActionError(describeError(err)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: () => {
      void invalidate();
      setPending(null);
    },
    onError: (err) => setActionError(describeError(err)),
  });

  /*
   * «Задать пароль» — не то же, что «Сбросить пароль» (просьба заказчика).
   *
   * Сброс выдаёт одноразовую ссылку: её надо переслать, человек должен по ней
   * перейти и придумать пароль — три шага и ожидание. Здесь администратор
   * говорит пароль вслух, и сотрудник входит сразу. На тринадцать человек,
   * половина из которых на смене, разница между «сейчас» и «когда дойдут
   * руки» решает.
   */
  const setPassword = useMutation({
    mutationFn: (p: { id: string; password: string }) => setUserPassword(p.id, p.password),
    onSuccess: () => {
      void invalidate();
      setPasswordFor(null);
      setNewPassword("");
    },
    onError: (err) => setActionError(describeError(err)),
  });

  /*
   * Правка имени и почты одним запросом. Шлём ТОЛЬКО изменённые поля: PATCH с
   * неизменной почтой прошёл бы через проверку занятости впустую, а главное —
   * записал бы в журнал смену логина, которой не было.
   */
  const editUser = useMutation({
    mutationFn: (p: { id: string; body: { full_name?: string; email?: string } }) =>
      updateUser(p.id, p.body),
    onSuccess: () => {
      void invalidate();
      setEditFor(null);
      showToast({ message: "Карточка сохранена", color: "lp" });
    },
    onError: (err) => setActionError(describeError(err)),
  });

  const reissue = useMutation({
    mutationFn: (u: TeamUserDto) => issueUserLink(u),
    onSuccess: (result) => {
      void invalidate();
      // Окно подтверждения закрывается, как у соседних действий: оставшись под
      // ссылкой, оно звало нажать ещё раз, а второй сброс гасит первую ссылку.
      setPending(null);
      setIssued(result);
    },
    onError: (err) => setActionError(describeError(err)),
  });

  const confirmBusy =
    roleChange.isPending ||
    deactivate.isPending ||
    activate.isPending ||
    remove.isPending ||
    reissue.isPending;

  const runPending = () => {
    if (!pending) return;
    setActionError(null);
    if (pending.kind === "role") roleChange.mutate({ id: pending.user.id, role: pending.role });
    else if (pending.kind === "deactivate") deactivate.mutate(pending.user.id);
    else if (pending.kind === "delete") remove.mutate(pending.user.id);
    else if (pending.kind === "reissue") reissue.mutate(pending.user);
    else activate.mutate(pending.user.id);
  };

  const closePending = () => {
    setPending(null);
    setActionError(null);
  };

  // `page` читаем осторожно: страница списка приходит с сервера, и падать
  // разметкой из-за неполного ответа таблице сотрудников незачем.
  const shown = q.data?.items.length ?? 0;
  const total = q.data?.page?.total ?? shown;

  /*
   * ПУСТОТА НАЗЫВАЕТ ПРИЧИНУ ПОИМЁННО. «Никого не нашлось» одинаково верно и
   * для промаха в поиске, и для роли, по которой в команде никого нет, — а
   * действия это требует разного. Тринадцать человек на девять аккаунтов: если
   * отфильтровано по «Наблюдатель», а наблюдателей в команде нет, человек
   * должен видеть, что ищет несуществующее, а не переписывать запрос.
   */
  const filtering = Boolean(debouncedSearch) || role !== null;

  const emptyReason =
    debouncedSearch && role
      ? `По запросу «${debouncedSearch}» среди роли «${ROLE_LABELS[role]}» никого нет`
      : debouncedSearch
        ? `По запросу «${debouncedSearch}» никого нет`
        : role
          ? `Ни одного сотрудника с ролью «${ROLE_LABELS[role]}»`
          : "";

  const resetFilters = () => {
    setSearch("");
    setRole(null);
    setOffset(0);
  };

  /* Тело справки одно на оба вида: развёрнутый сбоку и свёрнутый под таблицей
     обязаны говорить дословно одно и то же — по нему решают, кому что открыть. */
  const rolesReference = (
    <div className="team-roles__body">
      {ROLE_ORDER.map((r) => (
        <div key={r} className="team-roles__item">
          <span className={`team-role team-role--${r}`}>{ROLE_LABELS[r]}</span>
          <p className="team-roles__hint">{ROLE_HINTS[r]}</p>
        </div>
      ))}
    </div>
  );

  return (
    <section className="team-members" aria-label="Сотрудники">
      <div className="team-members__toolbar">
        <TextInput
          size="xs"
          w={240}
          aria-label="Поиск сотрудника"
          placeholder="Имя или email"
          leftSection={<IconSearch size={16} />}
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
        />
        <Select
          size="xs"
          w={180}
          aria-label="Фильтр по роли"
          placeholder="Роль: любая"
          clearable
          data={ROLE_OPTIONS}
          value={role}
          onChange={(v) => {
            setRole((v as Role | null) ?? null);
            setOffset(0);
          }}
        />
        <Checkbox
          size="xs"
          label="Показывать отключённых"
          checked={includeInactive}
          onChange={(e) => {
            setIncludeInactive(e.currentTarget.checked);
            setOffset(0);
          }}
        />
        <Button
          size="xs"
          ml="auto"
          leftSection={<IconPlus size={14} />}
          onClick={() => setInviteOpen(true)}
        >
          Пригласить
        </Button>
      </div>

      {/*
        Плашка прячется, пока открыто ЛЮБОЕ окно с собственным местом под
        ошибку: подтверждение (`pending`), «Задать пароль» (`passwordFor`) и
        «Изменить сотрудника» (`editFor`, проверка 24.09). Про окна забывали, и
        одна и та же ошибка рисовалась дважды — в окне и страницей под ним. Для
        глаза это два разных отказа, а для программы чтения с экрана — два
        подряд `role="alert"` с одинаковым текстом.
      */}
      {actionError && !pending && passwordFor === null && editFor === null && (
        <Alert color="red" variant="light" role="alert" mb="var(--lc-space-2)">
          {actionError}
        </Alert>
      )}

      {q.isPending ? (
        <div className="audit__skeleton" aria-hidden="true">
          {Array.from({ length: 5 }, (_, i) => (
            <span key={i} className="audit__skeleton-line" />
          ))}
        </div>
      ) : q.isError ? (
        <EmptyState
          illustration="error"
          title="Не получилось загрузить список сотрудников"
          description="Нажмите «Повторить». Если не помогает — обновите страницу"
          live="alert"
          action={
            <Button size="xs" variant="outline" onClick={() => void q.refetch()}>
              Повторить
            </Button>
          }
        />
      ) : shown === 0 ? (
        /* Кнопки «Пригласить» здесь намеренно нет: тот же вызов виден в
           тулбаре всегда, а ветка «первый сотрудник» практически недостижима —
           в списке всегда есть хотя бы сам смотрящий.

           А вот СБРОС фильтров нужен: у отфильтрованной пустоты причина
           названа («Никого не нашлось» + чем именно отфильтровано), но снять
           фильтр было нечем — поиск и роль стоят выше и на пустом экране легко
           теряются из виду. То же правило, что на экране быстрых ответов. */
        <EmptyState
          illustration={filtering ? "search" : "chat"}
          title={filtering ? "Никого не нашлось" : "Пригласите первого сотрудника"}
          description={filtering ? emptyReason : undefined}
          action={
            filtering ? (
              <Button size="xs" variant="outline" onClick={resetFilters}>
                Сбросить фильтры
              </Button>
            ) : undefined
          }
        />
      ) : (
        <div className="team-members__layout" ref={layoutRef}>
          <div className="audit__scroll lc-scroll-x">
            {/* На узком экране строка становится карточкой (lc-table-cards.css):
                  семь столбцов в 400 пикселей не помещаются никак, а
                  горизонтальная прокрутка прячет половину — человек видит
                  «Сотрудник · Email» и решает, что больше ничего нет. */}
            <table className="lc-table audit__table team-members__table lc-table--cards">
              <thead>
                <tr>
                  {/*
                    Колонок стало семь вместо девяти. «Email» и «Цвет» не
                    удалены, а сложены в первую ячейку: почта переносилась
                    посреди слова, потому что таблица делила ширину на девять
                    столбцов, а колонка цвета отдавала 22 пикселя ячейке, где у
                    большинства стоял прочерк. Оба значения теперь стоят там,
                    где работают, — под именем и на кружке с инициалами.
                  */}
                  <th scope="col">Сотрудник</th>
                  <th scope="col">Роль</th>
                  {/*
                    Отдел и участие в диалогах — рядом с ролью, потому что это
                    ответ на тот же вопрос «кто этот человек в команде».
                    Раньше роль отвечала сразу и за права, и за участие в
                    раздаче; теперь это две колонки, и видно, что они разные.
                  */}
                  <th scope="col">Отдел</th>
                  <th scope="col">Ведёт диалоги</th>
                  <th scope="col">Статус</th>
                  <th scope="col">Онлайн</th>
                  <th scope="col">
                    <span className="lc-visually-hidden">Действия</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {q.data.items.map((u) => {
                  const p = presence(u);
                  const isMe = u.id === meId;
                  return (
                    <tr key={u.id} data-inactive={!u.is_active || undefined}>
                      <td data-label="Сотрудник">
                        <div className="team-person">
                          {/*
                            ЦВЕТ ЖИВЁТ НА АВАТАРЕ, А НЕ В СВОЁЙ КОЛОНКЕ (04.09).
                            Было: своя колонка из девяти, а в ней кружок, в
                            котором у большинства прочерк, — 22 пикселя ширины под
                            значение, которое чаще всего ничего не сообщает.
                            Стало: цвет показан кольцом вокруг кружка с
                            инициалами, то есть там, где он и работает, а
                            правится нажатием на сам кружок.

                            Инициалы держат пару «фон/чернила» из палитры
                            аватаров (§5 lc-vars, контраст посчитан заранее) —
                            тот же аватар, что в «Людях и каналах» и в профиле.
                            Красить их выбранным HEX нельзя: десять цветов
                            сотрудников подбирались как метки в списке, а не как
                            подложка под текст.
                          */}
                          <Menu position="bottom-start" withinPortal>
                            <Menu.Target>
                              <button
                                type="button"
                                className="team-color-target"
                                /* Инлайновый цвет сильнее правила наведения — у
                                   выбравшего цвет кольцо не гаснет под курсором. */
                                style={u.color ? { borderColor: u.color } : undefined}
                                aria-label={`Цвет: ${u.full_name}`}
                                disabled={!u.is_active || savingIds[u.id]}
                              >
                                <UserAvatar name={u.full_name} size={32} />
                              </button>
                            </Menu.Target>
                            <Menu.Dropdown>
                              <div className="team-color-grid">
                                {STAFF_COLORS.map((c) => (
                                  <button
                                    key={c}
                                    type="button"
                                    className={
                                      "team-color-swatch" +
                                      ((u.color ?? "") === c ? " team-color-swatch--active" : "")
                                    }
                                    style={{ background: c }}
                                    aria-label={`Цвет ${c}`}
                                    onClick={() =>
                                      saveField(u.id, { color: c }, () =>
                                        showToast({ message: "Цвет сохранён", color: "lp" }),
                                      )
                                    }
                                  />
                                ))}
                              </div>
                              <Menu.Item
                                onClick={() =>
                                  saveField(u.id, { color: "" }, () =>
                                    showToast({ message: "Цвет снят", color: "lp" }),
                                  )
                                }
                              >
                                Без цвета
                              </Menu.Item>
                            </Menu.Dropdown>
                          </Menu>
                          <span className="team-person__text">
                            <span className="team-person__name">{u.full_name}</span>
                            {/*
                              Почта — второй строкой под именем, а не своей
                              колонкой. В колонке ей доставалось около 200
                              пикселей на девять столбцов, и адрес переносился
                              посреди слова («admin@leadpart / ner.ru») — такой
                              не прочесть и не сверить глазом. Класс ячейки
                              оставлен прежним: по нему ходит правило ширины.
                            */}
                            <span className="team-members__email">{u.email}</span>
                          </span>
                        </div>
                      </td>
                      <td data-label="Роль" data-role={u.role}>
                        <Select
                          size="xs"
                          /* Заполняет ячейку, а не держит фиксированные 150px: иначе
                             излишек ширины на широком экране уходит в пустоту между
                             колонками, а не в содержимое (замер 13.08). */
                          w="100%"
                          aria-label={`Роль: ${u.full_name}`}
                          data={ROLE_OPTIONS}
                          value={u.role}
                          allowDeselect={false}
                          disabled={!u.is_active}
                          onChange={(v) => {
                            if (!v || v === u.role) return;
                            setActionError(null);
                            setPending({ kind: "role", user: u, role: v as Role });
                          }}
                        />
                      </td>
                      <td data-label="Отдел">
                        <TextInput
                          size="xs"
                          w="100%"
                          aria-label={`Отдел: ${u.full_name}`}
                          placeholder="—"
                          maxLength={MAX_DEPARTMENT}
                          value={draftDept[u.id] ?? u.department ?? ""}
                          onChange={(e) =>
                            setDraftDept((m) => ({ ...m, [u.id]: e.currentTarget.value }))
                          }
                          disabled={!u.is_active || savingIds[u.id]}
                          // Сохраняем по уходу фокуса, а не на каждой букве:
                          // отдел печатают целиком, и запрос на каждый символ
                          // засыпал бы сервер и журнал.
                          onBlur={(e) => {
                            const next = e.currentTarget.value.trim();
                            if (next === (u.department ?? "")) {
                              // Ничего не поменялось (или вернули как было) —
                              // черновик убираем, чтобы ячейка снова жила из
                              // ответа сервера.
                              forget(u.id);
                              return;
                            }
                            saveField(u.id, { department: next }, () =>
                              // Сохранение по уходу фокуса не видно ничем: поле
                              // выглядит так же, как и до него. Тост — тот
                              // единственный признак, что правка уехала.
                              showToast({ message: "Отдел сохранён", color: "lp" }),
                            );
                          }}
                        />
                      </td>
                      <td data-label="Ведёт диалоги">
                        <Switch
                          size="sm"
                          aria-label={`Ведёт диалоги: ${u.full_name}`}
                          checked={draftAnswers[u.id] ?? u.handles_conversations}
                          // Роль без права отвечать диалоги не ведёт по
                          // определению — переключать нечего, и показывать
                          // живой тумблер значило бы обещать несуществующее.
                          // `savingIds` — про другое: пока летит запрос, второй
                          // щелчок отправил бы обратное значение вдогонку.
                          disabled={!u.is_active || !canAnswer(u.role) || savingIds[u.id]}
                          onChange={(e) => {
                            const next = e.currentTarget.checked;
                            setDraftAnswers((m) => ({ ...m, [u.id]: next }));
                            saveField(u.id, { handles_conversations: next });
                          }}
                        />
                      </td>
                      <td data-label="Статус">
                        {statusLabel(u) && (
                          <Text
                            fz="xs"
                            /* Вес — по важности, а не по наличию текста.
                               «Ждёт пароля» жёлтым (за ним идут разбираться),
                               «отключён» обычным серым, «работает» — самым
                               тихим: колонка обязана быть живой, но не должна
                               кричать штатным состоянием. */
                            c={
                              u.invite_pending
                                ? "var(--lc-warning-text)"
                                : u.is_active
                                  ? "var(--lc-text-3)"
                                  : "var(--lc-text-2)"
                            }
                          >
                            {statusLabel(u)}
                          </Text>
                        )}
                      </td>
                      <td data-label="Онлайн">
                        <StatusDot tone={p.tone} /> {p.label}
                      </td>
                      <td>
                        <Menu position="bottom-end" withArrow>
                          <Menu.Target>
                            {/* `size="lg"` — 34px против прежних 28: мишень
                                должна проходить норму 24×24 с запасом, а не
                                впритык. Цвет задаётся через `c`, а не пропом
                                `color`: тот у варианта `subtle` красит ещё и
                                подложку при наведении. */}
                            <ActionIcon
                              variant="subtle"
                              size="lg"
                              c="var(--lc-text-2)"
                              aria-label={`Действия: ${u.full_name}`}
                            >
                              <IconMore size={18} />
                            </ActionIcon>
                          </Menu.Target>
                          <Menu.Dropdown>
                            {/* Приглашённому — перевыпуск ссылки, работающему —
                                сброс пароля: ручки разные (01 §3.3). */}
                            {/*
                              ⚠ ЧЕРЕЗ ПОДТВЕРЖДЕНИЕ (обход экранов 31.08).
                              Пункт стоит ПЕРВЫМ в меню, а срабатывал сразу:
                              промах мышью по строке работающего диспетчера
                              обнулял ему пароль и выкидывал из смены посреди
                              разговора с клиентом. Соседние опасные действия
                              (роль, отключение, удаление) давно спрашивают —
                              это одно почему-то не спрашивало.
                            */}
                            <Menu.Item
                              onClick={() => {
                                setActionError(null);
                                setPending({ kind: "reissue", user: u });
                              }}
                              disabled={!u.is_active || reissue.isPending}
                            >
                              {u.invite_pending ? "Выслать новую ссылку" : "Сбросить пароль"}
                            </Menu.Item>
                            <Menu.Item
                              onClick={() => {
                                setActionError(null);
                                setEditName(u.full_name);
                                setEditEmail(u.email);
                                setEditFor(u);
                              }}
                            >
                              Изменить имя и почту
                            </Menu.Item>
                            <Menu.Item
                              onClick={() => {
                                setActionError(null);
                                setNewPassword("");
                                setPasswordFor(u);
                              }}
                            >
                              Задать пароль
                            </Menu.Item>
                            {u.is_active ? (
                              <Menu.Item
                                color="red"
                                disabled={isMe}
                                onClick={() => {
                                  setActionError(null);
                                  setPending({ kind: "deactivate", user: u });
                                }}
                              >
                                {isMe ? "Отключить (нельзя себя)" : "Отключить"}
                              </Menu.Item>
                            ) : (
                              <Menu.Item
                                onClick={() => {
                                  setActionError(null);
                                  setPending({ kind: "activate", user: u });
                                }}
                              >
                                Включить
                              </Menu.Item>
                            )}
                            <Menu.Divider />
                            <Menu.Item
                              color="red"
                              disabled={isMe}
                              onClick={() => {
                                setActionError(null);
                                setPending({ kind: "delete", user: u });
                              }}
                            >
                              {isMe ? "Удалить (нельзя себя)" : "Удалить"}
                            </Menu.Item>
                          </Menu.Dropdown>
                        </Menu>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/*
            СПРАВКА О ПРАВАХ СТОИТ РЯДОМ С МЕСТОМ, ГДЕ ПРАВА МЕНЯЮТ (04.09).

            Описания ролей в проекте есть давно (`ROLE_HINTS`), но показывались
            они только при ПРИГЛАШЕНИИ — там, где человека заводят. А меняют
            роль здесь, в строке таблицы, и там выпадающий список из четырёх
            слов: «Наблюдатель» ничего не говорит о том, что он не сможет
            ответить клиенту. Права применяются немедленно, и цена ошибки —
            диспетчер без доступа посреди смены.

            Место для карточки взялось не из воздуха: таблица упирается в
            потолок ширины, и на широком мониторе справа от неё оставалась
            пустая треть экрана.
          */}
          {/*
            ⚠ НИЖЕ 1560 ТА ЖЕ СПРАВКА СВЁРНУТА (05.09). Второй строкой сетки
            она забирала высоту у списка раньше него самого: 1440×900 — пять
            строк из тринадцати, 1024 — две, 375 — ни одной, а подпись
            «13 сотрудников» ложилась поверх её текста. Роль меняют редко,
            список читают каждую смену — значит уступает справка, и уступает
            не содержимым, а одним нажатием.
          */}
          {foldRoles ? (
            <details className="lc-card team-roles team-roles--fold">
              <summary className="team-roles__summary">
                Что может роль
                <IconChevronDown size={16} className="team-roles__chevron" />
              </summary>
              {rolesReference}
            </details>
          ) : (
            <aside className="lc-card team-roles" aria-label="Что может каждая роль">
              <h2 className="team-roles__title">Что может роль</h2>
              {rolesReference}
            </aside>
          )}
        </div>
      )}

      {/* Подвал живёт СНАРУЖИ ветки со списком: опустевшая вторая страница
          обязана оставить кнопку «Назад», иначе с неё не выбраться. */}
      {!q.isPending && !q.isError && (
        <Pager
          offset={offset}
          shown={shown}
          total={total}
          pageSize={TEAM_PAGE_SIZE}
          noun={["сотрудник", "сотрудника", "сотрудников"]}
          onOffset={setOffset}
        />
      )}

      <InviteModal opened={inviteOpen} onClose={() => setInviteOpen(false)} onIssued={setIssued} />

      <OneTimeLinkModal
        opened={Boolean(issued)}
        title={issued ? `${issued.user.full_name} приглашён` : "Ссылка установки пароля"}
        url={issued?.invite_url ?? ""}
        onClose={() => setIssued(null)}
      />

      <Modal
        opened={pending !== null}
        onClose={closePending}
        centered
        title={
          pending?.kind === "role"
            ? "Сменить роль"
            : pending?.kind === "deactivate"
              ? "Отключить сотрудника"
              : pending?.kind === "delete"
                ? "Удалить сотрудника"
                : pending?.kind === "reissue"
                  ? pending.user.invite_pending
                    ? "Выслать новую ссылку"
                    : "Сбросить пароль"
                  : "Включить сотрудника"
        }
      >
        <Stack gap="var(--lc-space-3)">
          {actionError && (
            <Alert color="red" variant="light" role="alert">
              {actionError}
            </Alert>
          )}
          {pending?.kind === "role" && (
            <Text fz="sm" c="var(--lc-text-2)">
              {pending.user.full_name} станет: {ROLE_LABELS[pending.role]}. Права применятся немедленно.
            </Text>
          )}
          {pending?.kind === "deactivate" && (
            <Text fz="sm" c="var(--lc-text-2)">
              {pending.user.full_name} потеряет доступ прямо сейчас — открытые окна перестанут работать.
              Открытые диалоги останутся назначенными на него: переназначьте их через фильтр «Менеджер» в Чатах.
            </Text>
          )}
          {pending?.kind === "delete" && (
            <Text fz="sm" c="var(--lc-text-2)">
              {pending.user.full_name} исчезнет из всех списков, доступ оборвётся сразу, а
              незакрытые диалоги вернутся в «Входящие» — их разберёт смена. В журнале аудита
              человек останется: иначе на вопрос «кто это сделал» ответить будет нечем.
              Отменить удаление нельзя.
            </Text>
          )}
          {pending?.kind === "reissue" && (
            <Text fz="sm" c="var(--lc-text-2)">
              {pending.user.invite_pending
                ? `${pending.user.full_name} получит новую ссылку установки пароля, прежняя перестанет работать.`
                : `Пароль ${pending.user.full_name} перестанет работать прямо сейчас: человек выйдет из системы посреди работы и войдёт только по новой ссылке.`}
            </Text>
          )}
          {pending?.kind === "activate" && (
            <Text fz="sm" c="var(--lc-text-2)">
              {pending.user.full_name} снова сможет войти с прежним паролем.
            </Text>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={closePending} disabled={confirmBusy}>
              Отмена
            </Button>
            <Button
              color={
                pending?.kind === "deactivate" ||
                pending?.kind === "delete" ||
                (pending?.kind === "reissue" && !pending.user.invite_pending)
                  ? "red"
                  : undefined
              }
              loading={confirmBusy}
              onClick={runPending}
            >
              {pending?.kind === "role"
                ? "Сменить роль"
                : pending?.kind === "deactivate"
                  ? "Отключить"
                  : pending?.kind === "delete"
                    ? "Удалить"
                    : pending?.kind === "reissue"
                      ? pending.user.invite_pending
                        ? "Выслать"
                        : "Сбросить пароль"
                      : "Включить"}
            </Button>
          </Group>
        </Stack>
      </Modal>

      {/* Правка карточки. Почта здесь не косметика: это логин, и после смены
          человек входит уже по новому адресу — поэтому окно говорит об этом
          прямо, а не оставляет догадываться. Занятый адрес сервер отбивает
          409-м, текст ошибки показываем как есть. */}
      <Modal
        opened={editFor !== null}
        onClose={() => {
          setEditFor(null);
          setActionError(null);
        }}
        title="Изменить сотрудника"
      >
        <Stack gap="var(--lc-space-3)">
          {actionError && (
            <Alert color="red" variant="light" role="alert">
              {actionError}
            </Alert>
          )}
          <TextInput
            label="Имя"
            value={editName}
            onChange={(e) => setEditName(e.currentTarget.value)}
            data-autofocus
          />
          <TextInput
            label="Почта"
            description="Это логин: после смены вход будет по новому адресу"
            value={editEmail}
            onChange={(e) => setEditEmail(e.currentTarget.value)}
            autoComplete="off"
          />
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setEditFor(null);
                setActionError(null);
              }}
              disabled={editUser.isPending}
            >
              Отмена
            </Button>
            <Button
              loading={editUser.isPending}
              disabled={
                editName.trim().length === 0 ||
                editEmail.trim().length === 0 ||
                // Ничего не изменили — кнопка не нужна: сервер на пустое тело
                // отвечает 400, и человек получил бы ошибку за бездействие.
                (editName.trim() === (editFor?.full_name ?? "") &&
                  editEmail.trim().toLowerCase() === (editFor?.email ?? "").toLowerCase())
              }
              onClick={() => {
                if (!editFor) return;
                const body: { full_name?: string; email?: string } = {};
                if (editName.trim() !== editFor.full_name) body.full_name = editName.trim();
                if (editEmail.trim().toLowerCase() !== editFor.email.toLowerCase()) {
                  body.email = editEmail.trim();
                }
                setActionError(null);
                editUser.mutate({ id: editFor.id, body });
              }}
            >
              Сохранить
            </Button>
          </Group>
        </Stack>
      </Modal>

      {/* «Задать пароль» отдельным окном, а не строкой в меню: пароль надо
          набрать, а набор в выпадающем меню закрывается от первого щелчка мимо.

          ⚠ ДЛИНА НЕ ОГРАНИЧЕНА — РЕШЕНИЕ ВЛАДЕЛЬЦА 27.08. Здесь стояло «минимум
          10 знаков», и кнопка не нажималась, пока их не наберут. Правило это
          осталось там, где сотрудник задаёт пароль СЕБЕ (приём приглашения и
          смена своего пароля): им человек пользуется дальше. А это окно —
          администратор, который заводит вход руками, чаще всего временный.
          Пустое поле по-прежнему не принимается: ноль знаков — это не простой
          пароль, а вход без пароля. */}
      <Modal
        opened={passwordFor !== null}
        onClose={() => {
          setPasswordFor(null);
          setNewPassword("");
          setActionError(null);
        }}
        title="Задать пароль сотруднику"
      >
        <Stack gap="var(--lc-space-3)">
          {actionError && (
            <Alert color="red" variant="light" role="alert">
              {actionError}
            </Alert>
          )}
          <Text fz="sm" c="var(--lc-text-2)">
            {passwordFor?.full_name} сможет войти с этим паролем сразу — ссылку присылать не
            нужно. Все открытые окна этого сотрудника закроются.
          </Text>
          <TextInput
            label="Новый пароль"
            description="Любой пароль — ограничения по длине нет"
            value={newPassword}
            onChange={(e) => setNewPassword(e.currentTarget.value)}
            autoComplete="off"
            data-autofocus
          />
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setPasswordFor(null);
                setNewPassword("");
                setActionError(null);
              }}
              disabled={setPassword.isPending}
            >
              Отмена
            </Button>
            <Button
              loading={setPassword.isPending}
              disabled={newPassword.length === 0}
              onClick={() => {
                if (passwordFor) setPassword.mutate({ id: passwordFor.id, password: newPassword });
              }}
            >
              Задать пароль
            </Button>
          </Group>
        </Stack>
      </Modal>
    </section>
  );
}
