import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { http } from "@/shared/api/http";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";

/**
 * Личность клиента: телефоны, идентификаторы Авито, ручные объединения.
 *
 * ПОЧЕМУ ОТДЕЛЬНЫЙ ЗАПРОС, А НЕ ПОЛЯ В ДЕТАЛИ ДИАЛОГА. Ответ ключуется
 * КЛИЕНТОМ, а деталь — диалогом. У одного человека диалогов бывает девять, и
 * класть в каждый одинаковый список телефонов значило бы считать его девять
 * раз на сервере и девять раз держать в кэше. При переходе между диалогами
 * ОДНОГО клиента (а это ровно то, что делает оператор, кликая по «Истории
 * клиента») запроса не будет вовсе — ключ тот же.
 *
 * ПОЧЕМУ КЛЮЧ ОБЪЯВЛЕН ЗДЕСЬ, А НЕ В `shared/api/queryKeys`. Правило «никаких
 * литеральных массивов в компонентах» (03 §2.1) про компоненты; это модуль
 * доступа к данным своей фичи, и держать его ключи рядом с его же запросами
 * честнее, чем растить общий словарь ради двух строк.
 *
 * ПЕРВЫЙ ЭЛЕМЕНТ — «clients», РОВНО КАК У `qk.clients.history`
 * (`["clients", "history", id]`). Это не совпадение и не украшение: TanStack
 * Query инвалидирует по ПРЕФИКСУ, и общий первый элемент означает, что один
 * `invalidateQueries({queryKey: CLIENTS_ROOT})` после объединения накрывает и
 * историю клиента, и его личность. Разъедься префиксы — история осталась бы
 * висеть от прошлой карточки.
 */
export const CLIENTS_ROOT = ["clients"] as const;

/**
 * Сбросить всё, что мы знаем о клиенте, — по кадру сокета.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 31.08: «когда клиент скинул номер, то чтобы он
 * привязался, приходится обновлять страницу». Так и было: сервер извлекал
 * телефон из текста входящего и записывал его клиенту, но наружу не сообщал
 * НИЧЕГО — а этот ключ не сверяет ни один кадр, ни тихая сверка (она трогает
 * только диалоги, сообщения и очередь), ни возврат на вкладку (перезапрос по
 * фокусу выключен глобально). Карточка держит данные свежими минуту и
 * перезапрашивает их лишь при пересоздании, которого у открытой карточки не
 * случается вовсе.
 *
 * Инвалидируем по КОРНЮ, а не по одному ключу личности: у клиента есть ещё
 * история и кандидаты на объединение, и телефон меняет их все.
 */
/**
 * Перезапросить всё, из чего собрана карточка клиента.
 *
 * ⚠ `conversationId` — НЕ УКРАШЕНИЕ, А ГЛАВНАЯ ЧАСТЬ (разбор записи 01.09).
 *
 * Здесь сбрасывалось только семейство `clients`, и это было ошибкой в самом
 * замысле: телефон в карточке приезжает НЕ оттуда, а в составе ДЕТАЛИ ДИАЛОГА
 * (`conversation.client.phone`). Из семейства `clients` берётся только строка
 * «Ещё номера этого человека».
 *
 * Отсюда ровно то, что владелец записал на видео: клиент прислал номер, внизу
 * карточки он появился («Ещё номера этого человека: +7 900 …»), а основное поле
 * осталось с кнопкой «указать телефон». Сервер записал телефон в ту же секунду
 * — замер показал разницу 0 секунд, — но экран об этом не спросил.
 *
 * Кадр `client:updated` несёт `conversation_id` с самого начала; не хватало
 * одной строки, которая бы им воспользовалась.
 */
export function сбросКлиента(conversationId?: string): void {
  void queryClient.invalidateQueries({ queryKey: CLIENTS_ROOT });
  if (conversationId) {
    void queryClient.invalidateQueries({
      queryKey: qk.conversations.detail(conversationId),
    });
  }
}
const identityKey = (clientId: string) =>
  [...CLIENTS_ROOT, "identity", clientId] as const;
