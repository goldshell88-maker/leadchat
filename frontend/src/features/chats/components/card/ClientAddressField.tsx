import { useEffect, useRef, useState } from "react";
import { Button, Text, TextInput } from "@mantine/core";
import { ApiError } from "@/shared/api/http";
import { ExternalLink } from "@/shared/ui/ExternalLink";
import { IconCopy, IconPin } from "@/shared/ui/Icon";
import { showToast } from "@/shared/ui/toast";
import type { AddressCandidate, AddressGeo, AddressSource } from "./clientApi";
import { geoWords } from "./geoWords";
import {
  useRejectAutoAddress,
  useResolveAddressCandidate,
  useSetClientAddress,
} from "./clientApi";
import "./client-card.css";

/**
 * АДРЕС ВЫЕЗДА — ПОЛЕМ, КОТОРОЕ ЗАПОЛНЯЕТ АВТОМАТИКА, А НЕ ОЧЕРЕДЬЮ ВОПРОСОВ.
 *
 * ⚠ ПОЧЕМУ НЕ ОЧЕРЕДЬ, КАК У ТЕЛЕФОНА. Замер боя: очередь предложений по
 * телефону за 30 дней получила 5 534 строки и 24 решения, 148 висят
 * непрочитанными с медианой возраста 15,7 дня. Она работает не потому, что её
 * читают, а потому что автозапись решает 6 736 случаев из 6 759. Поэтому
 * распознанное показывается ПРЯМО В ПОЛЕ и не требует ни одного решения,
 * чтобы приносить пользу: адрес видно и его можно скопировать.
 *
 * ⚠ С 18.09 АДРЕС ПИШЕТ АВТОМАТИКА ПО СТЕПЕНИ, ОПЕРАТОР НИЧЕГО НЕ ПОДТВЕРЖДАЕТ
 * (решение владельца). Степеней три, и экран читает их ОДНИМ полем сервера
 * `geo.precision`: `exact` — точка дома; `approx` — точка улицы, массива,
 * центра пункта или места; `none` — точки нет, в карточке текст без точки
 * (карта знает улицу клиента, но не дом). Подпись под адресом называет
 * степень словами; хвост провайдера («~approx») экран НЕ разбирает — иначе
 * два пути к одному признаку. У человека остаются два действия: «изменить»
 * у карточки (PUT, и правку руками автоматика не трогает никогда) и «Не
 * адрес» у строки — отказ, после которого место закрыто для автоматики.
 *
 * ⚠ СТРОКИ ПОД ПОЛЕМ — ПОКАЗ, А НЕ ВОПРОС. Сервер отдаёт только то, что
 * автоматика НЕ записала: ещё проверяется, удержано сторожем или второй адрес
 * диалога («Также назван» под заполненной карточкой — между двумя местами
 * карточка автоматикой не переезжает). Каждый отказ карты имеет имя — «карта
 * нашла дом в другом городе», «на карте несколько таких адресов» — и
 * варианты карты показываются текстом: информация тому, кто нажмёт
 * «изменить».
 *
 * ⚠ ЦИТАТА КЛИЕНТА ОБЯЗАТЕЛЬНА — И ПОД УЖЕ ЗАПИСАННЫМ АДРЕСОМ ТОЖЕ. По адресу
 * поедет мастер. Строка без фразы, из которой она вычитана, проверить
 * нечем; а записанный автоматикой адрес без цитаты не отличить от набранного
 * руками. Доказательство живёт дольше решения.
 *
 * ⚠ ДВЕ ОСИ ВЕРДИКТА (пакет 6.0а, 20.09). `precision` — КАКАЯ точка,
 * `rule_label` — ПОЧЕМУ она такая («точка улицы, дом не найден», «один из
 * нескольких тёзок в 40 км от города»). Вторая ось рисуется словами сервера
 * под записанным адресом и под строками — экран подписей не сочиняет; без
 * правила (решала сама карта) оси нет. Варианты карты у источника показываются
 * так же, как у строк: кто нажмёт «изменить», видит, из чего выбирала карта.
 * Под записанным АВТОМАТИКОЙ адресом есть кнопка «Адрес неверный» — одно
 * нажатие с подтверждением вместо «изменить → стереть → сохранить»: строка
 * закрывается для автоматики, карточка пустеет, а в журнале остаётся причина,
 * по которой воронка считает отказы по правилу. Под набранным руками её нет.
 * Кнопка записи у строки — там, где адрес иначе не запишет никто:
 * ПРЕДЛОЖЕНИЕ правила (`geo.suggest`, политика `suggest`) и строка, которую
 * автоматика сама не возьмёт (`writable`: автозапись выключена или правило
 * понижено до `suggest` после суда). Человек принимает одним нажатием
 * (`resolve` с `replace`).
 *
 * ⚠ ССЫЛКА «НА КАРТЕ» — ТОЛЬКО КООРДИНАТАМИ. Строка запроса с адресом клиента
 * в чужом сервисе — персональные данные наружу; координаты дома их не несут.
 */

