import { useState } from "react";
import { Badge, Button, Group, Modal, Skeleton, Stack, Switch, Table, Text } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { BotListItem, BotsPage as BotsPageResponse } from "@/shared/api/types";
import { qk } from "@/shared/api/queryKeys";
import { ApiError, request } from "@/shared/api/http";
import { EmptyState } from "@/shared/ui/EmptyState";
import { createBot, fetchBot, fetchBots, setBotEnabled } from "./api";
import { newBotInput } from "./newBot";
import { formatSchedule } from "./scenario";
import "./bots.css";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";
import { PageHeader } from "@/shared/ui/PageHeader";
import { IconPlus } from "@/shared/ui/Icon";

function forbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/**
 * Причина отказа СЛОВАМИ, а не одна фраза на все случаи (BOT-05).
 *
 * Раньше и 403, и 500, и оборванная сеть давали «Проверьте соединение и
 * попробуйте ещё раз» — и человек шёл проверять вайфай, когда у него просто
 * отобрали право в соседней вкладке или упал сервер. Утверждать причину, если
 * мы её не знаем, дороже, чем честно назвать код ответа.
 */
function loadFailureReason(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "Сервер не ответил. Проверьте соединение и попробуйте ещё раз";
  }
  if (error.status === 403) {
    return "Права на настройку ботов сняты. Обратитесь к администратору — экран доступен только администраторам";
  }
  if (error.status >= 500) {
    return "Сервер ответил ошибкой. Дело не в вашем соединении — попробуйте ещё раз через минуту";
  }
  return error.message;
}

/**
 * `/settings/bots` (11 §5.1, 02 §5.1) — только admin (право `bots:manage`,
 * guard в роутере). Switch «Вкл» дёргает enable/disable сразу, без сохранения
 * формы (01 §8.4); «Создать бота» кладёт копию дефолтного «Первичного приёма».
 */