const candidatesKey = (clientId: string) =>
  [...CLIENTS_ROOT, "merge-candidates", clientId] as const;

/** Ссылка на карточку клиента в ответах ручек объединения. */
export interface ClientCardRef {
  id: string;
  name: string | null;
  external_id: string;
  phone: string | null;
}

/**
 * Как назвать карточку, у которой имени нет.
 *
 * ЖИВЁТ ЗДЕСЬ, А НЕ РЯДОМ С РАЗМЕТКОЙ, потому что называть карточку приходится
 * в трёх разных местах (подсказка объединения, ручная склейка, двойник по
 * распознанному номеру), а «клиент без имени» в одном месте и «Без имени» в
 * другом — это два разных человека на слух у того, кто читает экран.
 */
export function cardTitle(card: ClientCardRef): string {
  return card.name?.trim() || `клиент ID ${card.external_id}`;
}

export interface MergedFrom extends ClientCardRef {
  merged_at: string | null;
  /**
   * `confirmed` — телефоны обеих карточек совпали ДО объединения; `assumed` —
   * совпало что-то слабее (имя, идентификатор) либо телефона не было вовсе.
   *
   * Считается сервером по журналу, а не по текущим строкам: после объединения
   * телефон победителя мог приехать от проигравшего, и сравнение живых полей
   * давало бы «подтверждено» у каждой склейки, включая ошибочную.
   */
  confidence: "confirmed" | "assumed";
  /** Объединила автоматика по телефону (12.09) — экран говорит это прямо. */
  auto: boolean;
  rule: string | null;
}

/**
 * Один номер карточки — с доказательством (12.09). Автоматика добавляет
 * дополнительные номера без вопроса, и оператор обязан видеть, откуда каждый:
 * диалог, сообщение, дата, кто решил и слово рядом («жена», «мастер»).
 */
export interface PhoneEntry {
  value: string;
  client_id: string;
  primary: boolean;
  candidate_id: string | null;
  /**
   * `dialog` — назван в переписке; `voice` — вычитан из расшифровки голосового
   * (доказательство машинное: оператор сверяет со звуком, а не с написанным);
   * `swap` — был основным до обмена; `manual` — вписан руками.
   */
  source: "dialog" | "voice" | "swap" | "manual" | "other";
  conversation_id: string | null;
  message_id: string | null;
  message_at: string | null;
  hint: string | null;
  decided_by: "auto" | "operator" | null;
  /** Отличается от основного одной цифрой или перестановкой — похоже на исправление. */
  near_primary: boolean;
}

export interface MergeCandidate extends ClientCardRef {
  /**
   * Что совпало: `phone` — сильный довод, `name` и `external_id` — слабые.
   *
   * `phone_candidate` — совпал номер, ВЫЧИТАННЫЙ из переписки и ещё никем не
   * подтверждённый (`app/services/clients.py::merge_candidates`). Это отдельная
   * причина, а не разновидность `phone`, ровно потому, что за ней никто не
   * ручается: у `phone` номер уже прошёл через человека, здесь — цепочка цифр
   * из сообщения. Пока значения не было в этом типе, `REASON_TEXT[reason]`
   * молча давал `undefined`, и подсказка печатала «Совпало: .».
   */
  reason: "phone" | "phone_candidate" | "name" | "external_id";
  confidence: "confirmed" | "assumed";
  /** Эту пару человек уже разъединял: автоматика её не тронет, подсказка говорит об этом. */
  vetoed?: boolean;
  /** В найденную карточку уже объединяли: присоединять надо текущую к ней. */
  has_group?: boolean;
  /** В открытую карточку уже объединяли — вместе с `has_group` объединить нельзя вовсе. */
  mine_has_group?: boolean;
}

