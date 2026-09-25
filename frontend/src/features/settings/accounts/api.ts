import { http, request } from "@/shared/api/http";
import type {
  ChannelOperatorsInput,
  ChannelOperatorsResponse,
  ChannelOperatorsSaved,
} from "@/shared/api/types";
import { plural } from "@/shared/lib/plural";

/**
 * Назначение операторов на канал Авито (план 7.2, экран из Jivo 15 §2.2).
 *
 * Обе ручки — право `accounts:manage`, то есть только администратор: состав
 * операторов канала определяет, кому вообще попадут обращения, и менять его
 * из интерфейса руководителя нельзя. Руководитель (`accounts:read`) видит на
 * карточке строку «Операторы: N» без кнопки — как и остальные управляющие
 * элементы этого экрана (11 §4.1).
 */

/** GET /avito-accounts/{id}/operators — назначенные + кандидаты со всеми флагами. */
export function fetchChannelOperators(
  accountId: string,
): Promise<ChannelOperatorsResponse> {
  return http.get<ChannelOperatorsResponse>(
    `/avito-accounts/${encodeURIComponent(accountId)}/operators`,
  );
}

/**
 * PUT /avito-accounts/{id}/operators — полная замена набора одним запросом.
 *
 * `request` напрямую, а не `http.put`: PUT-хелпера в `http` нет, и заводить
 * его — правка чужой зоны; тем же способом ходит `PUT /bots/{id}/accounts`
 * (features/settings/bots/api.ts).
 */
export function saveChannelOperators(
  accountId: string,
  operatorIds: string[],
): Promise<ChannelOperatorsSaved> {
  const body: ChannelOperatorsInput = { operator_ids: operatorIds };
  return request<ChannelOperatorsSaved>(
    `/avito-accounts/${encodeURIComponent(accountId)}/operators`,
    { method: "PUT", body },
  );
}

/**
 * Размер переписки канала — для подтверждения необратимого действия.
 *
 * Тип объявлен здесь, а не в `shared/api/types.ts`: там ему и место, но файл
 * в этом заходе чужая зона (см. cross-boundary) — переедет вместе с ключами
 * запросов.
 */
export interface ChannelHistorySize {
  conversations: number;
  messages: number;
}

/**
 * GET /avito-accounts/{id}/history-size — «сколько именно будет стёрто».
 *
 * Спрашивается по нажатию, а не вместе со списком карточек: считать
 * сообщения девяти каналов на каждое открытие экрана дорого, а нужны эти
 * числа ровно в момент, когда человек собрался стирать.
 */
export function fetchChannelHistorySize(
  accountId: string,
): Promise<ChannelHistorySize> {
  return http.get<ChannelHistorySize>(
    `/avito-accounts/${encodeURIComponent(accountId)}/history-size`,
  );
}

/**
 * «Будет стёрто 3 диалога и 128 сообщений» — строка для окна подтверждения.
 *
 * ПУСТОЙ КАНАЛ НАЗЫВАЕТСЯ ПУСТЫМ, и это половина смысла подсказки: ровно так
 * выглядит ошибочно подключённый аккаунт, ради которого удаление и заводили,
 * — и человек должен видеть, что стирать нечего, а не читать общее
 * предупреждение про безвозвратную потерю.
 *
 * `null` — числа получить не удалось (сеть, 500). Тогда честно говорим, что
 * размер неизвестен: молча показать «0 диалогов» было бы худшей из ошибок в
 * подтверждении необратимого действия.
 */
export function describeHistorySize(size: ChannelHistorySize | null): string {
  if (size === null) return "Сколько там переписки — посчитать не удалось.";
  if (size.conversations === 0)
    return "Переписки в канале нет — стирать нечего.";
  const dialogs = `${size.conversations} ${plural(size.conversations, "диалог", "диалога", "диалогов")}`;
  const messages = `${size.messages} ${plural(size.messages, "сообщение", "сообщения", "сообщений")}`;
  return `Будет стёрто ${dialogs} и ${messages}.`;
}

/**
 * Ключ кэша экрана назначения.
 *
 * По правилу 03 §2.1 ключи объявляются только в `shared/api/queryKeys.ts`, и
 * туда этот ключ и должен переехать — здесь он лежит временно, потому что
 * queryKeys.ts в этом заходе чужая зона (см. cross-boundary).
 *
 * Префикс `"accounts"` выбран не для красоты: `qk.accounts` — это `["accounts"]`,
 * поэтому `invalidateQueries({ queryKey: qk.accounts })` после сохранения
 * освежает и список карточек, и открытый экран назначения одним вызовом.
 */