export function BotsPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const bots = useQuery({ queryKey: qk.bots.list, queryFn: fetchBots });
  /*
   * Подтверждение удаления — модалка Mantine, а не `window.confirm` (FUNC-68).
   *
   * Нативное окно браузера в этом продукте было третьим способом спросить
   * «точно?»: рядом живут модалки Mantine на других экранах. Оно приходит
   * системным шрифтом, без наших цветов, пишет сверху «localhost:5173
   * сообщает» и в довесок замораживает вкладку — на боевом экране, где
   * удаление необратимо, это выглядит как чужое окно, а не как наше.
   */
  const [pendingDelete, setPendingDelete] = useState<BotListItem | null>(null);

  /*
   * Тумблер «Вкл» двигается СРАЗУ, ещё до ответа сервера (BOT-01).
   *
   * `checked` читался прямо из ответа запроса, состояния загрузки не было: на
   * неспешной сети админ щёлкал, ничего не происходило, он щёлкал ещё — и
   * уходили два-три enable/disable подряд, последний из которых мог оставить
   * боевого бота выключенным. Теперь список правится оптимистично, повторный
   * щелчок по той же строке заблокирован, а отказ сервера возвращает прежнее
   * значение и объясняет это тостом.
   */
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => setBotEnabled(id, enabled),
    onMutate: async ({ id, enabled }) => {
      await qc.cancelQueries({ queryKey: qk.bots.list });
      const previous = qc.getQueryData<BotsPageResponse>(qk.bots.list);
      qc.setQueryData<BotsPageResponse>(qk.bots.list, (old) =>
        old
          ? { ...old, items: old.items.map((b) => (b.id === id ? { ...b, is_enabled: enabled } : b)) }
          : old,
      );
      return { previous };
    },
    onError: (_error, _vars, context) => {
      if (context?.previous) qc.setQueryData(qk.bots.list, context.previous);
      showToast(describeError({ where: "Переключение бота", error: _error }));
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.bots.root }),
  });
  const togglingId = toggle.isPending ? toggle.variables.id : null;

  const create = useMutation({
    mutationFn: () => createBot(newBotInput()),
    onSuccess: (bot) => {
      qc.invalidateQueries({ queryKey: qk.bots.root });
      navigate(`/settings/bots/${bot.id}`);
    },
    onError: (err) =>
        showToast(describeError({ where: "Создать бота", error: err, fallback: "Попробуйте ещё раз" })),
  });

  /*
   * Удаление бота (просьба заказчика от 7 августа: «ботов я могу только
   * создать и отключить, но не удалить»).
   *
   * Сервер откажет, если бот привязан к аккаунтам Авито, и назовёт их поимённо —
   * иначе «Бот привязан к аккаунтам» означало бы «идите ищите, к каким».
   * Отвязка не делается автоматически: у аккаунта ровно один бот, и молча снять
   * его значило бы выключить автоответы там, где на них рассчитывают.
   */
  const remove = useMutation({
    mutationFn: (id: string) => request<void>(`/bots/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSettled: () => setPendingDelete(null),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.bots.root });
      showToast({ title: "Бот удалён", color: "lp" });
    },
    onError: (e) => {
      const details = e instanceof ApiError ? e.details : undefined;
      const bound = Array.isArray(details?.accounts)
        ? (details.accounts as { title: string }[]).map((a) => a.title).join(", ")
        : null;
      showToast({
        title: "Бот не удалён",
        message: bound
          ? `Сначала отвяжите аккаунты Авито: ${bound}`
          : "Попробуйте ещё раз",
        color: "red",
      });
    },
  });

  /** Дубликат не забирает аккаунты: у аккаунта ровно один бот (01 §8.5). */
  const duplicate = useMutation({
    mutationFn: async (id: string) => {
      const source = await fetchBot(id);
      return createBot({
        name: `${source.name} (копия)`,
        is_enabled: false,
        schedule: source.schedule,
        scenario: source.scenario,
        knowledge_base: source.knowledge_base ?? "",
        ai_provider: source.ai_provider ?? "claude",
        mode: source.mode ?? "suggest",
      });
    },
    onSuccess: (bot) => {
      qc.invalidateQueries({ queryKey: qk.bots.root });
      navigate(`/settings/bots/${bot.id}`);
    },
    onError: (err) =>
        showToast(describeError({ where: "Дублировать бота", error: err, fallback: "Попробуйте ещё раз" })),
  });

  const items = bots.data?.items ?? [];

  return (
    <div className="settings-section">
      <PageHeader
        title="Боты"
        description="Бот здоровается и собирает контекст, пока менеджеры заняты. Как только пишет менеджер — бот замолкает в этом диалоге навсегда"
        actions={
          /*
           * НА ПУСТОМ ЭКРАНЕ КНОПКА ОДНА — ЦЕНТРАЛЬНАЯ.
           *
           * Ботов нет — и «Создать бота» стояло дважды: здесь, в шапке, и в пустом
           * состоянии посреди экрана. Две одинаковые кнопки в одном кадре заставляют
           * выбирать между ними, хотя выбора нет: это одно и то же действие. Пустое
           * состояние объясняет, ЧТО создастся («из шаблона „Первичный приём"»), —
           * значит право первой кнопки за ним.
           */
          bots.isSuccess && items.length === 0 ? undefined : (
            <Button
              leftSection={<IconPlus size={14} />}
              onClick={() => create.mutate()}
              loading={create.isPending}
            >
              Создать бота
            </Button>
          )
        }
      />

      {bots.isPending && (
        <div className="bots-skeleton">
          <Skeleton height={40} mb={8} />
          <Skeleton height={40} mb={8} />
          <Skeleton height={40} />
        </div>
      )}

      {bots.isError && (
        <EmptyState
          illustration="error"
          title="Не удалось загрузить ботов"
          description={loadFailureReason(bots.error)}
          action={
            forbidden(bots.error) ? undefined : (
              <Button variant="outline" onClick={() => bots.refetch()}>
                Повторить
              </Button>
            )
          }
        />
      )}

      {bots.isSuccess && items.length === 0 && (
        <EmptyState
          illustration="bot"
          title="Ботов пока нет"
          description="Создайте первого из шаблона «Первичный приём» — он поздоровается и соберёт контакт, пока менеджеры заняты"
          action={
            <Button
              leftSection={<IconPlus size={14} />}
              onClick={() => create.mutate()}
              loading={create.isPending}
            >
              Создать бота
            </Button>
          }
        />
      )}

      {bots.isSuccess && items.length > 0 && (
        /*
          `lc-table--cards` — общий механизм «строка → карточка» ниже 900
          (src/app/lc-table-cards.css). Без него на 375 таблица просила 1018 px
          при колонке в 375: аккаунты, расписание, счётчик и обе кнопки
          уезжали в невидимую горизонтальную прокрутку. `data-label` у ячеек —
          обязательная часть механизма: подпись столбца CSS взять неоткуда.
        */
        <Table highlightOnHover verticalSpacing="sm" className="bots-table lc-table--cards">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Бот</Table.Th>
              <Table.Th w={80}>Вкл</Table.Th>
              <Table.Th w={190}>Как работает</Table.Th>
              <Table.Th>Аккаунты</Table.Th>
              <Table.Th>Расписание</Table.Th>
              <Table.Th w={120} ta="right">
                Диалогов/7д
              </Table.Th>
              <Table.Th w={130} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {items.map((bot: BotListItem) => (
              <Table.Tr key={bot.id} className="bots-table__row">
                <Table.Td>
                  <Text
                    component="button"
                    type="button"
                    className="bots-table__name"
                    fw={600}
                    fz="sm"
                    c="var(--lc-text-1)"
                    onClick={() => navigate(`/settings/bots/${bot.id}`)}
                  >
                    {bot.name}
                  </Text>
                  <Text fz="xs" c="var(--lc-text-3)">
                    {bot.scenario_steps_count} шаг(ов)
                    {bot.knowledge_base_present ? " · база знаний есть" : " · без базы знаний"}
                  </Text>
                </Table.Td>
                <Table.Td data-label="Вкл">
                  <Switch
                    checked={bot.is_enabled}
                    disabled={togglingId === bot.id}
                    aria-label={`Включить бота ${bot.name}`}
                    onChange={(e) => toggle.mutate({ id: bot.id, enabled: e.currentTarget.checked })}
                  />
                </Table.Td>
                <Table.Td data-label="Как работает">
                  {/*
                    «Включён» и «отвечает клиенту» — разные вещи, и путать их дорого:
                    бот в режиме подсказки работает, считает ответы и пишет заметки, но
                    клиент от него не получает ничего. В списке это должно быть видно
                    сразу, без захода в редактор.
                  */}
                  <Group gap={4} wrap="nowrap">
                    <Badge size="sm" variant="light" color={bot.mode === "auto" ? "lp" : "gray"}>
                      {bot.mode === "auto" ? "Автоответ" : "Подсказка"}
                    </Badge>
                  </Group>
                </Table.Td>
                <Table.Td data-label="Аккаунты">
                  {bot.accounts.length === 0 ? (
                    <Text fz="sm" c="var(--lc-text-3)">
                      —
                    </Text>
                  ) : (
                    <Group gap={4}>
                      {bot.accounts.map((a) => (
                        <Badge key={a.id} size="sm" variant="light" color="gray">
                          {a.title}
                        </Badge>
                      ))}
                    </Group>
                  )}
                </Table.Td>
                <Table.Td data-label="Расписание">
                  <Text fz="sm" c="var(--lc-text-2)">
                    {formatSchedule(bot.schedule)}
                  </Text>
                </Table.Td>
                <Table.Td ta="right" data-label="Диалогов/7д">
                  {/*
                    ЧИСЛО НАБРАНО КРУПНЕЕ СОСЕДЕЙ, А НЕ ВРОВЕНЬ С НИМИ.

                    «Диалогов/7д» — единственная цифра списка и единственная
                    причина сюда заглянуть: по ней видно, работает бот или
                    стоит. Набрана она была тем же кеглем `sm` и тем же
                    приглушённым `--lc-text-2`, что расписание и подпись «12
                    шаг(ов)» рядом, — на равных с шестью строками текста в той
                    же строке таблицы. В списке из трёх ботов это ещё
                    различимо, в списке из пятнадцати — уже нет.

                    `--lc-fz-card` (16), а не `--lc-fz-metric` (24): здесь
                    список для сравнения строк, и метрика в 24 разорвала бы
                    плотность ряда. `lc-num` даёт табличные цифры — без них
                    колонка чисел не выравнивается по разрядам, и сравнивать
                    её глазом бесполезно.
                  */}
                  <Text className="lc-num" fz="var(--lc-fz-card)" fw={600} c="var(--lc-text-1)">
                    {bot.conversations_7d ?? "—"}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Button
                    variant="subtle"
                    size="compact-sm"
                    loading={duplicate.isPending && duplicate.variables === bot.id}
                    onClick={() => duplicate.mutate(bot.id)}
                  >
                    Дублировать
                  </Button>
                  <Button
                    variant="subtle"
                    size="compact-sm"
                    color="red"
                    loading={remove.isPending && remove.variables === bot.id}
                    onClick={() => setPendingDelete(bot)}
                  >
                    Удалить
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}

      <Modal
        opened={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title="Удалить бота?"
        centered
      >
        <Stack gap="var(--lc-space-3)">
          <Text fz="sm" c="var(--lc-text-2)">
            Бот «{pendingDelete?.name}» исчезнет вместе со сценарием и базой знаний — вернуть их
            будет неоткуда. Если бот привязан к аккаунтам Авито, система откажет и назовёт их.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setPendingDelete(null)}>
              Отмена
            </Button>
            <Button
              color="red"
              loading={remove.isPending}
              onClick={() => {
                if (pendingDelete) remove.mutate(pendingDelete.id);
              }}
            >
              Удалить
            </Button>
          </Group>
        </Stack>
      </Modal>
    </div>
  );
}
