import { ActionIcon, Button, Checkbox, Group, Radio, Stack, Text, TextInput } from "@mantine/core";
import type { BotSchedule, BotScheduleInterval, BotWeekDay } from "@/shared/api/types";
import { WEEK_DAYS, crossesMidnight } from "../scenario";
import { IconX } from "@/shared/ui/Icon";

const ALL_DAYS = WEEK_DAYS.map((d) => d.value);

const DEFAULT_INTERVAL: BotScheduleInterval = { days: [...ALL_DAYS], start: "20:00", end: "10:00" };

/**
 * Редактор `bots.schedule` (02 §2.5, 11 §5.2). Расписание — фильтр на ВХОД
 * бота в диалог: начатый диалог бот доводит до конца и вне расписания.
 * Интервалы объединяются по ИЛИ, `start > end` — интервал через полночь.
 */
export function ScheduleEditor({
  schedule,
  onChange,
}: {
  schedule: BotSchedule;
  onChange: (next: BotSchedule) => void;
}) {
  const always = schedule.always !== false;
  const intervals = schedule.intervals ?? [];

  const patchInterval = (index: number, next: Partial<BotScheduleInterval>) =>
    onChange({
      ...schedule,
      intervals: intervals.map((iv, i) => (i === index ? { ...iv, ...next } : iv)),
    });

  return (
    <Stack gap="var(--lc-space-2)">
      <Radio.Group
        value={always ? "always" : "schedule"}
        onChange={(v) =>
          onChange(
            v === "always"
              ? // ⚠ ИНТЕРВАЛЫ СОХРАНЯЕМ, А НЕ ВЫБРАСЫВАЕМ. Здесь возвращался
                // объект `{ always: true }` целиком — без часового пояса и без
                // интервалов. Человек настраивал «пн–пт 9:00–18:00, сб 10:00–15:00»,
                // на минуту переключался на «Круглосуточно» посмотреть, как оно
                // будет, возвращался обратно — и получал один пустой интервал по
                // умолчанию. Настройки не было где взять: на сервер уходило то же
                // усечённое расписание.
                // «Круглосуточно» — это про то, КОГДА бот работает, а не команда
                // забыть, что было настроено.
                { ...schedule, always: true }
              : {
                  ...schedule,
                  always: false,
                  timezone: schedule.timezone ?? "Europe/Moscow",
                  intervals: intervals.length ? intervals : [{ ...DEFAULT_INTERVAL }],
                },
          )
        }
        label="Расписание"
      >
        <Group gap="var(--lc-space-4)" mt={4}>
          <Radio value="always" label="Круглосуточно" />
          <Radio value="schedule" label="По расписанию" />
        </Group>
      </Radio.Group>

      {!always && (
        <Stack gap="var(--lc-space-2)" mt={4}>
          {intervals.map((iv, i) => (
            <Group key={i} gap="var(--lc-space-2)" align="center" wrap="wrap" className="bot-interval-row">
              <Group gap={2}>
                {WEEK_DAYS.map((d) => (
                  <Checkbox
                    key={d.value}
                    size="xs"
                    label={d.label}
                    checked={(iv.days ?? ALL_DAYS).includes(d.value)}
                    onChange={(e) => {
                      const set = new Set<BotWeekDay>(iv.days ?? ALL_DAYS);
                      if (e.currentTarget.checked) set.add(d.value);
                      else set.delete(d.value);
                      patchInterval(i, { days: ALL_DAYS.filter((v) => set.has(v)) });
                    }}
                  />
                ))}
              </Group>
              <TextInput
                type="time"
                aria-label={`Интервал ${i + 1}: начало`}
                value={iv.start}
                onChange={(e) => patchInterval(i, { start: e.currentTarget.value })}
                size="xs"
                w={110}
              />
              <Text fz="sm" c="var(--lc-text-3)">
                —
              </Text>
              <TextInput
                type="time"
                aria-label={`Интервал ${i + 1}: конец`}
                value={iv.end}
                onChange={(e) => patchInterval(i, { end: e.currentTarget.value })}
                size="xs"
                w={110}
              />
              {crossesMidnight(iv.start, iv.end) && (
                <Text fz="xs" c="var(--lc-text-3)">
                  через полночь
                </Text>
              )}
              <ActionIcon
                variant="subtle"
                color="red"
                size="sm"
                aria-label={`Удалить интервал ${i + 1}`}
                // Расписание без интервалов не включит бота никогда — бэкенд
                // такое тело отвергает: последний интервал не удаляем,
                // «выключить расписание» — это радио «Круглосуточно».
                disabled={intervals.length === 1}
                onClick={() => onChange({ ...schedule, intervals: intervals.filter((_, idx) => idx !== i) })}
              >
                <IconX size={14} />
              </ActionIcon>
            </Group>
          ))}
          <Group gap="var(--lc-space-3)">
            <Button
              variant="subtle"
              size="compact-sm"
              onClick={() => onChange({ ...schedule, intervals: [...intervals, { ...DEFAULT_INTERVAL }] })}
            >
              Добавить интервал
            </Button>
            <Text fz="xs" c="var(--lc-text-3)">
              Время московское ({schedule.timezone ?? "Europe/Moscow"})
            </Text>
          </Group>
        </Stack>
      )}
    </Stack>
  );
}