/**
 * Откуда взялся номер, стоящий в карточке.
 *
 * ЧЕТЫРЕ ЗНАЧЕНИЯ, А НЕ ДВА, И ЭТО НЕ ИЗБЫТОЧНОСТЬ. `phone_manual` делит мир
 * надвое: «набрали руками» и «всё остальное». В «остальном» лежат три разные
 * вещи — номер, вычитанный из переписки (клиент написал его сам), номер,
 * принесённый ботом, и строки старше распознавания, про которые не известно
 * ничего. Подпись под телефоном ручается за него чужим авторитетом: «(из
 * диалога)» под набранным со слов приписывает клиенту цифры, которых он не
 * писал, а «(со слов)» под вычитанным — наоборот, снимает доверие, которое
 * номер заслужил.
 *
 * `voice` — ПЯТОЕ, и оно стоит отдельно от `dialog` ровно там, где риск (19.09):
 * номер вычитан из машинной расшифровки голосового. Ослышка Whisper в одной
 * цифре даёт правдоподобный номер (у опечатки чаще ломается длина), и подпись
 * «(из диалога)» под ним ручалась бы за машину авторитетом клиента. Подпись
 * «(из голосового)» говорит оператору сверить со звуком.
 *
 * `none` — номера нет вовсе; подписывать в карточке нечего.
 */
export type PhoneSource = "manual" | "dialog" | "voice" | "other" | "none";
/**
 * У адреса на один случай больше: `auto` — записала карта без человека (11.09);
 * и на один меньше: «голос» карточка адреса не различает — автозапись адреса
 * подписывается степенью точки карты, а не тем, написан адрес или наговорен
 * (`clients._address_source` значения `voice` не отдаёт).
 */
export type AddressSource = Exclude<PhoneSource, "voice"> | "auto";

/**
 * Номер, вычитанный из переписки и ЖДУЩИЙ решения оператора.
 *
 * В `identity.phone_candidates` сервер кладёт только ожидающих (`pending`):
 * принятые уже стоят в карточке или в списке `phones`, отклонённые — закрытый
 * вопрос. Пустой список означает «спрашивать не о чем», и блок не рисуется вовсе.
 */
export interface PhoneCandidate {
  id: string;
  /** Канонический вид (+7XXXXXXXXXX) — ровно то, что уедет в карточку. */
  phone: string;
  /**
   * Как номер написан в сообщении: «+7(900)1112240». Не для полноты: оператор
   * обязан иметь возможность сверить нашу догадку с тем, что человек написал.
   */
  raw: string;
  conversation_id: string;
  message_id: string | null;
  message_at: string | null;
  detected_at: string | null;
  /**
   * `inbound` — поймали на входящем, `rescan` — нашли пересчётом старой
   * переписки, `voice` — вычитали из расшифровки голосового (19.09): цитата у
   * такого предложения — не тело сообщения (оно пусто), а расшифровка.
   */
  source: "inbound" | "rescan" | "voice";
  /** В личности всегда `pending`; остальные два приезжают ответом на решение. */
  status: "pending" | "accepted" | "rejected";
}

/**
 * Распознанный в переписке адрес — предложение, а не факт.
 *
 * `raw` и `message_id` здесь не для полноты: по адресу поедет мастер, и
 * предложение, которое нельзя сверить с тем, что написал клиент, оператор либо
 * примет не глядя, либо перестанет замечать.
 */
/**
 * Вердикт карты по строке (11.09). `exact` — карта подтвердила дом (точка
 * дома или приблизительная — см. `AddressGeo.precision`); остальные —
 * именованный отказ, который оператор читает словами (`geoWords`). С 18.09
 * строка карты идёт в карточку и при отказе — текстом без точки, когда карта
 * знает улицу клиента в его пункте (`formatted` есть, `precision: "none"`).
 */
export type GeoStatus =
  | "pending"
  | "exact"
  | "ambiguous"
  | "not_found"
  | "house_missing"
  | "house_mismatch"
  | "street_mismatch"
  | "settlement_mismatch"
  | "city_mismatch"
  | "region_mismatch"
  | "other_city_in_text"
  | "no_city"
  | "blocked"
  | "error"
  // Дом нашёлся только в другом городе области объявления (12.09).
  | "elsewhere";

