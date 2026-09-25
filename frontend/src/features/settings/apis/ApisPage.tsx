import { useState } from "react";
import {
  Badge,
  Button,
  Group,
  Modal,
  NumberInput,
  Skeleton,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  VisuallyHidden,
} from "@mantine/core";
import { ApiError } from "@/shared/api/http";
import { ExternalLink } from "@/shared/ui/ExternalLink";
import { PageHeader } from "@/shared/ui/PageHeader";
import { showToast } from "@/shared/ui/toast";
import {
  useAddApi,
  useApis,
  useCheckApi,
  useDeleteApi,
  usePatchApi,
  type ApiEntryInput,
  type ApiRow,
  type ApiState,
} from "./apisApi";
import "./apis.css";

/**
 * «Внешние сервисы» — монитор API (владелец 16.09: «монитор по API, где
 * можно вручную обновлять, добавлять API и смотреть лимиты»).
 *
 * ОДНА ТАБЛИЦА НА ВСЁ. Ключ есть ли, включён ли, потолок, сколько выбрано
 * сегодня, состояние и когда сервер последний раз до сервиса достучался.
 * Встроенные строки считает сервер — их не редактируют здесь: ключи живут в
 * окружении, переключатели — в «Адресах в переписке». Свои строки владелец
 * заводит и правит сам: демо-ключ 2ГИС на тысячу запросов, аккаунт, который
 * ещё не подключён, — чтобы лимиты были перед глазами, а не в переписке.
 *
 * ПРОВЕРКА — ПО НАЖАТИЮ. Это поход наружу с боевого сервера; делать его сам
 * по расписанию монитор не будет. Итог живёт сутки и показан с временем.
 */

const STATE_LABEL: Record<ApiState, { text: string; color: string }> = {
  ok: { text: "Работает", color: "green" },
  off: { text: "Выключен", color: "gray" },
  no_key: { text: "Нет ключа", color: "yellow" },
  limit: { text: "Потолок выбран", color: "orange" },
  down: { text: "Не отвечает", color: "red" },
  unknown: { text: "Не проверялся", color: "gray" },
};

/**
 * Строка воронки адресов (18.09) — не сервис, а недельный замер «сколько
 * адресов из переписки дошло до карточки»: у неё нет адреса пробы, а слова
 * состояния свои — «Не отвечает» про падение доли читалось бы как обрыв сети.
 */
const FUNNEL_KEY = "address_funnel";
const FUNNEL_LABEL: Partial<Record<ApiState, { text: string; color: string }>> =
  {
    ok: { text: "Ровно", color: "green" },
    down: { text: "Падение", color: "red" },
    unknown: { text: "Нет замера", color: "gray" },
  };