const SOURCE_LABEL: Record<
  Exclude<AddressSource, "none" | "auto">,
  string
> = {
  manual: "(со слов)",
  dialog: "(из диалога)",
  other: "(источник неизвестен)",
};

/**
 * Подпись автозаписи — по степени точки, не по имени карты. Без точки
 * (`precision: "none"`) поле держит одно из двух: строку карты без точки
 * (карта знает улицу клиента, номер дома — его слова; `formatted` есть) или
 * слова самого клиента (карта отказала или выключена; `formatted` нет) —
 * подпись называет, что именно (ревью 19.09: «словами клиента» под строкой
 * карты врало).
 */
function подписьИсточника(
  source: AddressSource | null,
  geo: AddressGeo | null,
): string | null {
  if (!source || source === "none") return null;
  if (source !== "auto") return SOURCE_LABEL[source];
  switch (geo?.precision) {
    case "exact":
      return "(автоматически, по карте)";
    case "approx":
      return "(автоматически, точка приблизительная)";
    default:
      return geo?.formatted
        ? "(автоматически: улица по карте, дом со слов клиента)"
        : "(автоматически, словами клиента)";
  }
}

/** Ссылка на дом координатами: без адреса в строке запроса. */
function mapHref(geo: AddressGeo | null): string | null {
  if (!geo || geo.lat === null || geo.lon === null) return null;
  const ll = `${geo.lon},${geo.lat}`;
  return `https://yandex.ru/maps/?ll=${ll}&pt=${ll}&z=17`;
}

/** «кв 3 · 2 подъезд · 5 этаж · домофон 1234» — части одной строкой. */
function частиСтрокой(parts: AddressCandidate["parts"]): string {
  const куски: string[] = [];
  if (parts.office) куски.push(`кв ${parts.office}`);
  if (parts.entrance) куски.push(`${parts.entrance} подъезд`);
  if (parts.floor) куски.push(`${parts.floor} этаж`);
  if (parts.intercom) куски.push(`домофон ${parts.intercom}`);
  return куски.join(" · ");
}

/** Части, которых нет в тексте адреса: «кв 3» уже в поле — её не повторяем. */
function частиНеВАдресе(
  parts: AddressCandidate["parts"],
  address: string,
): string {
  const текст = address.toLowerCase();
  const куски: string[] = [];
  if (parts.office && !текст.includes(`кв ${parts.office}`.toLowerCase()))
    куски.push(`кв ${parts.office}`);
  if (parts.entrance && !текст.includes(`подъезд ${parts.entrance}`))
    куски.push(`${parts.entrance} подъезд`);
  if (parts.floor && !текст.includes(`этаж ${parts.floor}`))
    куски.push(`${parts.floor} этаж`);
  if (parts.intercom && !текст.includes(`домофон ${parts.intercom}`))
    куски.push(`домофон ${parts.intercom}`);
  return куски.join(" · ");
}