/** Один из вариантов карты, когда она не выбрала дом сама (12.09). */
export interface GeoVariant {
  formatted: string;
  lat: number | null;
  lon: number | null;
  city: string | null;
}

export interface AddressGeo {
  status: GeoStatus;
  /** «Сиреневая улица, 1, посёлок Заречный, Орск» — как в картах. */
  formatted: string | null;
  lat: number | null;
  lon: number | null;
  /**
   * Имя карты для подписи по условиям карт (OSM / Яндекс / Спеллер):
   * «speller+dadata», «ahunter+nominatim», «dadata+yandex». Степень точки
   * отсюда НЕ читается — хвост «~approx» разбирает только сервер.
   */
  provider: string | null;
  checked_at: string | null;
  /**
   * Степень точки — единственный источник для «точка дома / приблизительная /
   * без точки» (сервер, 18.09): `exact` — точка дома; `approx` — точка улицы,
   * массива, центра пункта или места; `none` — точки нет (карта не
   * подтвердила, ещё проверяет или в карточке текст без точки).
   */
  precision: "exact" | "approx" | "none";
  /** Что нашла карта, когда не выбрала один дом: информация к «изменить». */
  variants?: GeoVariant[];
  /**
   * ВТОРАЯ ОСЬ ВЕРДИКТА — ПРИЧИНА (пакет 6.0а, 20.09). `precision` говорит,
   * КАКАЯ точка; `rule` — ПОЧЕМУ она такая: имя правила слоя вердикта
   * (`street_point`, `only_in_radius`, `house_family`…), которым решена
   * строка, и его подпись словами (`rule_label`, словарь `RULE_LABEL` на
   * сервере — экран подписей не сочиняет). `null` — решала сама карта, оси
   * нет. Отдаётся и у источника карточки (`address_geo`), и у строк.
   */
  rule?: string | null;
  rule_label?: string | null;
  /**
   * Вердикт — ПРЕДЛОЖЕНИЕ правила (политика `suggest`, §0.3 программы): в
   * карточку автоматика его не положила, точка на экране настоящая, варианты
   * сохранены; оператор принимает одним нажатием (`resolve` с `replace`) или
   * отказывает («Не адрес»). У записанного адреса всегда `false`.
   */
  suggest?: boolean;
}

export interface AddressCandidate {
  id: string;
  /** `pending` | `accepted` | `rejected` — ответ на решение говорит свой исход сам. */
  status: string;
  value: string;
  /** Населённый пункт и город, названные клиентом рядом с улицей. */
  settlement: string | null;
  settlement_type: string | null;
  locality: string | null;
  /** null — строка старше проверки по карте. */
  geo: AddressGeo | null;
  street: string;
  house: string;
  /**
   * «house» — улица и дом; «place» — только место без улицы («Гатчинский р-н,
   * д. Малая Сосновка, массив Южный», 13.09): у места street/house пустые,
   * карта даёт точку пункта или массива, улица придёт позже и заменит его.
   */
  kind: "house" | "place";
  district: string | null;
  area: string | null;
  /** Квартира, подъезд, этаж, домофон — дописываются следующими сообщениями. */
  parts: Partial<Record<"office" | "entrance" | "floor" | "intercom", string>>;
  raw: string;
  level: string;
  conversation_id: string;
  message_id: string | null;
  message_at: string | null;
  detected_at: string;
  /**
   * Автоматика эту строку сама не запишет (автозапись выключена или правило
   * понижено до `suggest` после суда) — у неё кнопка «Записать в карточку».
   * Необязательное: в окне выката фронт едет раньше бэкенда.
   */
  writable?: boolean;
}

