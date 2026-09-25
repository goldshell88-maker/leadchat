import { useState } from "react";
import { Button, Text, TextInput, Textarea } from "@mantine/core";
import type {
  AddressDetectSettings,
  ParseRuleEffective,
  RulePolicy,
} from "./api";

/**
 * Политика правил привязки, правила разбора и свои адреса (проверка 24.09).
 *
 * ЗАЧЕМ. Сервер отдавал и принимал эти настройки с 20.09, а на экране их не
 * было: уведомление лестницы «правило будет поднято до exact — остановить,
 * сохранив настройку со строкой «правило=approx»» было невыполнимо, стоп-классы
 * речи и эхо мастерской включить было нечем.
 *
 * ⚠ СОХРАНЕНИЕ — ВСЕЙ СТРОКОЙ И ВСЕГДА, ДАЖЕ БЕЗ ПРАВОК. Вето на подъём — это
 * повторное сохранение той же строки: значение уже вписала автоматика, а
 * задача лестницы узнаёт человека по факту подачи `rule_policy`
 * (`human_set_rules`), и все правила сохранённой строки становятся его.
 * Поэтому строка видна целиком, а не собирается из выпадающих списков за
 * спиной у человека: он видит, за какие правила ручается. Кнопка шлёт ровно
 * своё поле — `{rule_policy}`, — иначе в журнал попала бы подача чужих полей.
 */

type Save = (patch: {
  rule_policy?: string;
  parse_rules?: string;
  own_addresses?: string;
}) => void;

const POLICY_LABEL: Record<RulePolicy, string> = {
  off: "выключено",
  shadow: "тень — только замер",
  suggest: "предложение оператору",
  approx: "пишет с пометкой «приблизительно»",
  exact: "пишет как точный адрес",
};

const PARSE_STATE_LABEL: Record<ParseRuleEffective["state"], string> = {
  on: "включено",
  off: "выключено",
  shadow: "тень — только замер",
};

export function AddressRuleSettings({
  value,
  onSave,
  pending,
}: {
  value: AddressDetectSettings;
  onSave: Save;
  pending: boolean;
}) {
  return (
    <>
      <div className="accounts-page__field">
        <Text fz="sm" c="var(--lc-text-2)" id="rule-policy-title">
          Правила привязки адреса
        </Text>
        <RuleList
          labelledBy="rule-policy-title"
          rows={(value.rule_policy_effective ?? []).map((r) => ({
            rule: r.rule,
            label: r.label,
            state: POLICY_LABEL[r.policy] ?? r.policy,
          }))}
        />
        {/* Ключ — сохранённое значение: после сохранения (своего или чужого)
            черновик начинается заново с того, что лежит на сервере. */}
        <SettingLine
          key={value.rule_policy}
          id="rule-policy-line"
          label="Перекрытия политики"
          initial={value.rule_policy}
          placeholder="suburb=suggest, street_point=approx"
          saveLabel="Сохранить политику"
          pending={pending}
          onSave={(text) => onSave({ rule_policy: text })}
        />
        <Text fz="xs" c="var(--lc-text-3)">
          «Правило=значение» через запятую; значения: off · shadow · suggest ·
          approx · exact. Пусто — политика по умолчанию. Сохранение закрепляет
          за вами все правила строки: автоматика их больше не поднимает, только
          понижает при ошибках. Чтобы остановить объявленный подъём до exact,
          сохраните строку — даже не меняя её.
        </Text>
      </div>

      <div className="accounts-page__field">
        <Text fz="sm" c="var(--lc-text-2)" id="parse-rules-title">
          Правила разбора: что не считать адресом
        </Text>
        <RuleList
          labelledBy="parse-rules-title"
          rows={(value.parse_rules_effective ?? []).map((r) => ({
            rule: r.rule,
            label: r.label,
            state: PARSE_STATE_LABEL[r.state] ?? r.state,
          }))}
        />
        <SettingLine
          key={value.parse_rules}
          id="parse-rules-line"
          label="Перекрытия правил разбора"
          initial={value.parse_rules}
          placeholder="STOP_LATIN_BRAND=shadow"
          saveLabel="Сохранить правила разбора"
          pending={pending}
          onSave={(text) => onSave({ parse_rules: text })}
        />
        <Text fz="xs" c="var(--lc-text-3)">
          «Правило=on|off|shadow» через запятую. Пусто — значения по умолчанию.
          Новое правило сначала включайте в тень: оно только замеряет, что снял
          бы, и в разбор не вмешивается.
        </Text>
      </div>

      <div className="accounts-page__field">
        <SettingLine
          key={value.own_addresses}
          id="own-addresses-line"
          label="Свои адреса: мастерская, офис, пункт приёма"
          initial={value.own_addresses}
          placeholder={"ул Невская, 7а\nпр Мира, 12"}
          saveLabel="Сохранить свои адреса"
          multiline
          pending={pending}
          onSave={(text) => onSave({ own_addresses: text })}
        />
        <Text fz="xs" c="var(--lc-text-3)">
          Улица и дом — по строке или через «;». Клиент, повторивший такой
          адрес, не получает его в карточку, пока включено правило разбора
          «клиент повторяет наш адрес».
        </Text>
        <Text fz="xs" c="var(--lc-text-2)" data-testid="own-addresses-auto">
          Автоматика нашла в наших сообщениях:{" "}
          {value.own_addresses_auto?.trim()
            ? value.own_addresses_auto
            : "пока ничего"}
        </Text>
      </div>
    </>
  );
}

function RuleList({
  labelledBy,
  rows,
}: {
  labelledBy: string;
  rows: { rule: string; label: string; state: string }[];
}) {
  if (rows.length === 0) return null;
  return (
    <dl className="accounts-page__rules" aria-labelledby={labelledBy}>
      {rows.map((r) => (
        <div key={r.rule} className="accounts-page__rule">
          <dt>
            {r.label} <code>{r.rule}</code>
          </dt>
          <dd>{r.state}</dd>
        </div>
      ))}
    </dl>
  );
}

function SettingLine({
  id,
  label,
  initial,
  placeholder,
  saveLabel,
  multiline = false,
  pending,
  onSave,
}: {
  id: string;
  label: string;
  initial: string;
  placeholder: string;
  saveLabel: string;
  multiline?: boolean;
  pending: boolean;
  onSave: (text: string) => void;
}) {
  const [draft, setDraft] = useState(initial ?? "");
  const field = multiline ? (
    <Textarea
      id={id}
      label={label}
      value={draft}
      placeholder={placeholder}
      autosize
      minRows={2}
      maxLength={2000}
      onChange={(e) => setDraft(e.currentTarget.value)}
    />
  ) : (
    <TextInput
      id={id}
      label={label}
      value={draft}
      placeholder={placeholder}
      maxLength={2000}
      onChange={(e) => setDraft(e.currentTarget.value)}
    />
  );
  return (
    <>
      {field}
      <div>
        <Button
          size="xs"
          variant="outline"
          disabled={pending}
          onClick={() => onSave(draft.trim())}
        >
          {saveLabel}
        </Button>
      </div>
    </>
  );
}