function GeoLine({
  geo,
  locality,
  kind = "house",
  street = null,
}: {
  geo: AddressGeo | null;
  locality: string | null;
  kind?: AddressCandidate["kind"];
  street?: string | null;
}) {
  const слова = geoWords(geo, locality, kind, street);
  if (!слова) return null;
  const тон =
    geo?.status === "exact"
      ? "var(--lc-success-text)"
      : geo?.status === "pending"
        ? "var(--lc-text-3)"
        : "var(--lc-warn-text)";
  const href = mapHref(geo);
  return (
    <Text
      component="p"
      fz="xs"
      c={тон}
      className="card-address__geo"
      data-geo-status={geo?.status}
    >
      {слова}
      {href && (
        <>
          {" · "}
          <ExternalLink url={href} className="card-address__map-link">
            на карте
          </ExternalLink>
        </>
      )}
      {/* Подписи карт — по имени провайдера: это условия использования карт,
          а не степень точки. Порядок важен: подпись карты первая, степень —
          после неё. */}
      {geo?.provider?.startsWith("speller+") && geo.status === "exact" && (
        <span className="card-address__attribution">
          {" "}
          · Проверка правописания:{" "}
          <ExternalLink url="https://yandex.ru/dev/speller/">Яндекс.Спеллер</ExternalLink>
        </span>
      )}
      {/* Подстрокой, не концом строки: хвост «~approx» (приблизительная
          точка OSM) — степень, а не другая карта, и условия использования
          OSM он не отменяет. */}
      {geo?.provider?.includes("nominatim") && geo.status === "exact" && (
        <span className="card-address__attribution">
          {" "}
          · © OpenStreetMap contributors
        </span>
      )}
      {/* Точку дал Яндекс после DaData («dadata+yandex», 18.09): подпись по
          условиям карт, как у OSM. Слова «точка дома» отсюда убраны — их
          даёт степень ниже. */}
      {geo?.provider?.includes("+yandex") && geo.status === "exact" && (
        <span className="card-address__attribution">
          {" "}
          · © Яндекс
        </span>
      )}
      {/* СТЕПЕНЬ ТОЧКИ — ТОЛЬКО ИЗ `precision` (18.09). Хвост «~approx» и
          `kind` экран не читает: приблизительная точка дома, массива и
          центра пункта, записанное место — всё «приблизительная» одним
          словом сервера. Для `none` под записанным адресом ничего не
          добавляется: слова статуса выше уже говорят, почему точки нет. */}
      {geo?.status === "exact" && geo.precision === "exact" && (
        <span className="card-address__attribution" data-precision="exact">
          {" "}
          · точка дома
        </span>
      )}
      {geo?.status === "exact" && geo.precision === "approx" && (
        <span className="card-address__attribution" data-approx="1">
          {" "}
          · точка приблизительная
        </span>
      )}
      {/* ВТОРАЯ ОСЬ — ПРИЧИНА (пакет 6.0а). После степени, словами сервера
          (`RULE_LABEL`): «точка приблизительная · точка улицы, дом не найден».
          Предложение правила (`suggest`) названо предложением здесь же — это
          свойство вердикта, а не отдельный статус, и статуса у него нет. */}
      {geo?.rule_label && (
        <span
          className="card-address__attribution"
          data-rule={geo.rule ?? undefined}
          data-suggest={geo.suggest ? "1" : undefined}
        >
          {" "}
          · {geo.rule_label}
          {geo.suggest ? " — предложение" : ""}
        </span>
      )}
    </Text>
  );
}

/** Варианты карты текстом (12.09) — у строки и у источника карточки одни. */
function Варианты({
  variants,
  keyPrefix,
}: {
  variants: AddressGeo["variants"] | undefined;
  keyPrefix: string;
}) {
  if (!variants || variants.length === 0) return null;
  return (
    <ul className="card-address__variants" aria-label="Варианты карты">
      {variants.map((v, i) => (
        <li key={`${keyPrefix}-${i}`} className="card-address__variant">
          {v.formatted}
        </li>
      ))}
    </ul>
  );
}