export interface ClientIdentityDto {
  id: string;
  name: string | null;
  phone: string | null;
  /**
   * Номер введён руками, а не вычитан из переписки.
   *
   * Оставлено рядом с `phone_source` намеренно: поле старое, его читают другие
   * ответы сервера, и убирать его отсюда ради красоты значило бы тронуть чужие
   * места ради нуля пользы. Подпись под телефоном берётся из `phone_source` —
   * он различает четыре случая там, где этот признак различает два.
   */
  phone_manual: boolean;
  phone_source: PhoneSource;
  /**
   * Адрес выезда. Пишет автоматика (`autofill_address`) по степени строки
   * (`address_geo.precision` и текст без точки); руками — через PUT
   * («изменить»), и правку человека автоматика не трогает.
   */
  address: string | null;
  address_source: AddressSource;
  /**
   * Строки, которые автоматика НЕ записала: проверяются, удержаны сторожем
   * или второй адрес диалога. Показ, не вопрос; единственное решение с
   * экрана — «Не адрес». Отобраны сервером по уровню.
   */
  address_candidates: AddressCandidate[];
  /** Вердикт карты по строке, из которой записан адрес (null — адрес руками). */
  address_geo: AddressGeo | null;
  /** Цитата клиента и части адреса под уже записанным адресом — доказательство живёт дольше решения. */
  address_evidence: {
    raw: string;
    parts: Partial<
      Record<"office" | "entrance" | "floor" | "intercom", string>
    >;
    conversation_id: string;
    /** Город, названный клиентом словами, — для слов «клиент назвал другой город: …». */
    locality: string | null;
  } | null;
  /** Прочие подтверждённые адреса человека, кроме основного. */
  addresses: { value: string; client_id: string }[];
  /** Только ожидающие решения. Пустой список — блока кандидатов нет. */
  phone_candidates: PhoneCandidate[];
  external_id: string;
  phones: PhoneEntry[];
  /** Снятые «Не его номер» — с действием «Вернуть»; только этой карточки. */
  rejected_phones?: Array<{
    value: string;
    candidate_id: string;
    resolved_at: string | null;
  }>;
  avito_ids: Array<{ value: string; client_id: string; primary: boolean }>;
  merged_from: MergedFrom[];
  /** Карточка, В КОТОРУЮ увели эту. Почти всегда null — см. сервер. */
  merged_into: ClientCardRef | null;
}

export function useClientIdentity(
  clientId: string | undefined,
  enabled = true,
) {
  return useQuery({
    queryKey: identityKey(clientId ?? ""),
    queryFn: () =>
      http.get<ClientIdentityDto>(
        `/clients/${encodeURIComponent(clientId!)}/identity`,
      ),
    enabled: enabled && Boolean(clientId),
    staleTime: 60_000,
  });
}

/**
 * Подсказки «может быть, это один человек».
 *
 * Запрашиваются ТОЛЬКО когда карточку открыл тот, кто может объединять:
 * показывать подсказку без кнопки — это рассказать о проблеме и не дать её
 * решить.
 */
export function useMergeCandidates(
  clientId: string | undefined,
  enabled: boolean,
) {
  return useQuery({
    queryKey: candidatesKey(clientId ?? ""),
    queryFn: () =>
      http.get<{ items: MergeCandidate[] }>(
        `/clients/${encodeURIComponent(clientId!)}/merge-candidates`,
      ),
    enabled: enabled && Boolean(clientId),
    staleTime: 60_000,
  });
}

export interface SetPhoneResult {
  phone: string;
  changed: boolean;
  /** Другие карточки с этим же номером — повод предложить объединение. */
  twins: ClientCardRef[];
}

/**
 * Обновление кэша после любой правки личности.
 *
 * СБРАСЫВАЕТСЯ И ДЕТАЛЬ ДИАЛОГА, И СПИСОК. Имя с телефоном едут в детали
 * (`conversation.client`), а список диалогов рисует то же имя в строке; после
 * объединения у победителя меняются оба, и оставить список нетронутым значило
 * бы показать в одном окне два разных имени одного человека.
 */
function useIdentityInvalidation(
  clientId: string | undefined,
  convId: string | null,
) {
  const qc = useQueryClient();
  return async () => {
    await Promise.all([
      clientId
        ? qc.invalidateQueries({ queryKey: identityKey(clientId) })
        : Promise.resolve(),
      clientId
        ? qc.invalidateQueries({ queryKey: candidatesKey(clientId) })
        : Promise.resolve(),
      convId
        ? qc.invalidateQueries({ queryKey: qk.conversations.detail(convId) })
        : Promise.resolve(),
      qc.invalidateQueries({ queryKey: qk.conversations.root }),
      qc.invalidateQueries({ queryKey: CLIENTS_ROOT }),
    ]);
  };
}