export const channelOperatorsKey = (accountId: string) =>
  ["accounts", "operators", accountId] as const;

/** Сколько аватарок помещается в строку карточки; остальные — «+N», как в Jivo. */
export const OPERATOR_AVATARS_SHOWN = 4;

/* ------------------------------------------------------------------------- *
 *  Распознавание телефонов в тексте входящих (`/settings/phone-detect`).
 *
 *  ПОЧЕМУ ЭТИ ДВА ПЕРЕКЛЮЧАТЕЛЯ ЖИВУТ НА ЭКРАНЕ КАНАЛОВ. Настройка про то, что
 *  система делает с ЧУЖИМИ сообщениями, приходящими по этим самым каналам, и
 *  спрашивают о ней здесь же: «откуда в карточке взялся телефон». Отдельного
 *  раздела ради двух тумблеров заводить незачем — его бы не нашли.
 * ------------------------------------------------------------------------- */

export type MergeAutoMode = "off" | "shadow" | "on";

export interface PhoneDetectSettings {
  /** Разбирать ли текст входящих вообще. */
  enabled: boolean;
  /**
   * Писать найденное сразу: пустой основной заполняется, остальные номера из
   * переписки ложатся дополнительными без вопроса (12.09). Выключено — всё
   * уходит в вопросы оператору.
   */
  autofill: boolean;
  /** Наши номера через запятую — в карточку клиента не пишутся. */
  own_numbers: string;
  /** Сколько номеров сервер разобрал из строки — чтобы мусор не терялся молча. */
  own_numbers_parsed: string[];
  /** Автообъединение карточек-двойников по телефону. */
  merge_auto: MergeAutoMode;
}

/** Тот же префикс `"settings"`, что у соседних настроек экрана «Распределение». */
export const phoneDetectKey = ["settings", "phone-detect"] as const;

export function fetchPhoneDetect(): Promise<PhoneDetectSettings> {
  return http.get<PhoneDetectSettings>("/settings/phone-detect");
}

/**
 * Оба поля необязательны: шлём ровно тот тумблер, который тронули.
 *
 * Отправлять пару целиком было бы опаснее, чем кажется: экран открыт у
 * двоих, один включает распознавание, второй в ту же минуту выключает
 * автозапись — и полный PATCH второго вернул бы распознавание в то состояние,
 * которое он загрузил минуту назад, молча отменив чужое решение.
 */
export function savePhoneDetect(
  patch: Partial<PhoneDetectSettings>,
): Promise<PhoneDetectSettings> {
  return http.patch<PhoneDetectSettings>("/settings/phone-detect", patch);
}

/* ------------------------------------------------------------------------- *
 *  Адрес из переписки и проверка по карте (`/settings/address-detect`, 11.09).
 *  Живёт рядом с телефонами по той же причине: это про то, что система
 *  делает с чужими сообщениями из этих каналов.
 * ------------------------------------------------------------------------- */

export type GeoProvider = "nominatim" | "yandex" | "osm_then_yandex";

/** Лестница политики правила привязки (пакет 6.0а, §0.3). */
export type RulePolicy = "off" | "shadow" | "suggest" | "approx" | "exact";

/** Действующая политика правила: перекрытие настройки или умолчание реестра. */
export interface RulePolicyEffective {
  rule: string;
  label: string;
  policy: RulePolicy;
}

/** Действующее состояние правила разбора (пакет 7a). */
export interface ParseRuleEffective {
  rule: string;
  label: string;
  state: "on" | "off" | "shadow";
}