export function ClientAddressField({
  clientId,
  convId,
  address,
  source,
  geo,
  evidence,
  candidates,
  editable,
}: {
  clientId: string | undefined;
  convId: string | null;
  address: string | null;
  source: AddressSource | null;
  /** Вердикт карты по строке, из которой записан адрес. */
  geo: AddressGeo | null;
  /** Цитата клиента под записанным адресом. */
  evidence: {
    raw: string;
    parts: AddressCandidate["parts"];
    locality?: string | null;
  } | null;
  /**
   * Строки, которые автоматика НЕ записала: проверяются, удержаны сторожем
   * или второй адрес диалога. Сервер уже отобрал их по уровню.
   */
  candidates: readonly AddressCandidate[];
  editable: boolean;
}) {
  const [правим, setПравим] = useState(false);
  const [черновик, setЧерновик] = useState(address ?? "");
  /* «Адрес неверный» — в два касания: первое открывает подтверждение под
     полем, второе стирает. Стирание обратимо только руками, а кнопка стоит
     рядом с «изменить» — случайное касание обязано быть дешёвым. */
  const [подтверждаемНеверный, setПодтверждаемНеверный] = useState(false);
  const поле = useRef<HTMLInputElement>(null);
  const сохранить = useSetClientAddress(clientId, convId);
  const решить = useResolveAddressCandidate(clientId, convId);
  const неверный = useRejectAutoAddress(clientId, convId);

  useEffect(() => {
    if (правим) поле.current?.focus();
  }, [правим]);

  /* Адрес сменился (коллега исправил, автоматика перезаписала) — вопрос
     «точно неверный?» задан был про прежний текст и снимается. */
  useEffect(() => {
    setПодтверждаемНеверный(false);
  }, [address]);

  function адресНеверный(): void {
    неверный.mutate(undefined, {
      onSuccess: () => {
        setПодтверждаемНеверный(false);
        showToast({ message: "Адрес убран: отмечен как неверный", color: "lp" });
      },
      onError: (e) => {
        setПодтверждаемНеверный(false);
        showToast({
          message:
            e instanceof ApiError ? e.message : "Не получилось убрать адрес",
          color: "red",
        });
      },
    });
  }

  /** Принять предложение правила (`geo.suggest`) — одно нажатие, `replace`. */
  function записатьПредложение(candidateId: string): void {
    решить.mutate(
      { candidateId, decision: "replace" },
      {
        onSuccess: () =>
          showToast({ message: "Адрес записан в карточку", color: "lp" }),
        onError: (e) =>
          showToast({
            message:
              e instanceof ApiError
                ? e.message
                : "Не получилось записать адрес",
            color: "red",
          }),
      },
    );
  }

  function сохранение(): void {
    сохранить.mutate(черновик.trim(), {
      onSuccess: () => {
        setПравим(false);
        showToast({
          message: черновик.trim() ? "Адрес сохранён" : "Адрес убран",
          color: "lp",
        });
      },
      onError: (e) =>
        showToast({
          message:
            e instanceof ApiError ? e.message : "Не получилось сохранить адрес",
          color: "red",
        }),
    });
  }

  /** Единственное решение с экрана — «Не адрес»: адрес пишет автоматика. */
  function неАдрес(candidateId: string): void {
    решить.mutate(
      { candidateId, decision: "reject" },
      {
        onSuccess: () =>
          showToast({ message: "Отмечено: это не адрес", color: "lp" }),
        onError: (e) =>
          showToast({
            message:
              e instanceof ApiError
                ? e.message
                : "Не получилось сохранить решение",
            color: "red",
          }),
      },
    );
  }

  const подпись = подписьИсточника(source, geo);

  return (
    <div className="card-address">
      {правим ? (
        <div className="card-address__edit">
          <TextInput
            ref={поле}
            size="xs"
            label="Адрес выезда"
            placeholder="ул. Ленина 5, кв 3"
            value={черновик}
            maxLength={300}
            onChange={(e) => setЧерновик(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") сохранение();
              if (e.key === "Escape") {
                // Esc отменяет правку адреса и только её: без этого он же
                // закрывал карточку поверх ленты, сбрасывал поиск и режим заметки.
                e.stopPropagation();
                setПравим(false);
              }
            }}
          />
          <div className="card-address__buttons">
            <Button
              size="compact-xs"
              loading={сохранить.isPending}
              onClick={сохранение}
            >
              Сохранить
            </Button>
            <Button
              size="compact-xs"
              variant="subtle"
              onClick={() => setПравим(false)}
            >
              Отмена
            </Button>
          </div>
        </div>
      ) : address ? (
        <>
          <div className="card-address__value">
            <IconPin size={14} />
            <span className="card-address__text">{address}</span>
            {подпись && (
              <Text component="span" fz="xs" c="var(--lc-text-3)">
                {подпись}
              </Text>
            )}
            <button
              type="button"
              className="card-address__copy"
              aria-label="Скопировать адрес"
              title="Скопировать адрес"
              onClick={() => {
                void navigator.clipboard
                  ?.writeText(address)
                  .then(() =>
                    showToast({ message: "Адрес скопирован", color: "lp" }),
                  )
                  .catch(() => {});
              }}
            >
              <IconCopy size={14} />
            </button>
            {editable && (
              <Button
                size="compact-xs"
                variant="subtle"
                onClick={() => {
                  setЧерновик(address);
                  setПравим(true);
                }}
              >
                изменить
              </Button>
            )}
          </div>
          <GeoLine geo={geo} locality={evidence?.locality ?? null} />
          {/* Из чего выбирала карта у источника (пакет 6.0а): тот же список,
              что у строк, — подсказка тому, кто нажмёт «изменить». Записанный
              правилом `exact`/`approx` адрес вариантов не несёт; принятое
              человеком предложение — несёт. */}
          <Варианты variants={geo?.variants} keyPrefix="source" />
          {/* Доказательство под записанным адресом: по нему мастер сверяет,
              туда ли едет, — особенно когда адрес поставила автоматика. Части,
              названные ПОСЛЕ записи («2 подъезд, домофон 1234»), в текст поля
              не попадают по замыслу — значит показываются здесь (ревью 11.09). */}
          {evidence && (
            <blockquote className="card-address__quote">
              {evidence.raw}
              {частиНеВАдресе(evidence.parts, address) && (
                <span className="card-address__quote-parts">
                  {" "}
                  · {частиНеВАдресе(evidence.parts, address)}
                </span>
              )}
            </blockquote>
          )}
          {/* «АДРЕС НЕВЕРНЫЙ» — ТОЛЬКО ПОД АДРЕСОМ АВТОМАТИКИ (`source: auto`).
              Под набранным руками или принятым человеком (`dialog`) кнопки
              нет: там спорят два человека, и спор решается через «изменить»
              — сервер на такое отвечает 409. Подтверждение встроено в поле, а
              не всплывает окном: вопрос задаётся про этот адрес и стоит под
              ним. */}
          {editable && source === "auto" && !подтверждаемНеверный && (
            <div className="card-address__buttons">
              <Button
                size="compact-xs"
                variant="subtle"
                color="red"
                onClick={() => setПодтверждаемНеверный(true)}
              >
                Адрес неверный
              </Button>
            </div>
          )}
          {editable && source === "auto" && подтверждаемНеверный && (
            <div
              className="card-address__buttons"
              role="group"
              aria-label="Подтверждение: адрес неверный"
            >
              <Text component="span" fz="xs" c="var(--lc-text-2)">
                Убрать адрес из карточки? Автоматика сюда его больше не
                поставит.
              </Text>
              <Button
                size="compact-xs"
                color="red"
                loading={неверный.isPending}
                onClick={адресНеверный}
              >
                Да, неверный
              </Button>
              <Button
                size="compact-xs"
                variant="subtle"
                disabled={неверный.isPending}
                onClick={() => setПодтверждаемНеверный(false)}
              >
                Отмена
              </Button>
            </div>
          )}
        </>
      ) : (
        editable && (
          <Button
            size="compact-xs"
            variant="subtle"
            leftSection={<IconPin size={14} />}
            onClick={() => {
              setЧерновик("");
              setПравим(true);
            }}
          >
            указать адрес
          </Button>
        )
      )}

      {/*
        СТРОКИ ИЗ ПЕРЕПИСКИ — ЗДЕСЬ ЖЕ, ВПЛОТНУЮ К ПОЛЮ. Отодвинуть их вниз
        блока значило бы дать диспетчеру продиктовать мастеру старый адрес,
        не увидев, что клиент назвал другой. Под заполненной карточкой это
        «Также назван»: второй адрес диалога, между местами автоматика не
        переезжает — решает человек кнопкой «изменить».
      */}
      {candidates.map((c) => {
        const подтверждено = c.geo?.status === "exact" && c.geo.formatted;
        const заголовок = address
          ? c.kind === "place"
            ? "Также названо место"
            : "Также назван"
          : c.kind === "place"
            ? "Место из переписки"
            : "Из переписки";
        return (
          <div key={c.id} className="card-address__suggest">
            <Text
              component="p"
              fz="xs"
              c="var(--lc-text-3)"
              className="card-address__suggest-title"
            >
              {заголовок}
            </Text>
            <div className="card-address__suggest-value">
              {подтверждено ? c.geo!.formatted : c.value}
              {частиСтрокой(c.parts) && (
                <Text component="span" fz="xs" c="var(--lc-text-3)">
                  {" "}
                  · {частиСтрокой(c.parts)}
                </Text>
              )}
            </div>
            <GeoLine
              geo={c.geo}
              locality={c.locality}
              kind={c.kind}
              street={c.street}
            />
            {/* Фраза клиента целиком: по ней и сверяют. */}
            <blockquote className="card-address__quote">{c.raw}</blockquote>
            {/*
              ВАРИАНТЫ КАРТЫ (просьба владельца 12.09: «писал варианты, если не
              понимает точно, где клиент») — текстом, без кнопок и без
              `editable`: карта не выбрала один дом, автоматика такое не
              пишет, а тому, кто нажмёт «изменить», найденные адреса —
              подсказка, что набрать.
            */}
            <Варианты variants={c.geo?.variants} keyPrefix={c.id} />
            {editable && (
              <div className="card-address__buttons">
                {/* КНОПКА ЗАПИСИ — там, где адрес иначе не запишется никем:
                    предложение правила (`suggest`, пакет 6.0а) и строка со
                    степенью, которую автоматика сама не пишет (`writable`,
                    проверка 24.09). У прочих строк кнопки нет — адрес пишет
                    автоматика (18.09). */}
                {(c.geo?.suggest || c.writable) && (
                  <Button
                    size="compact-xs"
                    loading={решить.isPending}
                    // Видимая подпись — префикс доступного имени (WCAG 2.5.3):
                    // голосовое управление ищет кнопку по словам с экрана.
                    aria-label={`Записать в карточку: ${c.geo?.formatted ?? c.value}`}
                    onClick={() => записатьПредложение(c.id)}
                  >
                    Записать в карточку
                  </Button>
                )}
                <Button
                  size="compact-xs"
                  variant="subtle"
                  loading={решить.isPending}
                  aria-label={`Это не адрес: ${c.value}`}
                  onClick={() => неАдрес(c.id)}
                >
                  Не адрес
                </Button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