function когда(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function лимитом(row: ApiRow): string {
  const куски: string[] = [];
  if (row.daily_limit !== null) куски.push(`${row.daily_limit} в сутки`);
  if (row.monthly_limit !== null) куски.push(`${row.monthly_limit} в месяц`);
  return куски.length ? куски.join(", ") : "без потолка";
}

function расходом(row: ApiRow): string {
  if (row.used_today === null) return "—";
  if (row.daily_limit === null) return String(row.used_today);
  return `${row.used_today} из ${row.daily_limit}`;
}

const ПУСТАЯ: ApiEntryInput = {
  name: "",
  purpose: "",
  url: "",
  docs_url: "",
  daily_limit: null,
  monthly_limit: null,
  notes: "",
};

export function ApisPage() {
  const q = useApis();
  const add = useAddApi();
  const patch = usePatchApi();
  const remove = useDeleteApi();
  const check = useCheckApi();
  const [форма, setФорма] = useState<ApiEntryInput | null>(null);
  const [правим, setПравим] = useState<ApiRow | null>(null);
  const [проверяем, setПроверяем] = useState<string | null>(null);
  const [всеИдут, setВсеИдут] = useState(false);

  const ошибкой = (e: unknown, что: string) =>
    showToast({
      message: e instanceof ApiError ? e.message : что,
      color: "red",
    });

  function проверить(key: string) {
    setПроверяем(key);
    check.mutate(key, {
      onSettled: () => setПроверяем(null),
      onSuccess: (data) =>
        showToast({
          message: data.checked.ok
            ? `Ответил: ${data.checked.status} за ${data.checked.ms} мс`
            : `Не ответил: ${data.checked.error ?? data.checked.status}`,
          color: data.checked.ok ? "lp" : "red",
        }),
      onError: (e) => ошибкой(e, "Проверка не удалась"),
    });
  }

  async function проверитьВсе() {
    if (!q.data || всеИдут) return;
    setВсеИдут(true);
    let живы = 0;
    let легли = 0;
    let без_адреса = 0;
    for (const row of q.data.items) {
      if (row.key === FUNNEL_KEY) continue; // замер, а не сервис
      if (!row.url) {
        без_адреса += 1;
        continue;
      }
      setПроверяем(row.key);
      try {
        const data = await check.mutateAsync(row.key);
        if (data.checked.ok) живы += 1;
        else легли += 1;
      } catch (e) {
        легли += 1;
        ошибкой(e, `Проверка ${row.name} не удалась`);
      }
    }
    setПроверяем(null);
    setВсеИдут(false);
    const куски = [`отвечают: ${живы}`, `не отвечают: ${легли}`];
    if (без_адреса) куски.push(`без адреса: ${без_адреса}`);
    showToast({
      message: `Проверено. ${куски.join(", ")}`,
      color: легли ? "yellow" : "lp",
    });
  }

  return (
    <section className="apis" aria-label="Внешние сервисы">
      <PageHeader
        title="Внешние сервисы"
        description="Кто подключён, какой потолок, сколько выбрано сегодня и отвечает ли сервис"
        actions={
          <Group gap="xs">
            <Button
              className="lc-btn"
              variant="subtle"
              size="xs"
              onClick={() => void q.refetch()}
              loading={q.isFetching && !всеИдут}
              disabled={!q.data}
              title="Перечитать счётчики и состояния без походов наружу"
            >
              Обновить
            </Button>
            <Button
              className="lc-btn"
              variant="outline"
              size="xs"
              onClick={() => void проверитьВсе()}
              loading={всеИдут}
              disabled={!q.data || всеИдут}
            >
              Проверить все
            </Button>
            <Button
              className="lc-btn"
              size="xs"
              onClick={() => setФорма({ ...ПУСТАЯ })}
            >
              Добавить API
            </Button>
          </Group>
        }
      />

      {q.isPending && (
        <Stack gap="xs">
          <Skeleton height={34} radius="var(--lc-radius-sm)" />
          <Skeleton height={34} radius="var(--lc-radius-sm)" />
          <Skeleton height={34} radius="var(--lc-radius-sm)" />
        </Stack>
      )}
      {q.isError && (
        <Text fz="sm" c="var(--lc-danger-text)" role="alert">
          Список сервисов не загрузился.{" "}
          <Button
            variant="subtle"
            size="compact-xs"
            onClick={() => void q.refetch()}
          >
            Повторить
          </Button>
        </Text>
      )}

      {q.data && (
        <div className="apis__scroll">
          <table className="lc-table apis__table lc-table--cards">
            <thead>
              <tr>
                <th scope="col">Сервис</th>
                <th scope="col">Зачем</th>
                <th scope="col">Ключ</th>
                <th scope="col">Потолок</th>
                <th scope="col">Сегодня</th>
                <th scope="col">Состояние</th>
                <th scope="col">Проверка</th>
                <th scope="col">
                  <VisuallyHidden>Действия</VisuallyHidden>
                </th>
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((row) => {
                const состояние =
                  (row.key === FUNNEL_KEY && FUNNEL_LABEL[row.state]) ||
                  STATE_LABEL[row.state];
                return (
                  <tr key={row.key} data-kind={row.kind} data-state={row.state}>
                    <td data-label="Сервис">
                      <Text fz="sm" fw={600} c="var(--lc-text-1)">
                        {row.name}
                      </Text>
                      {row.docs_url && (
                        <ExternalLink url={row.docs_url} className="apis__link">
                          документация
                        </ExternalLink>
                      )}
                    </td>
                    <td data-label="Зачем">
                      <Text fz="xs" c="var(--lc-text-2)">
                        {row.purpose}
                      </Text>
                      {row.notes && (
                        <Text fz="xs" c="var(--lc-text-3)">
                          {row.notes}
                        </Text>
                      )}
                    </td>
                    <td data-label="Ключ">
                      {row.kind === "custom" ? (
                        <Text fz="xs" c="var(--lc-text-3)">
                          —
                        </Text>
                      ) : row.key_present === null ? (
                        <Text fz="xs" c="var(--lc-text-3)">
                          не нужен
                        </Text>
                      ) : row.key_present ? (
                        <Text fz="xs" c="var(--lc-success-text)">
                          задан
                        </Text>
                      ) : (
                        <Text
                          fz="xs"
                          c="var(--lc-warn-text)"
                          title={row.env_var ?? ""}
                        >
                          нет · {row.env_var}
                        </Text>
                      )}
                    </td>
                    <td data-label="Потолок">
                      <Text fz="xs">{лимитом(row)}</Text>
                    </td>
                    <td data-label="Сегодня">
                      <Text fz="xs" className="lc-num">
                        {расходом(row)}
                      </Text>
                    </td>
                    <td data-label="Состояние">
                      <Badge color={состояние.color} variant="light" size="sm">
                        {состояние.text}
                      </Badge>
                      {row.state_note && (
                        <Text fz="xs" c="var(--lc-text-3)">
                          {row.state_note}
                        </Text>
                      )}
                    </td>
                    <td data-label="Проверка">
                      {row.checked ? (
                        <Text
                          fz="xs"
                          c={
                            row.checked.ok
                              ? "var(--lc-text-2)"
                              : "var(--lc-danger-text)"
                          }
                        >
                          {row.checked.ok
                            ? `${row.checked.status} · ${row.checked.ms} мс`
                            : (row.checked.error ??
                              String(row.checked.status ?? "ошибка"))}
                          <br />
                          {когда(row.checked.at)}
                        </Text>
                      ) : (
                        <Text fz="xs" c="var(--lc-text-3)">
                          —
                        </Text>
                      )}
                    </td>
                    <td data-label="">
                      <Group gap={4} wrap="nowrap">
                        <Button
                          variant="subtle"
                          size="compact-xs"
                          onClick={() => проверить(row.key)}
                          loading={проверяем === row.key}
                          disabled={!row.url || всеИдут}
                        >
                          Проверить
                        </Button>
                        {row.editable && (
                          <Button
                            variant="subtle"
                            size="compact-xs"
                            onClick={() => setПравим(row)}
                          >
                            Править
                          </Button>
                        )}
                      </Group>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <Text fz="xs" c="var(--lc-text-3)">
        Встроенные сервисы включаются в «Аккаунты Авито → Адреса в переписке»;
        их ключи лежат на шлюзе внешних API в Амстердаме, а не в настройках
        LeadChat. Проверка — поход с сервера по адресу сервиса, итог хранится
        сутки.
      </Text>

      <Modal
        opened={форма !== null}
        onClose={() => setФорма(null)}
        title="Добавить API"
        centered
      >
        {форма && (
          <EntryForm
            value={форма}
            onChange={setФорма}
            saving={add.isPending}
            onSubmit={() =>
              add.mutate(нормализовать(форма), {
                onSuccess: () => {
                  setФорма(null);
                  showToast({ message: "Сервис добавлен", color: "lp" });
                },
                onError: (e) => ошибкой(e, "Не получилось добавить"),
              })
            }
          />
        )}
      </Modal>

      <Modal
        opened={правим !== null}
        onClose={() => setПравим(null)}
        title={правим ? `Править: ${правим.name}` : ""}
        centered
      >
        {правим && (
          <EntryEditor
            row={правим}
            saving={patch.isPending || remove.isPending}
            onSave={(input, enabled) =>
              patch.mutate(
                {
                  key: правим.key,
                  patch: {
                    ...дляПравки(input),
                    unlimited_daily: input.daily_limit === null,
                    unlimited_monthly: input.monthly_limit === null,
                    enabled,
                  },
                },
                {
                  onSuccess: () => {
                    setПравим(null);
                    showToast({ message: "Сохранено", color: "lp" });
                  },
                  onError: (e) => ошибкой(e, "Не получилось сохранить"),
                },
              )
            }
            onDelete={() =>
              remove.mutate(правим.key, {
                onSuccess: () => {
                  setПравим(null);
                  showToast({ message: "Запись удалена", color: "lp" });
                },
                onError: (e) => ошибкой(e, "Не получилось удалить"),
              })
            }
          />
        )}
      </Modal>
    </section>
  );
}

/** Для создания: пустое поле — null. */
function нормализовать(input: ApiEntryInput): ApiEntryInput {
  return {
    ...input,
    url: input.url?.trim() ? input.url.trim() : null,
    docs_url: input.docs_url?.trim() ? input.docs_url.trim() : null,
  };
}

/** Для правки: пустое поле — пустая строка, сервер читает её как «стереть»;
 *  null в PATCH значит «не трогали». */
function дляПравки(input: ApiEntryInput): ApiEntryInput {
  return {
    ...input,
    url: input.url?.trim() ?? "",
    docs_url: input.docs_url?.trim() ?? "",
  };
}

function EntryForm({
  value,
  onChange,
  saving,
  onSubmit,
}: {
  value: ApiEntryInput;
  onChange: (v: ApiEntryInput) => void;
  saving: boolean;
  onSubmit: () => void;
}) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <Stack gap="sm">
        <TextInput
          label="Название"
          required
          value={value.name}
          onChange={(e) => onChange({ ...value, name: e.currentTarget.value })}
          placeholder="2ГИС Places (демо-ключ)"
        />
        <TextInput
          label="Зачем"
          value={value.purpose}
          onChange={(e) =>
            onChange({ ...value, purpose: e.currentTarget.value })
          }
          placeholder="Организации по названию в Иркутске"
        />
        <TextInput
          label="Адрес для проверки"
          value={value.url ?? ""}
          onChange={(e) => onChange({ ...value, url: e.currentTarget.value })}
          placeholder="https://catalog.api.2gis.com/3.0/items"
          description="Сервер сходит по нему по кнопке «Проверить»; только http(s), не внутренние адреса"
        />
        <TextInput
          label="Документация"
          value={value.docs_url ?? ""}
          onChange={(e) =>
            onChange({ ...value, docs_url: e.currentTarget.value })
          }
          placeholder="https://docs.2gis.com/"
        />
        <Group grow>
          <NumberInput
            label="Потолок в сутки"
            value={value.daily_limit ?? ""}
            onChange={(v) =>
              onChange({
                ...value,
                daily_limit: typeof v === "number" ? v : null,
              })
            }
            min={0}
            allowDecimal={false}
          />
          <NumberInput
            label="Потолок в месяц"
            value={value.monthly_limit ?? ""}
            onChange={(v) =>
              onChange({
                ...value,
                monthly_limit: typeof v === "number" ? v : null,
              })
            }
            min={0}
            allowDecimal={false}
          />
        </Group>
        <Textarea
          label="Заметка"
          value={value.notes}
          onChange={(e) => onChange({ ...value, notes: e.currentTarget.value })}
          placeholder="Демо-ключ на 1 000 запросов навсегда, дальше 6 700 ₽ за 10 000 в месяц"
          autosize
          minRows={2}
        />
        <Group justify="flex-end">
          <Button
            type="submit"
            className="lc-btn"
            loading={saving}
            disabled={!value.name.trim()}
          >
            Сохранить
          </Button>
        </Group>
      </Stack>
    </form>
  );
}

function EntryEditor({
  row,
  saving,
  onSave,
  onDelete,
}: {
  row: ApiRow;
  saving: boolean;
  onSave: (input: ApiEntryInput, enabled: boolean) => void;
  onDelete: () => void;
}) {
  const [value, setValue] = useState<ApiEntryInput>({
    name: row.name,
    purpose: row.purpose,
    url: row.url ?? "",
    docs_url: row.docs_url ?? "",
    daily_limit: row.daily_limit,
    monthly_limit: row.monthly_limit,
    notes: row.notes,
  });
  const [enabled, setEnabled] = useState(row.enabled);
  const [удаляем, setУдаляем] = useState(false);
  return (
    <Stack gap="sm">
      <Switch
        checked={enabled}
        onChange={(e) => setEnabled(e.currentTarget.checked)}
        label="Включён"
        description="Выключенный остаётся в списке, но помечается серым"
      />
      <EntryForm
        value={value}
        onChange={setValue}
        saving={saving}
        onSubmit={() => onSave(value, enabled)}
      />
      {удаляем ? (
        <Group justify="space-between">
          <Text fz="xs" c="var(--lc-danger-text)">
            Удалить запись «{row.name}»? Это не выключает сам сервис — только
            строку здесь.
          </Text>
          <Group gap="xs">
            <Button
              variant="subtle"
              size="xs"
              onClick={() => setУдаляем(false)}
            >
              Оставить
            </Button>
            <Button color="red" size="xs" onClick={onDelete} loading={saving}>
              Удалить
            </Button>
          </Group>
        </Group>
      ) : (
        <Button
          variant="subtle"
          color="red"
          size="xs"
          onClick={() => setУдаляем(true)}
        >
          Удалить запись
        </Button>
      )}
    </Stack>
  );
}
