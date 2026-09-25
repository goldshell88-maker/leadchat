import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Badge, Button, Drawer, Group, Radio, Stack, Table, Text, TextInput, Textarea } from "@mantine/core";
import { useMutation } from "@tanstack/react-query";
import type { BotScenarioIssue, SandboxAiMode, SandboxEvent, SandboxState } from "@/shared/api/types";
import { ApiError } from "@/shared/api/http";
import { parseScenarioIssues, sandboxFireTimeout, sandboxMessage, sandboxStart, sandboxStop } from "./api";
import { useBotDraft } from "./draftStore";
import { scenarioLimits } from "./scenario";
import { IconAlert, IconBot, IconCheck, IconCheckCircle, IconClock, IconNote, IconSend, IconXCircle } from "@/shared/ui/Icon";

/**
 * 🧪 Песочница (02 §5.3, 11 §5.4). Работает с ЧЕРНОВИКОМ редактора — сохранять
 * бота не нужно; реальному клиенту ничего не уходит, в conversations/messages
 * не пишется ни строчки. Состояние сессии живёт в Redis на бэкенде (TTL 1 час).
 */

type FeedItem = { key: number } & ({ kind: "client"; text: string } | { kind: "event"; event: SandboxEvent });

/** Серая техническая строка трассировки (11 §5.4). */
function Trace({ children }: { children: ReactNode }) {
  return (
    <Text fz="xs" className="sandbox-trace">
      {children}
    </Text>
  );
}

/**
 * Причины отказа, у которых нет серверной подписи.
 *
 * Отказы ВХОДА бота приходят с `label` от `app/bots/runtime.py:ENTRY_BLOCKS` —
 * той же функции, что решает это в проде; своего словаря на них редактор не
 * держит, иначе объяснения разошлись бы. Здесь остаётся только то, что
 * придумывает сама песочница.
 */
const SKIPPED_REASONS: Record<string, string> = {
  not_waiting: "бот сейчас ничего не ждёт — таймаут промотать нечему",
};

function EventLine({ event }: { event: SandboxEvent }) {
  switch (event.kind) {
    case "bot_message":
      return (
        <div className="sandbox-msg sandbox-msg--bot">
          <Text fz="sm"><IconBot size={14} /> {event.text}</Text>
        </div>
      );
    case "step":
      return (
        <Trace>
          Шаг {event.id} ({event.type}){event.detail ? `: ${event.detail}` : ""}
        </Trace>
      );
    case "waiting":
      return (
        <Trace>
          <IconClock size={14} /> жду ответ{event.var ? ` (${event.var})` : ""}
          {event.deadline ? ` · дедлайн ${event.deadline}` : ""}
        </Trace>
      );
    case "answer":
      return (
        <Trace>
          <IconCheck size={14} /> ответ принят{event.var ? `: ${event.var} = «${event.value ?? ""}»` : ""}
        </Trace>
      );
    case "condition":
      return (
        <Trace>
          Условие {event.step} → {event.next}
        </Trace>
      );
    case "retry":
      return (
        <Trace>
          ↻ переспрашиваю ({event.step}, попытка {event.attempts})
        </Trace>
      );
    case "offscript":
      return <Trace>↯ сообщение мимо сценария: {event.count}</Trace>;
    case "detector":
      return (
        <Trace>
          🔎 сработал детектор: {event.detector === "human_request" ? "клиент просит человека" : event.detector}
        </Trace>
      );
    case "timeout":
      return <Trace>⏩ таймаут шага {event.step}</Trace>;
    case "timeout_ignored":
      return <Trace>таймаут не в счёт — клиент успел ответить</Trace>;
    case "skipped":
      return <Trace>⏸ {event.label ?? SKIPPED_REASONS[event.reason] ?? event.reason}</Trace>;
    case "empty_render":
      // Не серая строка, а заметная: клиент этого сообщения НЕ получил.
      return (
        <Alert color="yellow" variant="light" p="xs" mt={4}>
          <IconAlert size={14} /> Шаг {event.step}:{" "}
          {event.question
            ? "вопрос не задан — подставлять нечего"
            : "сообщение не ушло — подставлять нечего"}{" "}
          ({event.placeholders.join(", ")}). Поправьте текст шага
        </Alert>
      );
    case "ai_call":
      return (
        <Trace>
          ✨ ai_answer: confidence {event.confidence}
          {event.needs_operator ? " · модель просит менеджера" : ""}
        </Trace>
      );
    case "note":
      return (
        <div className="sandbox-note">
          <Text fz="sm"><IconNote size={14} /> {event.text}</Text>
        </div>
      );
    case "tags":
      return <Trace>🏷 теги: {event.tags.join(", ")}</Trace>;
    case "handoff":
      return (
        <div className="sandbox-handoff">
          <Text fz="sm" fw={600}>
            👤 HANDOFF: {event.reason}
          </Text>
          {event.comment ? <Text fz="sm">«{event.comment}»</Text> : null}
        </div>
      );
    case "close":
      return (
        <div className="sandbox-close">
          <Text fz="sm"><IconCheckCircle size={14} /> Диалог закрыт ботом{event.text ? `: ${event.text}` : ""}</Text>
        </div>
      );
    default: {
      // Движок может завести новое событие раньше редактора — показываем
      // его серой строкой, а не роняем панель и не молчим.
      const raw = event as { kind: string };
      return <Trace>{raw.kind}</Trace>;
    }
  }
}