export interface SetNameResult {
  name: string | null;
  changed: boolean;
}

/**
 * Имя клиента, введённое человеком (требование заказчика 13 августа).
 *
 * Инвалидация та же, что у телефона, и по той же причине: имя стоит в трёх местах
 * одновременно — в карточке, в шапке диалога и строкой в списке чатов. Обнови одно —
 * и в одном окне будут два разных имени одного человека.
 */
export function useSetClientName(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (name: string) =>
      http.put<SetNameResult>(
        `/clients/${encodeURIComponent(clientId!)}/name`,
        { name },
      ),
    onSuccess: refresh,
  });
}

export function useSetClientPhone(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (phone: string) =>
      http.put<SetPhoneResult>(
        `/clients/${encodeURIComponent(clientId!)}/phone`,
        {
          phone,
          conversation_id: convId,
        },
      ),
    onSuccess: refresh,
  });
}

export interface SetAddressResult {
  address: string | null;
  changed: boolean;
}

/** Записать адрес руками. Пустая строка стирает — это законное действие. */
export function useSetClientAddress(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (address: string) =>
      http.put<SetAddressResult>(
        `/clients/${encodeURIComponent(clientId!)}/address`,
        {
          address,
          conversation_id: convId,
        },
      ),
    onSuccess: refresh,
  });
}

/**
 * «Адрес неверный» под адресом, который записала автоматика (пакет 6.0а, I-9).
 *
 * Одно нажатие вместо «изменить → стереть → сохранить»: сервер отказывает
 * строке-источнику (место закрыто для автоматики), очищает карточку и пишет
 * в журнал причину `wrong` — по ней воронка считает отказы руками по
 * правилу. Только для `address_source === "auto"`: набранный руками или
 * принятый человеком адрес сервер не трогает (409 `address_not_auto`) —
 * это спор двух людей, и он решается через «изменить».
 */
export function useRejectAutoAddress(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: () =>
      http.post<SetAddressResult>(
        `/clients/${encodeURIComponent(clientId!)}/address/wrong`,
        { conversation_id: convId },
      ),
    /* Обновляемся и после отказа: 409 значит «коллега уже исправил или
       подтвердил» — кнопка обязана пропасть вместе с устаревшим адресом. */
    onSettled: refresh,
  });
}

export interface ResolveAddressResult {
  address: string | null;
  candidate: AddressCandidate;
}

/**
 * Решение по распознанному адресу. Слова те же, что у телефона: «заменить»,
 * «добавить», «отклонить» — четвёртого понимания тех же трёх действий заводить
 * незачем. С 18.09 экран зовёт только `reject` («Не адрес»): адрес пишет
 * автоматика по степени, `replace`/`add` остаются за CLI и тестами.
 */
export function useResolveAddressCandidate(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: ({
      candidateId,
      decision,
      variant,
    }: {
      candidateId: string;
      decision: PhoneCandidateDecision;
      /** Индекс выбранного варианта карты (`geo.variants`). */
      variant?: number;
    }) =>
      http.post<ResolveAddressResult>(
        `/clients/${encodeURIComponent(clientId!)}/address-candidates/${encodeURIComponent(candidateId)}/resolve`,
        variant === undefined ? { decision } : { decision, variant },
      ),
    /* Обновляемся и после отказа: самый частый здесь — 409 «решение уже
       принято», когда тот же вопрос висел у коллеги и он ответил первым. */
    onSettled: refresh,
  });
}