export interface AddressDetectSettings {
  enabled: boolean;
  /** Писать ПОДТВЕРЖДЁННЫЙ картой адрес в пустую карточку без человека. */
  autofill: boolean;
  /** Буквы уровней показа: «A», «AB», «ABC»; пусто — не показывать. */
  levels: string;
  geo_enabled: boolean;
  provider: GeoProvider;
  /**
   * Шлюз внешних API настроен (адрес и токен). Ключи помощников живут у него:
   * `false` — ключей нет ни у кого, и чинится адрес шлюза, а не ключ.
   * Необязательное: в окне выката фронт едет раньше бэкенда.
   */
  gateway_configured?: boolean;
  /** Ключ Яндекса есть у шлюза — иначе провайдер «yandex» молчит. */
  yandex_key_present: boolean;
  /** Потолок запросов к Яндексу в сутки; null — без потолка (платный тариф). */
  yandex_daily_limit: number | null;
  /** Сколько уже спросили у Яндекса сегодня. */
  yandex_used_today: number;
  /** Геосаджест: подсказка по опечатке/сокращению, когда карты дом не нашли. */
  suggest_enabled: boolean;
  suggest_key_present: boolean;
  suggest_daily_limit: number | null;
  suggest_used_today: number;
  /** DaData (ФИАС с координатами): спрашивается первой, до OSM и Яндекса. */
  dadata_enabled: boolean;
  dadata_key_present: boolean;
  dadata_daily_limit: number | null;
  dadata_used_today: number;
  /** Бесплатная модель OpenRouter перечитывает реплику, когда правила промолчали или карта отказала. */
  llm_enabled: boolean;
  llm_key_present: boolean;
  llm_daily_limit: number | null;
  llm_used_today: number;
  /** Ahunter — второй справочник ГАР без ключа и потолка (16.09). */
  ahunter_enabled: boolean;
  ahunter_used_today: number;
  /** Яндекс Спеллер — опечатки в улице до карты; 10 000 в сутки. */
  speller_enabled: boolean;
  speller_daily_limit: number | null;
  speller_used_today: number;
  /**
   * Автопривязка без человека (владелец 18.09): политика карты сама довершает
   * вердикт до степени — точная точка, приблизительная (улица, массив, центр
   * пункта) или строка улицы без точки — и адрес ложится в карточку с пометкой.
   * Выключено — в карточку сама идёт только точка, как до 18.09.
   */
  auto_decide: boolean;
  /**
   * Перекрытия политик правил строкой «правило=значение,…» — как хранятся.
   * Пишут и человек, и еженедельная лестница; сохранение человеком закрепляет
   * за ним все правила строки (вето на подъём до `exact`).
   */
  rule_policy: string;
  rule_policy_effective: RulePolicyEffective[];
  /** Перекрытия правил разбора «правило=on|off|shadow,…». */
  parse_rules: string;
  /** Необязательное: в окне выката фронт едет раньше бэкенда. */
  parse_rules_effective?: ParseRuleEffective[];
  /** Свои адреса (мастерская, офис) — список владельца, правится. */
  own_addresses: string;
  /** Тот же список, выведенный задачей из наших исходящих, — только показ. */
  own_addresses_auto: string;
}

export const addressDetectKey = ["settings", "address-detect"] as const;

export function fetchAddressDetect(): Promise<AddressDetectSettings> {
  return http.get<AddressDetectSettings>("/settings/address-detect");
}

export function saveAddressDetect(
  patch: Partial<
    Omit<
      AddressDetectSettings,
      | "gateway_configured"
      | "yandex_key_present"
      | "yandex_used_today"
      | "suggest_key_present"
      | "suggest_used_today"
      | "dadata_key_present"
      | "dadata_used_today"
      | "suggest_daily_limit"
      | "dadata_daily_limit"
      | "llm_key_present"
      | "llm_used_today"
      | "llm_daily_limit"
      | "ahunter_used_today"
      | "speller_daily_limit"
      | "speller_used_today"
      | "rule_policy_effective"
      | "parse_rules_effective"
      | "own_addresses_auto"
    >
  > & {
    unlimited_yandex?: boolean;
  },
): Promise<AddressDetectSettings> {
  return http.patch<AddressDetectSettings>("/settings/address-detect", patch);
}

/* ---------------------------------------------------------------------------
 *  Лента переписки: показывать ли служебные записи Авито (14 августа).
 * ------------------------------------------------------------------------- */

export interface ThreadSettings {
  /** Прятать ли служебные записи Авито в ленте. */
  avito_system_hidden: boolean;
}

export const threadSettingsKey = ["settings", "thread"] as const;

export function fetchThreadSettings(): Promise<ThreadSettings> {
  return http.get<ThreadSettings>("/settings/thread");
}

export function saveThreadSettings(
  patch: Partial<ThreadSettings>,
): Promise<ThreadSettings> {
  return http.patch<ThreadSettings>("/settings/thread", patch);
}
