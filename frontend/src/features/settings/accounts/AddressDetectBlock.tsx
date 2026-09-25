import { Button, SegmentedControl, Skeleton, Switch, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import { ExternalLink } from "@/shared/ui/ExternalLink";
import { showToast } from "@/shared/ui/toast";
import { AddressRuleSettings } from "./AddressRuleSettings";
import {
  addressDetectKey,
  fetchAddressDetect,
  saveAddressDetect,
  type AddressDetectSettings,
} from "./api";

/**
 * «Адреса в переписке» — разбор, проверка по карте и автозапись (11.09).
 *
 * ЗАЧЕМ БЛОК НА ЭКРАНЕ. До 11.09 ключей `address_detect.*` нельзя было
 * достичь ни из API, ни из панели: выключатель автозаписи существовал только
 * как SQL. Раз автоматика пишет в карточку сама — по подтверждению карты, но
 * сама, — снять её обязано быть одним нажатием руководителя, а не выкаткой.
 *
 * ПЕРЕКЛЮЧАТЕЛИ ОДНИМ БЛОКОМ, как у телефона: «распознавать» без
 * «проверять» — рабочее сочетание; «писать сразу» без «проверять» —
 * бессмыслица, и увидеть это надо на экране, а не после сохранения.
 *
 * С 18.09 ЗДЕСЬ ЖЕ — АВТОПРИВЯЗКА (решение владельца: «оператор ничего не
 * подтверждает, у него остаётся только „изменить“»). Тумблер автопривязки
 * зависит от автозаписи — без «писать в карточку» решать нечего.
 *
 * Вопроса клиенту об адресе здесь нет и не будет: LeadChat клиентам сам не
 * пишет (владелец 20.09 и 24.09), сервер такой тумблер не включает.
 *
 * ПРАВО — `settings:manage`, администратор.
 */
/**
 * Почему у помощника нет ключа — начало подписи (проверка 24.09). С 18.09
 * ключи живут на шлюзе внешних API в Амстердаме (docs/46), и совет «задайте
 * DADATA_API_KEY на сервере» вёл править .env LeadChat, который их больше не
 * читает. «Шлюза нет» и «у шлюза нет ключа» чинятся в разных местах.
 */
function noKeyReason(value: AddressDetectSettings, what: string): string {
  return value.gateway_configured === false
    ? "Шлюз внешних API не настроен (GATEWAY_URL и GATEWAY_TOKEN в .env LeadChat)"
    : `У шлюза внешних API нет ключа ${what}`;
}

export function AddressDetectBlock() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: addressDetectKey,
    queryFn: fetchAddressDetect,
  });
  const save = useMutation({
    mutationFn: saveAddressDetect,
    onSuccess: (data) => {
      qc.setQueryData(addressDetectKey, data);
      showToast({ title: "Сохранено", color: "lp" });
    },
    onError: (e) =>
      showToast({
        title: "Настройка не сохранилась",
        // Отказ сервера с причиной (текст вопроса без слова об адресе)
        // показывается словами сервера; сетевая ошибка — общим советом.
        message:
          e instanceof ApiError
            ? e.message
            : "Проверьте соединение и переключите ещё раз",
        color: "red",
      }),
  });

  if (q.isPending) {
    return (
      <section className="accounts-page__block">
        <Skeleton height={18} width={220} radius="var(--lc-radius-sm)" />
        <Skeleton height={34} width={320} radius="var(--lc-radius-sm)" />
      </section>
    );
  }

  if (q.isError) {
    return (
      <section className="accounts-page__block">
        <Text fz="sm" c="var(--lc-danger-text)" role="alert">
          Настройки адресов не загрузились. Сами настройки при этом не
          изменились — система работает так, как её настроили в прошлый раз
        </Text>
        <Button variant="outline" size="xs" onClick={() => void q.refetch()}>
          Повторить
        </Button>
      </section>
    );
  }

  const value: AddressDetectSettings = q.data;
  const yandexБезКлюча =
    value.provider !== "nominatim" && !value.yandex_key_present;
  const yandexВключён =
    value.provider !== "nominatim" && value.yandex_key_present;

  return (
    <section
      className="accounts-page__block"
      aria-labelledby="address-detect-title"
    >
      <Text id="address-detect-title" fw={600} fz="md" c="var(--lc-text-1)">
        Адреса в переписке
      </Text>

      <Switch
        checked={value.enabled}
        disabled={save.isPending}
        onChange={(e) => save.mutate({ enabled: e.currentTarget.checked })}
        label="Распознавать адреса в тексте сообщений"
        description={
          value.enabled
            ? "Улицу и дом, названные клиентом, система находит сама и показывает под полем адреса"
            : "Адрес из текста останется незамеченным — его вписывают руками"
        }
      />

      <Switch
        checked={value.geo_enabled}
        disabled={!value.enabled || save.isPending}
        onChange={(e) => save.mutate({ geo_enabled: e.currentTarget.checked })}
        label="Проверять найденный адрес по карте"
        description={
          value.geo_enabled
            ? "Дом ищется на карте в городе объявления; подтверждённый адрес показывается в формате карт"
            : "Предложения показываются как есть, без проверки и без строки в формате карт"
        }
      />

      <div className="accounts-page__field">
        <Text fz="sm" c="var(--lc-text-2)" id="address-levels-label">
          Какие ещё находки показывать оператору
        </Text>
        <SegmentedControl
          aria-labelledby="address-levels-label"
          size="xs"
          value={value.levels === "" ? "none" : value.levels}
          disabled={!value.enabled || save.isPending}
          onChange={(v) => save.mutate({ levels: v === "none" ? "" : v })}
          data={[
            { value: "A", label: "Только с «ул.», «дом»" },
            { value: "AB", label: "+ из разговора об адресе" },
            { value: "ABC", label: "Все" },
            { value: "none", label: "Только подтверждённые картой" },
          ]}
        />
        {/*
          Подпись говорит, что уровень ограничивает ТОЛЬКО ПОКАЗ (проверка
          24.09). Здесь стояло «Автозапись берёт только первые», а автозапись
          пишет находку любого уровня, если карта подтвердила дом (бой: у
          карточек автоматики 1 058 строк уровня C). И подтверждённые картой
          строки оператор видит при любом выборе — «Никакие» их не прятал.
        */}
        <Text fz="xs" c="var(--lc-text-3)">
          Подтверждённые картой адреса видны всегда, и автозапись пишет их любого
          уровня; выбор добавляет к ним находки без подтверждения. Замер боя:
          находки с названным типом улицы — 3 613 в месяц и почти без ложных;
          «все» — ещё 2 619, половина из них не адрес.
        </Text>
      </div>

      <Switch
        checked={value.dadata_enabled}
        disabled={!value.enabled || !value.geo_enabled || save.isPending}
        onChange={(e) =>
          save.mutate({ dadata_enabled: e.currentTarget.checked })
        }
        label="Сначала спрашивать DaData (справочник ФИАС)"
        description={
          value.dadata_enabled
            ? "Государственный справочник адресов с координатами: находит кварталы, микрорайоны, сокращённые и с опечаткой улицы; карта ниже — только если DaData дом не подтвердила"
            : "Проверка начинается сразу с карты ниже"
        }
      />
      {value.dadata_enabled && !value.dadata_key_present && (
        <Text fz="xs" c="var(--lc-warn-text)" role="alert">
          {noKeyReason(value, "DaData")} — справочник не спрашивается, проверяет
          только карта
        </Text>
      )}
      {value.dadata_enabled && value.dadata_key_present && (
        <Text fz="xs" c="var(--lc-text-2)" data-testid="dadata-usage">
          DaData сегодня: {value.dadata_used_today} из{" "}
          {value.dadata_daily_limit === null
            ? "без потолка"
            : value.dadata_daily_limit}
          {value.dadata_daily_limit !== null &&
          value.dadata_used_today >= value.dadata_daily_limit
            ? " — потолок выбран, до полуночи проверяет только карта"
            : ""}
        </Text>
      )}

      <Switch
        checked={value.llm_enabled}
        disabled={!value.enabled || save.isPending}
        onChange={(e) => save.mutate({ llm_enabled: e.currentTarget.checked })}
        label="Бесплатная ИИ-модель перечитывает адрес (OpenRouter, Groq, Mistral)"
        description={
          value.llm_enabled
            ? "Когда правила адрес не нашли или карта его не подтвердила, реплики клиента за сутки уходят бесплатной модели; её разбор проходит те же проверки — сторож цитаты и карту"
            : "Адрес читают только правила и карта"
        }
      />
      {value.llm_enabled && !value.llm_key_present && (
        <Text fz="xs" c="var(--lc-warn-text)" role="alert">
          {noKeyReason(value, "ни одного читателя: OpenRouter, Groq, Mistral")} —
          модель не спрашивается
        </Text>
      )}
      {value.llm_enabled && value.llm_key_present && (
        <Text fz="xs" c="var(--lc-text-2)" data-testid="llm-usage">
          Модель сегодня: {value.llm_used_today} из{" "}
          {value.llm_daily_limit === null
            ? "без потолка"
            : value.llm_daily_limit}
          {value.llm_daily_limit !== null &&
          value.llm_used_today >= value.llm_daily_limit
            ? " — потолок выбран, до полуночи читают только правила"
            : ""}
        </Text>
      )}

      <div className="accounts-page__field">
        <Text fz="sm" c="var(--lc-text-2)" id="address-provider-label">
          Какой картой проверять
        </Text>
        <SegmentedControl
          aria-labelledby="address-provider-label"
          size="xs"
          value={value.provider}
          disabled={!value.enabled || !value.geo_enabled || save.isPending}
          onChange={(v) =>
            save.mutate({ provider: v as AddressDetectSettings["provider"] })
          }
          data={[
            { value: "nominatim", label: "OpenStreetMap" },
            { value: "osm_then_yandex", label: "OSM, потом Яндекс" },
            { value: "yandex", label: "Яндекс" },
          ]}
        />
        <Text fz="xs" c="var(--lc-text-3)">
          «OSM, потом Яндекс» — Яндекс спрашивается только там, где
          OpenStreetMap дом не нашёл: бесплатная тысяча запросов в сутки
          расходуется в несколько раз медленнее.
        </Text>
        {yandexБезКлюча && (
          <Text fz="xs" c="var(--lc-warn-text)" role="alert">
            {noKeyReason(value, "Яндекс Геокодера")} — проверка по Яндексу не
            работает, пока ключ не появится
          </Text>
        )}
        {yandexВключён && (
          <Text fz="xs" c="var(--lc-text-2)" data-testid="yandex-usage">
            Яндекс сегодня: {value.yandex_used_today} из{" "}
            {value.yandex_daily_limit === null
              ? "без потолка"
              : value.yandex_daily_limit}
            {value.yandex_daily_limit !== null &&
            value.yandex_used_today >= value.yandex_daily_limit
              ? " — потолок выбран, до полуночи проверяет только OpenStreetMap"
              : ""}
          </Text>
        )}
      </div>

      <Switch
        checked={value.suggest_enabled}
        disabled={!value.enabled || !value.geo_enabled || save.isPending}
        onChange={(e) =>
          save.mutate({ suggest_enabled: e.currentTarget.checked })
        }
        label="Подсказка Яндекса при опечатке или сокращении"
        description={
          value.suggest_enabled
            ? "Карты дом не нашли — спрашиваем Геосаджест, исправленную улицу проверяем картой заново"
            : "Опечатка в улице останется «карта не нашла» — оператор разберётся сам"
        }
      />
      {value.suggest_enabled && !value.suggest_key_present && (
        <Text fz="xs" c="var(--lc-warn-text)" role="alert">
          {noKeyReason(value, "Геосаджеста")} — подсказка не работает
        </Text>
      )}
      {value.suggest_enabled && value.suggest_key_present && (
        <Text fz="xs" c="var(--lc-text-2)" data-testid="suggest-usage">
          Подсказки сегодня: {value.suggest_used_today} из{" "}
          {value.suggest_daily_limit === null
            ? "без потолка"
            : value.suggest_daily_limit}
        </Text>
      )}

      <Switch
        checked={value.speller_enabled}
        disabled={!value.enabled || !value.geo_enabled || save.isPending}
        onChange={(e) =>
          save.mutate({ speller_enabled: e.currentTarget.checked })
        }
        label="Спеллер Яндекса: опечатка в названии улицы"
        description={
          value.speller_enabled
            ? "Карта улицу не нашла — Спеллер правит опечатку («Ленена» → «Ленина»), исправленную улицу проверяем картой заново. Без ключа, 10 000 в сутки"
            : "Опечатка в улице останется «карта не нашла»"
        }
      />
      {value.speller_enabled && (
        <Text fz="xs" c="var(--lc-text-2)" data-testid="speller-usage">
          Спеллер сегодня: {value.speller_used_today} из{" "}
          {value.speller_daily_limit === null
            ? "без потолка"
            : value.speller_daily_limit}{" "}
          · Проверка правописания:{" "}
          <ExternalLink url="https://yandex.ru/dev/speller/">Яндекс.Спеллер</ExternalLink>
        </Text>
      )}

      <Switch
        checked={value.ahunter_enabled}
        disabled={!value.enabled || !value.geo_enabled || save.isPending}
        onChange={(e) =>
          save.mutate({ ahunter_enabled: e.currentTarget.checked })
        }
        label="Ahunter: второй справочник адресов (ГАР) без потолка"
        description={
          value.ahunter_enabled
            ? "Карты дом не нашли — Ahunter подсказывает адрес по справочнику ГАР (кварталы, корпуса, СНТ), подсказку проверяем картой заново. Без ключа и без суточного потолка"
            : "Отказ карт останется отказом, второй справочник не спрашивается"
        }
      />
      {value.ahunter_enabled && (
        <Text fz="xs" c="var(--lc-text-2)" data-testid="ahunter-usage">
          Ahunter сегодня: {value.ahunter_used_today}
        </Text>
      )}

      <Switch
        checked={value.autofill}
        disabled={!value.enabled || !value.geo_enabled || save.isPending}
        onChange={(e) => save.mutate({ autofill: e.currentTarget.checked })}
        label="Писать подтверждённый картой адрес прямо в карточку"
        description={
          value.autofill
            ? "Пустая карточка заполнится сама, когда карта подтвердила дом и все сторожа сошлись. Ошибка карты станет адресом, по которому поедет мастер"
            : "Оператор увидит найденный адрес с кнопками «Записать в карточку» и «Не адрес» и решит сам"
        }
      />

      <Switch
        checked={value.auto_decide}
        disabled={
          !value.enabled ||
          !value.geo_enabled ||
          !value.autofill ||
          save.isPending
        }
        onChange={(e) => save.mutate({ auto_decide: e.currentTarget.checked })}
        label="Привязывать адрес автоматически: точку, приблизительную точку или текст клиента"
        description={
          value.auto_decide
            ? "Система решает сама и помечает степень: точная точка · приблизительная (улица, массив, центр пункта) · без точки (улица словами клиента, когда карта её знает). Оператору остаётся «Изменить»"
            : "Сама в карточку попадает только точка, которую карта подтвердила (дом, улица или пункт); адрес без точки оператор вписывает сам через «Изменить»"
        }
      />

      <Text fz="xs" c="var(--lc-text-3)">
        Адрес, набранный руками, автоматика не трогает никогда; свой адрес она
        уточняет только лучшей степенью того же места. Стёртый оператором адрес
        автоматика не возвращает.
      </Text>

      <AddressRuleSettings
        value={value}
        onSave={(patch) => save.mutate(patch)}
        pending={save.isPending}
      />
    </section>
  );
}