/**
 * Сделать основным один из УЖЕ ИЗВЕСТНЫХ номеров человека — обмен местами.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09: «второй изменить нельзя, поменять их местами
 * нельзя». Дополнительные номера рисовались обычным текстом и действий не
 * имели.
 *
 * ⚠ ЭТО НЕ `useSetClientPhone` С ДРУГИМ АРГУМЕНТОМ. Та ручка ЗАТИРАЕТ прежний
 * номер (там человек исправляет опечатку, и хранить неверное вторым значило бы
 * показывать в карточке заведомую ложь). Здесь прежний удерживается
 * дополнительным: обмен — перестановка, а не исправление, и оба номера
 * принадлежат человеку.
 *
 * `convId` обязателен — без диалога прежний номер негде удержать, и сервер
 * отвечает 422. Кнопка поэтому и не рисуется, когда карточка открыта не из
 * диалога.
 */
export interface MakePrimaryResult {
  phone: string;
  changed: boolean;
  /**
   * Прежний основной номер. Он не пропал: остался в карточке дополнительным —
   * ровно ради этого ручка и заведена. `changed: false` (номер уже был
   * основным) поля не несёт вовсе.
   */
  previous?: string | null;
}

export function useMakePhonePrimary(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (phone: string) =>
      http.post<MakePrimaryResult>(
        `/clients/${encodeURIComponent(clientId!)}/phone/primary`,
        {
          phone,
          conversation_id: convId,
        },
      ),
    onSuccess: refresh,
  });
}

/**
 * Что оператор может сделать с распознанным номером. Слова — из постановки
 * владельца, и они же напечатаны на кнопках: переводить их по дороге в другие
 * значило бы завести четвёртое понимание тех же трёх действий.
 */
export type PhoneCandidateDecision = "replace" | "add" | "reject";

export interface ResolveCandidateResult {
  /** Телефон карточки ПОСЛЕ решения: у «добавить» и «отклонить» — прежний. */
  phone: string | null;
  candidate: PhoneCandidate;
  /**
   * Другие карточки с этим же номером. При «отклонить» всегда пусто — оператор
   * только что сказал, что номер не его, и искать по нему двойников незачем.
   */
  twins: ClientCardRef[];
}

export function useResolvePhoneCandidate(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: ({
      candidateId,
      decision,
    }: {
      candidateId: string;
      decision: PhoneCandidateDecision;
    }) =>
      http.post<ResolveCandidateResult>(
        `/clients/${encodeURIComponent(clientId!)}/phone-candidates/${encodeURIComponent(candidateId)}/resolve`,
        { decision },
      ),
    /**
     * ОБНОВЛЯЕМСЯ И ПОСЛЕ ОТКАЗА, а не только после успеха (`onSettled`, не
     * `onSuccess`). Самый частый отказ здесь — 409 «решение уже принято»: тот
     * же вопрос висел в карточке у коллеги, и он ответил первым. Оставить
     * предложение на экране значило бы звать нажать ещё раз и получить тот же
     * 409; правда о карточке в этот момент только на сервере.
     */
    onSettled: refresh,
  });
}

export function useMergeClients(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (sourceId: string) =>
      http.post<{ moved_conversations: number }>(
        `/clients/${encodeURIComponent(clientId!)}/merge`,
        { source_id: sourceId },
      ),
    onSuccess: refresh,
  });
}

/**
 * Присоединить открытую карточку к группе найденной: объединение в обратную
 * сторону. В карточку, в которую уже объединяли, другую влить нельзя — цепочка
 * сломала бы «Разъединить».
 */
export function useJoinGroup(clientId: string | undefined, convId: string | null) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (targetId: string) =>
      http.post<{ moved_conversations: number }>(
        `/clients/${encodeURIComponent(targetId)}/merge`,
        { source_id: clientId },
      ),
    onSuccess: refresh,
  });
}

export function useUnmergeClients(
  clientId: string | undefined,
  convId: string | null,
) {
  const refresh = useIdentityInvalidation(clientId, convId);
  return useMutation({
    mutationFn: (sourceId: string) =>
      http.post<{ moved_conversations: number }>(
        `/clients/${encodeURIComponent(clientId!)}/unmerge`,
        { source_id: sourceId },
      ),
    onSuccess: refresh,
  });
}
