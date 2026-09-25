import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import {
  Accordion,
  Alert,
  Anchor,
  Button,
  Group,
  Menu,
  Modal,
  SegmentedControl,
  Skeleton,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Title,
  MultiSelect,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useBlocker, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "@/shared/api/http";
import { useAvitoAccountsQuery } from "@/shared/api/reference";
import type { BotMode, BotScenarioIssue, BotStepType } from "@/shared/api/types";
import { qk } from "@/shared/api/queryKeys";
import { EmptyState } from "@/shared/ui/EmptyState";
import { fetchBot, parseScenarioIssues, setBotAccounts, setBotEnabled, updateBot } from "./api";
import { ScheduleEditor } from "./components/ScheduleEditor";
import { StepCard } from "./components/StepCard";
import { useBotDraft } from "./draftStore";
import { STEP_META, varsAvailableAt } from "./scenario";
import { KNOWLEDGE_BASE_MAX, hasBlockingErrors, validateBotForm, validateScenario } from "./validation";
import "./bots.css";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";
import { IconAlert, IconChevronLeft, IconFlask, IconXCircle } from "@/shared/ui/Icon";
import { подписьКанала } from "@/shared/lib/channelLabel";

/**
 * Песочница — свой чанк: Drawer с чат-симулятором нужен по кнопке и далеко
 * не в каждой правке сценария, тянуть его вместе с формами шагов незачем (03 §7).
 */
const SandboxDrawer = lazy(() =>
  import("./SandboxDrawer").then((m) => ({ default: m.SandboxDrawer })),
);

function AddStepButton({ index, label }: { index: number; label: string }) {
  const addStep = useBotDraft((s) => s.addStep);
  return (
    <Menu shadow="md" width={220} withinPortal={false}>
      <Menu.Target>
        <Button variant="subtle" size="compact-sm" className="bot-add-step">
          {label}
        </Button>
      </Menu.Target>
      <Menu.Dropdown>
        {STEP_META.map((m) => (
          <Menu.Item key={m.type} onClick={() => addStep(m.type as BotStepType, index)}>
            {m.icon} {m.label} — <span style={{ color: "var(--lc-text-3)" }}>{m.hint}</span>
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}

/**
 * `/settings/bots/:id` (11 §5.2, 02 §5.1) — шапка бота, вертикальный список
 * карточек-шагов и sticky-панель действий. Черновик живёт в Zustand-сторе и
 * уходит на сервер только по «Сохранить»; «Протестировать» работает с этим же
 * черновиком, ничего не сохраняя (02 §5.3).
 */
export function BotEditor() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [sandboxOpen, setSandboxOpen] = useState(false);

  // `always`: черновик уходит в PUT полной заменой, и собранный из кэша он
  // затёр бы чужое сохранение (или выключение) за последние полминуты.
  const detail = useQuery({
    queryKey: qk.bots.detail(id),
    queryFn: () => fetchBot(id),
    enabled: Boolean(id),
    refetchOnMount: "always",
  });
  const accountsQuery = useAvitoAccountsQuery();

  const draft = useBotDraft();
  const loadedFor = useRef<string | null>(null);

  const boundAccountIds = useMemo(() => {
    if (detail.data?.accounts) return detail.data.accounts.map((a) => a.id);
    return (accountsQuery.data?.items ?? []).filter((a) => a.bot_id === id).map((a) => a.id);
  }, [detail.data, accountsQuery.data, id]);

  // Загружаем черновик РОВНО один раз на бота: рефетч списка аккаунтов или
  // фоновое обновление детали не должны затирать несохранённые правки.
  // И только из СВЕЖЕЙ детали (проверка 24.09): кэш прошлого захода грузился
  // сразу, а ответ сервера «бот выключен» черновик уже не менял — и
  // «Сохранить» включал бота обратно. Не удалось перечитать — ветка ошибки ниже
  // с «Повторить», а не правка поверх кэша.
  useEffect(() => {
    if (!detail.data || detail.isFetching || detail.isError) return;
    if (loadedFor.current === detail.data.id) return;
    if (accountsQuery.isPending && !detail.data.accounts) return;
    loadedFor.current = detail.data.id;
    draft.load(detail.data, boundAccountIds);
  }, [detail.data, detail.isFetching, detail.isError, accountsQuery.isPending, boundAccountIds, draft]);

  // Закрытие вкладки и перезагрузка с несохранённым — предупреждение браузера
  // (11 §5.2). Оно ловит ТОЛЬКО выход из приложения: см. блокировщик ниже.
  useEffect(() => {
    if (!draft.dirty) return;
    const handler = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [draft.dirty]);

  /*
   * УХОД ВНУТРИ ПРИЛОЖЕНИЯ — ОТДЕЛЬНЫЙ СЛУЧАЙ, И ИМЕННО ОН ЧАСТЫЙ (BOT-02).
   *
   * `beforeunload` выше не срабатывает ни на ссылке «← К списку ботов», ни на
   * пункте левого меню настроек, ни на кнопке «Назад» браузера: адрес меняет
   * router, страницу никто не выгружает. То есть предупреждение стояло ровно на
   * том выходе, которым отсюда почти не пользуются, а сценарий бота — работа на
   * полчаса — исчезал молча от одного щелчка по соседнему пункту меню.
   *
   * `useBlocker` — штатная ручка react-router для data-роутера
   * (`createBrowserRouter`, app/router.tsx): навигация останавливается, и мы
   * спрашиваем человека. Дальше он либо остаётся (`reset`), либо уходит
   * (`proceed`) — молчком не уходит никто.
   *
   * Сравниваем pathname: переход на тот же адрес (перерисовка, смена query)
   * останавливать незачем — это не уход со страницы.
   */
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      draft.dirty && currentLocation.pathname !== nextLocation.pathname,
  );

  const localIssues = useMemo(
    () =>
      draft.loaded
        ? [
            ...validateBotForm(draft.name, draft.knowledgeBase, draft.schedule),
            ...validateScenario(draft.scenario, draft.knowledgeBase),
          ]
        : [],
    [draft.loaded, draft.name, draft.schedule, draft.scenario, draft.knowledgeBase],
  );
  // Сервер — истина: пока его ответ актуален (черновик не менялся), показываем
  // именно его список; любая правка сбрасывает serverIssues и возвращает локальный.
  const issues = draft.serverIssues.length > 0 ? draft.serverIssues : localIssues;
  const blocked = hasBlockingErrors(issues);
  const warnings = issues.filter((i) => i.level === "warning").length;

  const issuesByStep = useMemo(() => {
    const map = new Map<string, BotScenarioIssue[]>();
    for (const issue of issues) {
      if (!issue.step_id) continue;
      if (!map.has(issue.step_id)) map.set(issue.step_id, []);
      (map.get(issue.step_id) as BotScenarioIssue[]).push(issue);
    }
    return map;
  }, [issues]);

  /** Клик по строке ошибки: раскрыть карточку, подсветить и проскроллить к ней. */
  const revealStep = (stepId: string | null) => {
    if (!stepId) return;
    draft.openStep(stepId);
    draft.focusStep(stepId);
    requestAnimationFrame(() => {
      document.querySelector<HTMLElement>(`[data-step-id="${stepId}"]`)?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    });
  };

  const toggleEnabled = useMutation({
    mutationFn: (enabled: boolean) => setBotEnabled(id, enabled),
    onSuccess: (bot) => {
      draft.setEnabled(bot.is_enabled);
      qc.invalidateQueries({ queryKey: qk.bots.root });
    },
    onError: (err) =>
        showToast(describeError({ where: "Переключить бота", error: err, fallback: "Попробуйте ещё раз" })),
  });

  const save = useMutation({
    mutationFn: async () => {
      const bot = await updateBot(id, draft.toInput());
      await setBotAccounts(id, draft.accountIds);
      return bot;
    },
    onSuccess: (bot) => {
      draft.markSaved(bot, draft.accountIds);
      qc.invalidateQueries({ queryKey: qk.bots.root });
      showToast({ title: "Сценарий сохранён", message: `Бот «${bot.name}» обновлён`, color: "lp" });
    },
    onError: (error) => {
      const serverIssues = parseScenarioIssues(error);
      if (serverIssues.length > 0) {
        draft.setServerIssues(serverIssues);
        revealStep(serverIssues.find((i) => i.step_id)?.step_id ?? null);
        return;
      }
      showToast(describeError({ where: "Сохранение бота", error }));
    },
  });

  /*
   * Вопрос об уходе рисуем в КАЖДОЙ ветке, где страница вообще что-то рисует:
   * остановленную навигацию обязан кто-то разрешить. Останься окно за
   * ранним `return`, человек получил бы намертво замерший интерфейс — щелчки по
   * меню не делают ничего и не объясняют почему.
   */
  const leaveConfirm = (
    <Modal
      opened={blocker.state === "blocked"}
      onClose={() => blocker.reset?.()}
      title="Сценарий не сохранён"
      centered
    >
      <Stack gap="var(--lc-space-3)">
        <Text fz="sm" c="var(--lc-text-2)">
          Уйти со страницы и потерять правки сценария? Сохранить их отсюда можно кнопкой
          «Сохранить» — она внизу страницы.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={() => blocker.reset?.()}>
            Остаться
          </Button>
          <Button color="red" onClick={() => blocker.proceed?.()}>
            Уйти без сохранения
          </Button>
        </Group>
      </Stack>
    </Modal>
  );

  /*
   * `draft.botId !== id` — ЗАЩИТА ОТ ЧУЖОГО ПЕРВОГО КАДРА (BOT-06).
   *
   * Стор черновика глобальный и при уходе со страницы не сбрасывается. Когда
   * админ возвращается к боту, которого уже открывал, деталь приходит из кэша
   * (`isPending === false`), а `draft.loaded` всё ещё true — но с именем,
   * расписанием и сценарием ПРЕДЫДУЩЕГО бота. Эффект загрузки отработает
   * только после рендера, поэтому один кадр показывал чужие данные: в лучшем
   * случае мелькание, в худшем — правка не того сценария.
   */
  /*
   * ОТКАЗ — РАНЬШЕ СКЕЛЕТА (проверка 24.09). Здесь ветка ошибки стояла после
   * ветки «черновик не загружен», а при ошибке черновик не загружается
   * никогда: по ссылке на удалённого бота и при 5xx экран навсегда оставался
   * серыми прямоугольниками. Черновик ЭТОГО бота уже загружен — фоновый сбой
   * перечитывания правку не прячет. 404 — «не найден»; прочее — «Повторить».
   */
  if (detail.isError && (!draft.loaded || draft.botId !== id)) {
    const notFound = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <div className="settings-section">
        <EmptyState
          illustration={notFound ? "bot" : "error"}
          live="alert"
          title={notFound ? "Бот не найден" : "Не получилось загрузить бота"}
          description={
            notFound
              ? "Возможно, его удалили"
              : detail.error instanceof ApiError && detail.error.message
                ? detail.error.message
                : "Нет связи с сервером — попробуйте ещё раз"
          }
          action={
            <Group gap="var(--lc-space-2)">
              {!notFound && (
                <Button variant="outline" onClick={() => void detail.refetch()}>
                  Повторить
                </Button>
              )}
              <Button
                variant={notFound ? "outline" : "subtle"}
                onClick={() => navigate("/settings/bots")}
              >
                К списку ботов
              </Button>
            </Group>
          }
        />
        {leaveConfirm}
      </div>
    );
  }

  if (detail.isPending || !draft.loaded || draft.botId !== id) {
    return (
      <div className="settings-section">
        <Skeleton height={140} mb="var(--lc-space-4)" />
        <Skeleton height={56} mb={8} />
        <Skeleton height={56} mb={8} />
        <Skeleton height={56} />
        {leaveConfirm}
      </div>
    );
  }

  const steps = draft.scenario.steps;
  const stepIds = steps.map((s) => s.id);
  const accountOptions = (accountsQuery.data?.items ?? []).map((a) => ({
    value: a.id,
    label: подписьКанала(a),
  }));
  const stolen = (accountsQuery.data?.items ?? []).filter(
    (a) => draft.accountIds.includes(a.id) && a.bot_id && a.bot_id !== id,
  );

  return (
    <div className="settings-section bot-editor">
      <Anchor component={Link} to="/settings/bots" fz="sm" mb="var(--lc-space-2)" display="block">
        <IconChevronLeft size={14} /> К списку ботов
      </Anchor>

      <section className="bot-editor__header">
        <Group align="flex-end" gap="var(--lc-space-4)" wrap="wrap">
          <TextInput
            label="Имя бота"
            value={draft.name}
            onChange={(e) => draft.setName(e.currentTarget.value)}
            w={280}
          />
          {/*
            ⚠ ПЕРЕКЛЮЧАТЕЛЬ ЖДЁТ СЕРВЕРА ЗАМЕТНО (находка 23.08 №14).

            `checked` привязан к `draft.isEnabled`, а он меняется только после
            ответа. То есть щелчок не двигал ничего: человек видел неподвижный
            переключатель и щёлкал ещё раз, и ещё — каждый щелчок уходил
            отдельной правкой, и последняя выигрывала гонку. Бот включался или
            выключался «через раз», причём непредсказуемо.

            Блокируем на время запроса: неподвижность становится объяснённой, а
            вторая правка физически не уходит.
          */}
          <Switch
            label="Включён"
            checked={draft.isEnabled}
            onChange={(e) => toggleEnabled.mutate(e.currentTarget.checked)}
            disabled={toggleEnabled.isPending}
            aria-label="Включён"
            mb={6}
          />
        </Group>

        {/*
          Режим — отдельно от «Включён» и намеренно рядом с ним: это два разных
          выключателя. «Включён» решает, работает ли бот вообще; режим — попадает ли
          его текст клиенту. Выключенный бот молчит совсем; включённый в режиме
          подсказки работает на полную, но говорит только с оператором.

          В отличие от «Включён» (отдельный endpoint, применяется сразу), режим —
          часть черновика и уезжает по «Сохранить»: переключают его почти всегда
          вместе с правкой текстов, и применить его раньше правок значит выпустить
          к клиенту недоделанный сценарий.
        */}
        <Group align="flex-start" gap="var(--lc-space-5)" wrap="wrap" mt="var(--lc-space-3)">
        {/*
          ВЫБОРА «МОЗГА» ЗДЕСЬ БОЛЬШЕ НЕТ (решение владельца от 12 августа:
          «сам LeadBot это не совсем бот, я хочу чтобы у него была своя
          собственная вкладка и настройки»).

          Лид-бот переехал в свой раздел — «Настройки → Лид-бот»: там его
          адрес, токен, каналы, режим, тестовый разговор и журнал работы.
          Обычный бот отвечает нашим ИИ, и третьего варианта у него не бывает,
          поэтому переключателя из одного значения тут не осталось вовсе.
        */}
        <Stack gap={6} className="bot-editor__mode">
          <Text fz="sm" fw={500} component="label" id="bot-mode-label">
            Что делать с ответом
          </Text>
          <SegmentedControl
            value={draft.mode}
            onChange={(v) => draft.setMode(v as BotMode)}
            aria-labelledby="bot-mode-label"
            w={340}
            data={[
              { value: "suggest", label: "Подсказка оператору" },
              { value: "auto", label: "Отвечать клиенту" },
            ]}
          />
          <Text
            fz="xs"
            maw={340}
            c={draft.mode === "auto" ? "var(--lc-warning-text)" : "var(--lc-text-3)"}
          >
            {draft.mode === "auto"
              ? "Бот пишет клиенту в Авито сам. Оператор подключается по правилам сценария — на «позовите человека», по таймауту и при передаче."
              : "Клиент не получает ничего. Ответ появляется заметкой в диалоге — оператор читает и отвечает сам. Так стоит начинать на новом аккаунте."}
          </Text>
        </Stack>
        </Group>

        <MultiSelect
          label="Аккаунты Авито"
          description="Аккаунт может быть привязан только к одному боту"
          data={accountOptions}
          value={draft.accountIds}
          onChange={draft.setAccounts}
          searchable
          clearable
          comboboxProps={{ withinPortal: false }}
          mt="var(--lc-space-3)"
        />
        {stolen.length > 0 && (
          <Text fz="xs" c="var(--lc-warning-text)" mt={4}>
            <IconAlert size={14} /> {stolen.map((a) => a.title).join(", ")} — сейчас за другим ботом, при сохранении перепривяжется
          </Text>
        )}

        <div style={{ marginTop: "var(--lc-space-3)" }}>
          <ScheduleEditor schedule={draft.schedule} onChange={draft.setSchedule} />
        </div>

        <Textarea
          label="База знаний ai_answer"
          description="Текст целиком уходит в промпт модели на шаге ai_answer. Пишите фактами: услуга — цена от — срок"
          value={draft.knowledgeBase}
          onChange={(e) => draft.setKnowledge(e.currentTarget.value)}
          autosize
          minRows={4}
          maxRows={16}
          mt="var(--lc-space-3)"
          /* 13 стояло числом; это ступень `--lc-fz-note` — на мониторе от 1800
             она поднимается до 14, а число осталось бы прежним. */
          styles={{
            input: {
              fontFamily: "var(--mantine-font-family-monospace, monospace)",
              fontSize: "var(--lc-fz-note)",
            },
          }}
        />
        <Text fz="xs" c={draft.knowledgeBase.length > KNOWLEDGE_BASE_MAX ? "var(--lc-danger-text)" : "var(--lc-text-3)"} ta="right">
          {draft.knowledgeBase.length} / {KNOWLEDGE_BASE_MAX}
        </Text>
      </section>

      <section className="bot-editor__steps">
        {/*
          Кегль заголовка — токеном, и именно ступенью РАЗДЕЛА.

          Стояло `fz={16}` числом: 16 — это `--lc-fz-card`, ступень заголовка
          карточки. То есть «Сценарий» — единственный разделитель на длинной
          странице, отделяющий настройки бота от списка его шагов, — набирался
          ровно тем же кеглем, что подписи внутри карточек под ним, и с
          полуметра не читался как граница разделов вовсе. `--lc-fz-section`
          (20, а на мониторе от 1800 — 22) ставит его на ступень выше
          содержимого и на ступень ниже названия экрана.
        */}
        <Title order={2} fz="var(--lc-fz-section)" c="var(--lc-text-1)" mb="var(--lc-space-3)">
          Сценарий
        </Title>
        <Accordion
          multiple
          value={draft.openSteps}
          onChange={draft.setOpenSteps}
          chevronPosition="right"
          variant="separated"
        >
          {steps.map((step, index) => (
            <div key={step.id}>
              {index > 0 && <AddStepButton index={index} label="Добавить шаг" />}
              <StepCard
                step={step}
                index={index}
                total={steps.length}
                stepIds={stepIds}
                vars={varsAvailableAt(steps, index)}
                isEntry={draft.scenario.entry === step.id}
                issues={issuesByStep.get(step.id) ?? []}
                focused={draft.focusedStepId === step.id}
                open={draft.openSteps.includes(step.id)}
              />
            </div>
          ))}
        </Accordion>
        <AddStepButton index={steps.length} label="Добавить шаг" />
      </section>

      <section className="bot-editor__actions">
        <Group gap="var(--lc-space-3)">
          <Button
            onClick={() => save.mutate()}
            disabled={blocked || !draft.dirty}
            loading={save.isPending}
            color={warnings > 0 && !blocked ? "yellow" : undefined}
          >
            {warnings > 0 && !blocked ? "Сохранить с предупреждениями" : "Сохранить"}
          </Button>
          <Button
            variant="outline"
            leftSection={<IconFlask size={16} />}
            onClick={() => setSandboxOpen(true)}
          >
            Протестировать
          </Button>
          <Button variant="subtle" onClick={draft.revert} disabled={!draft.dirty}>
            Отменить изменения
          </Button>
          {draft.dirty && (
            <Text fz="xs" c="var(--lc-text-3)">
              Есть несохранённые изменения
            </Text>
          )}
        </Group>

        {issues.length > 0 && (
          <Stack gap={2} mt="var(--lc-space-2)" className="bot-editor__issues" data-testid="bot-issues">
            {issues.map((issue, i) => (
              <Text
                key={`${issue.code}-${issue.step_id}-${i}`}
                component="button"
                type="button"
                ta="left"
                fz="sm"
                className="bot-issue"
                data-level={issue.level}
                onClick={() => revealStep(issue.step_id)}
              >
                {issue.level === "error" ? <IconXCircle size={13} /> : <IconAlert size={13} />}{" "}
                {issue.message}
              </Text>
            ))}
          </Stack>
        )}

        {blocked && (
          <Alert color="red" variant="light" mt="var(--lc-space-2)" p="xs">
            Ошибки блокируют сохранение — почините отмеченные шаги
          </Alert>
        )}
      </section>

      {sandboxOpen && (
        <Suspense fallback={null}>
          <SandboxDrawer opened onClose={() => setSandboxOpen(false)} />
        </Suspense>
      )}

      {leaveConfirm}
    </div>
  );
}