export function SandboxDrawer({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const draft = useBotDraft();
  const [aiMode, setAiMode] = useState<SandboxAiMode>("stub");
  const [timeMode, setTimeMode] = useState<"now" | "custom">("now");
  const [nowOverride, setNowOverride] = useState("");
  const [clientName, setClientName] = useState("Иван");
  const [itemTitle, setItemTitle] = useState("Ремонт iPhone");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [state, setState] = useState<SandboxState | null>(null);
  const [issues, setIssues] = useState<BotScenarioIssue[]>([]);
  const [draftText, setDraftText] = useState("");
  /*
   * ОТКАЗ СЕРВЕРА ПОСРЕДИ СЕССИИ (FUNC-67).
   *
   * У ленты не было состояния ошибки вовсе: сессия живёт час, и когда она
   * истекала (404) или падал сервер, кнопка «отправить» отщёлкивала, а на
   * экране не появлялось ни строчки. Со стороны — «песочница сломалась и
   * молчит», хотя лечится это одной кнопкой «Сбросить».
   */
  const [failure, setFailure] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const keyRef = useRef(0);

  const push = (items: FeedItem[]) => setFeed((prev) => [...prev, ...items]);
  const eventsToFeed = (events: SandboxEvent[]): FeedItem[] =>
    events.map((event) => ({ key: (keyRef.current += 1), kind: "event", event }));

  const onTickError = (error: unknown) => {
    const api = error instanceof ApiError ? error : null;
    if (api?.status === 404) {
      // Сессия истекла: ждать от неё ответов больше нечего, поле ввода гасим.
      setExpired(true);
      setFailure("Сессия песочницы истекла — она живёт час. Начните заново, сценарий не пострадал");
      return;
    }
    if (api?.status === 503) {
      setFailure(`${api.message}. Переключитесь на «заглушку» — граф отлаживается и без модели`);
      return;
    }
    setFailure(api ? api.message : "Сервер не ответил. Проверьте соединение и повторите");
  };

  const start = useMutation({
    mutationFn: () =>
      sandboxStart({
        scenario: draft.scenario,
        knowledge_base: draft.knowledgeBase,
        schedule: draft.schedule,
        client_name: clientName,
        item_title: itemTitle,
        // Строкой без пояса: сервер читает её как московскую (schemas/bots.py).
        // Перевод в UTC шёл через пояс БРАУЗЕРА — владелец на UTC+10 задавал
        // 23:00, а сервер проверял 16:00 по Москве (проверка 24.09).
        now_override: timeMode === "custom" && nowOverride ? nowOverride : null,
        ai_mode: aiMode,
      }),
    onSuccess: (res) => {
      setIssues([]);
      setFailure(null);
      setExpired(false);
      setSessionId(res.session_id);
      setFeed(eventsToFeed(res.events ?? []));
      setState(res.state ?? null);
    },
    onError: (error) => {
      // 422 — «сначала почините сценарий» (11 §5.4): список ошибок поверх Drawer.
      const scenarioIssues = parseScenarioIssues(error);
      setIssues(scenarioIssues);
      setSessionId(null);
      // Не 422 — сценарий ни при чём: 503 «нет ключа AI», 500, оборванная сеть.
      // Без этой ветки панель открывалась пустой и не объясняла ничего.
      if (scenarioIssues.length === 0) onTickError(error);
    },
  });

  const send = useMutation({
    mutationFn: (text: string) => sandboxMessage(sessionId as string, text),
    onSuccess: (res) => {
      setFailure(null);
      push(eventsToFeed(res.events ?? []));
      setState(res.state ?? null);
    },
    onError: onTickError,
  });

  const fireTimeout = useMutation({
    mutationFn: () => sandboxFireTimeout(sessionId as string),
    onSuccess: (res) => {
      setFailure(null);
      push([{ key: (keyRef.current += 1), kind: "event", event: { kind: "step", id: "⏩ таймаут", type: "ask" } }]);
      push(eventsToFeed(res.events ?? []));
      setState(res.state ?? null);
    },
    onError: onTickError,
  });

  // Открыли панель — стартуем сессию черновика; закрыли — гасим её на бэке.
  useEffect(() => {
    if (opened && !sessionId && !start.isPending) start.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- старт ровно один раз на открытие
  }, [opened]);

  const stopSession = () => {
    if (sessionId) void sandboxStop(sessionId).catch(() => undefined);
    setSessionId(null);
    setFeed([]);
    setState(null);
    setFailure(null);
    setExpired(false);
  };

  const reset = () => {
    stopSession();
    start.mutate();
  };

  const submit = () => {
    const text = draftText.trim();
    if (!text || !sessionId) return;
    push([{ key: (keyRef.current += 1), kind: "client", text }]);
    setDraftText("");
    send.mutate(text);
  };

  const limits = scenarioLimits(draft.scenario);
  const counters = state?.counters ?? {};
  const waiting = state?.waiting ?? null;

  return (
    <Drawer
      opened={opened}
      onClose={() => {
        stopSession();
        onClose();
      }}
      position="right"
      size="xl"
      title={`Песочница: ${draft.name} (черновик)`}
      /*
        Ширину ленты и панели состояния решает ЯЩИК, а не окно: `size="xl"` —
        780 px на любом мониторе. Класс объявляет тело ящика контейнером, и
        раскладка внутри (bots.css) считается по его ширине, а не по ширине
        окна, которая с ней не связана. Разбор — там же, у `.sandbox-body`.
      */
      classNames={{ body: "sandbox-drawer__body" }}
      /*
       * ПОРТАЛ ОБЯЗАТЕЛЕН — БЕЗ НЕГО ПАНЕЛЬ УЕЗЖАЕТ ЗА ЭКРАН.
       *
       * Стоял `withinPortal={false}`, и панель рисовалась прямо внутри колонки
       * настроек. Её `position: fixed` считался от этой колонки, а не от окна:
       * на ноутбучных 1280 px весь ящик сдвигался вправо на ширину двух меню
       * (300 px), и правая колонка состояния — текущий шаг, собранные
       * переменные, счётчики и кнопка «⏩ Промотать таймаут» — оказывалась за
       * краем экрана целиком. Горизонтальной прокрутки там нет, добраться до
       * неё было нельзя никак.
       */
    >
      <Stack gap="var(--lc-space-3)">
        <Group gap="var(--lc-space-4)" align="flex-start" wrap="wrap">
          <Radio.Group value={timeMode} onChange={(v) => setTimeMode(v as "now" | "custom")} label="Время">
            <Group gap="var(--lc-space-3)" mt={4}>
              <Radio value="now" label="сейчас" />
              <Radio value="custom" label="задать" />
            </Group>
          </Radio.Group>
          {timeMode === "custom" && (
            <TextInput
              type="datetime-local"
              label="по Москве"
              aria-label="Время симуляции по Москве"
              value={nowOverride}
              onChange={(e) => setNowOverride(e.currentTarget.value)}
              size="sm"
            />
          )}
          <Radio.Group value={aiMode} onChange={(v) => setAiMode(v as SandboxAiMode)} label="AI">
            <Group gap="var(--lc-space-3)" mt={4}>
              <Radio value="stub" label="заглушка" />
              <Radio value="real" label="настоящий" />
            </Group>
          </Radio.Group>
        </Group>

        <Group gap="var(--lc-space-3)" wrap="wrap">
          <TextInput
            label="Клиент"
            value={clientName}
            onChange={(e) => setClientName(e.currentTarget.value)}
            size="sm"
            w={160}
          />
          <TextInput
            label="Объявление"
            value={itemTitle}
            onChange={(e) => setItemTitle(e.currentTarget.value)}
            size="sm"
            w={220}
          />
          {/* «Начать заново», как и вторая кнопка того же `reset` ниже. Было
              «Сбросить» — то же слово, которым на соседних экранах снимают
              фильтры, и то же действие под двумя именами на одном экране. */}
          <Button variant="default" size="sm" mt={22} onClick={reset} loading={start.isPending}>
            Начать заново
          </Button>
        </Group>

        {aiMode === "real" && (
          <Alert color="yellow" variant="light" p="xs">
            <IconAlert size={14} /> Настоящий режим делает живые вызовы Claude и расходует токены. Для отладки графа хватает заглушки
          </Alert>
        )}

        {failure && (
          <Alert
            color="red"
            variant="light"
            p="xs"
            title="Песочница не ответила"
            data-testid="sandbox-failure"
          >
            <Stack gap={6} align="flex-start">
              <Text fz="xs">{failure}</Text>
              <Button variant="light" size="compact-xs" onClick={reset} loading={start.isPending}>
                Начать заново
              </Button>
            </Stack>
          </Alert>
        )}

        {issues.length > 0 && (
          <Alert color="red" variant="light" p="xs" title="Сначала почините сценарий">
            <Stack gap={2}>
              {issues.map((issue, i) => (
                <Text key={i} fz="xs">
                  <IconXCircle size={13} /> {issue.step_id ? `${issue.step_id}: ` : ""}
                  {issue.message}
                </Text>
              ))}
            </Stack>
          </Alert>
        )}

        <div className="sandbox-body">
          <div className="sandbox-chat" data-testid="sandbox-chat">
            {feed.length === 0 && issues.length === 0 && (
              <Text fz="xs" c="var(--lc-text-3)">
                Типовой чек перед включением: дневной сценарий → «сейчас ночь» → невалидный телефон дважды →
                «позовите менеджера» → «ужасный сервис!». Так за минуту проверяются все шесть условий передачи
                менеджеру
              </Text>
            )}
            {feed.map((item) =>
              item.kind === "client" ? (
                <div key={item.key} className="sandbox-msg sandbox-msg--client">
                  <Text fz="sm">{item.text}</Text>
                </div>
              ) : (
                <EventLine key={item.key} event={item.event} />
              ),
            )}
          </div>

          <aside className="sandbox-state">
            <Text fz="sm" fw={600} c="var(--lc-text-1)">
              Состояние
            </Text>
            <Text fz="xs" c="var(--lc-text-2)">
              Шаг: {state?.step ?? "—"} {waiting ? <Badge size="xs">WAITING</Badge> : null}
            </Text>
            {waiting && (
              <Text fz="xs" c="var(--lc-text-2)">
                Ждёт: {waiting.var ?? waiting.kind}
                {waiting.deadline ? ` · до ${waiting.deadline}` : ""}
              </Text>
            )}

            <Text fz="xs" fw={600} mt="var(--lc-space-2)" c="var(--lc-text-1)">
              vars
            </Text>
            <Table withRowBorders={false} verticalSpacing={2} fz="xs">
              <Table.Tbody>
                {Object.entries(state?.vars ?? {}).map(([k, v]) => (
                  <Table.Tr key={k}>
                    <Table.Td c="var(--lc-text-3)">{k}</Table.Td>
                    <Table.Td>{v === null || v === "" ? "—" : String(v)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>

            <Text fz="xs" fw={600} mt="var(--lc-space-2)" c="var(--lc-text-1)">
              счётчики
            </Text>
            <Text fz="xs" c="var(--lc-text-2)">
              шагов: {counters.steps_total ?? 0}/{limits.max_steps_total} · подряд бота:{" "}
              {counters.bot_msgs_row ?? 0}/{limits.max_bot_messages_row}
            </Text>
            <Text fz="xs" c="var(--lc-text-2)">
              мимо сценария: {counters.offscript_msgs ?? 0}/{limits.max_offscript_messages} · вызовов AI:{" "}
              {counters.ai_calls ?? 0}
            </Text>

            <Button
              variant="light"
              size="compact-sm"
              mt="var(--lc-space-3)"
              disabled={!waiting || !sessionId}
              loading={fireTimeout.isPending}
              onClick={() => fireTimeout.mutate()}
            >
              ⏩ Промотать таймаут
            </Button>
          </aside>
        </div>

        {/* Класс — ради прилипания строки ввода к низу ящика: разбор и числа
            замера в bots.css у `.sandbox-composer`. */}
        <Group align="flex-end" gap="var(--lc-space-2)" className="sandbox-composer">
          <Textarea
            aria-label="Ответ клиента"
            placeholder="ответ клиента…"
            value={draftText}
            onChange={(e) => setDraftText(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            autosize
            minRows={1}
            maxRows={4}
            style={{ flex: 1 }}
            disabled={!sessionId || expired}
          />
          {/* aria-label обязателен: раньше кнопку называл сам символ «➤»,
              теперь внутри иконка, а она декоративная — без подписи кнопка
              осталась бы безымянной и для скринридера, и для тестов. */}
          <Button
            onClick={submit}
            disabled={!sessionId || expired || !draftText.trim()}
            loading={send.isPending}
            aria-label="Отправить ответ клиента"
          >
            <IconSend size={16} />
          </Button>
        </Group>
      </Stack>
    </Drawer>
  );
}
