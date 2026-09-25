import { useState } from "react";
import {
  Badge,
  Button,
  Checkbox,
  Menu,
  Modal,
  Radio,
  Select,
  Switch,
  Textarea,
  TextInput,
  Tooltip,
  useMantineColorScheme,
} from "@mantine/core";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { EmptyState, type Illustration } from "@/shared/ui/EmptyState";
import * as Icons from "@/shared/ui/Icon";
import { toast } from "@/shared/ui/toast";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import "./uikit.css";

/**
 * UI Kit — живой каталог системы (docs/16-DESIGN-SYSTEM-2026.md §8).
 *
 * Не картинка и не отдельный макет: страница собирает НАСТОЯЩИЕ компоненты
 * продукта на выдуманных данных. Поэтому она не может разойтись с
 * приложением — если карточка диалога изменится, здесь изменится тоже, а
 * нарисованный макет молча устарел бы к следующему спринту.
 *
 * Заодно это единственное место, где видны все состояния сразу: hover и
 * disabled в бою поодиночке не поймать, а «пустая очередь» в рабочей системе
 * бывает раз в день и не тогда, когда её проверяют.
 */

/* ─────────────────────────────────────────────── выдуманные данные ──── */

const CONV: ConversationDto = {
  id: "demo-1",
  status: "in_progress",
  channel: "avito",
  account: { id: "a1", title: "! Парт - 7 / Ист - В43 МНЧ !" },
  client: { id: "c1", name: "Алексей Смирнов", phone: null, avito_rating: null },
  assignee: { id: "u1", full_name: "Николай Петров", role: "manager" },
  item: { id: "i1", title: "Ремонт холодильника Bosch", url: null, price: null },
  last_message: { direction: "in", body: "Здравствуйте! Холодильник не морозит", created_at: "" },
  unread_count: 3,
  bot_active: false,
  tags: [],
  transferred_to_me: false,
  last_message_at: new Date().toISOString(),
} as unknown as ConversationDto;

/** Одно «сейчас» на всю страницу: каталог не тикает, ему нужна стабильность. */
const NOW = Date.now();

function minutesAgo(m: number): string {
  return new Date(NOW - m * 60_000).toISOString();
}

function conv(patch: Partial<ConversationDto>): ConversationDto {
  return { ...CONV, ...patch } as ConversationDto;
}

const MSG_BASE = {
  id: "m",
  attachments: [],
  delivery_status: "delivered",
  created_at: new Date().toISOString(),
} as const;

function msg(patch: Record<string, unknown>): MessageDto {
  return { ...MSG_BASE, ...patch } as unknown as MessageDto;
}

/* ────────────────────────────────────────────────────── каркас ──────── */

function Section({ id, title, note, children }: {
  id: string;
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="uikit__section" id={id}>
      <h2 className="uikit__h2">{title}</h2>
      {note && <p className="uikit__note">{note}</p>}
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="uikit__row">
      <span className="uikit__label">{label}</span>
      <div className="uikit__row-items">{children}</div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────── цвета ──────── */

const SURFACES = ["--lc-bg-1", "--lc-bg-0", "--lc-surface", "--lc-surface-hover", "--lc-selected", "--lc-border"];
const TEXTS = ["--lc-text-1", "--lc-text-2", "--lc-text-3", "--lc-text-disabled"];
const ACCENTS: Array<[string, string, string]> = [
  ["primary", "--lc-primary", "--lc-primary-solid"],
  ["success", "--lc-success", "--lc-success-solid"],
  ["danger", "--lc-danger", "--lc-danger-solid"],
  ["warning", "--lc-warning", "--lc-warning-solid"],
  ["accent", "--lc-accent", "--lc-accent-solid"],
];

function Swatch({ token }: { token: string }) {
  return (
    <div className="uikit__swatch">
      <span className="uikit__chipcolor" style={{ background: `var(${token})` }} />
      <code>{token}</code>
    </div>
  );
}

/* ─────────────────────────────────────────────────── страница ───────── */

export function UiKitPage() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const [modal, setModal] = useState(false);
  const [checked, setChecked] = useState(true);
  const [radio, setRadio] = useState("a");
  const [sw, setSw] = useState(true);

  return (
    <div className="uikit">
      <header className="uikit__head">
        <div>
          <h1 className="uikit__h1">UI Kit LeadChat</h1>
          <p className="uikit__note">
            Живой каталог: компоненты настоящие, данные выдуманные. Переключите тему —
            всё ниже обязано остаться читаемым.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => setColorScheme(colorScheme === "dark" ? "light" : "dark")}
        >
          {colorScheme === "dark" ? "Светлая тема" : "Тёмная тема"}
        </Button>
      </header>

      <Section id="colors" title="Цвета" note="Компоненты берут цвет только отсюда; HEX в компоненте — ошибка ревью.">
        <Row label="Поверхности">
          {SURFACES.map((t) => <Swatch key={t} token={t} />)}
        </Row>
        <Row label="Текст">
          {TEXTS.map((t) => <Swatch key={t} token={t} />)}
        </Row>
        <div className="uikit__note">
          У каждого акцента пара: яркий — для рамок, иконок и фокуса (нужно 3:1),
          плотный — под заливку с текстом (нужно 4.5:1).
        </div>
        {ACCENTS.map(([name, accent, solid]) => (
          <Row key={name} label={name}>
            <Swatch token={accent} />
            <Swatch token={solid} />
          </Row>
        ))}
      </Section>

      <Section id="type" title="Типографика" note="Inter / SF Pro Display, интерлиньяж 150%.">
        <p style={{ fontSize: "var(--lc-fz-page)", fontWeight: 600, margin: 0 }}>
          Заголовок страницы — 24 Semibold
        </p>
        <p style={{ fontSize: "var(--lc-fz-section)", fontWeight: 600, margin: 0 }}>
          Заголовок секции — 18 Semibold
        </p>
        <p style={{ fontSize: "var(--lc-fz-card)", fontWeight: 500, margin: 0 }}>
          Заголовок карточки — 16 Medium
        </p>
        <p style={{ fontSize: "var(--lc-fz-body)", margin: 0 }}>
          Основной текст — 14 Regular. Клиент пишет про холодильник, оператор отвечает.
        </p>
        <p style={{ fontSize: "var(--lc-fz-caption)", color: "var(--lc-text-3)", margin: 0 }}>
          Подпись — 12 Regular
        </p>
      </Section>

      <Section id="form" title="Форма и глубина">
        <Row label="Радиусы">
          {["xs", "sm", "md", "lg", "xl"].map((r) => (
            <div key={r} className="uikit__box" style={{ borderRadius: `var(--lc-radius-${r})` }}>
              {r}
            </div>
          ))}
        </Row>
        <Row label="Тени">
          {["0", "1", "2", "3"].map((s) => (
            <div key={s} className="uikit__box" style={{ boxShadow: `var(--lc-shadow-${s})` }}>
              {s}
            </div>
          ))}
        </Row>
        <Row label="Сетка 8pt">
          {["1", "2", "3", "4", "5", "6", "7", "8"].map((s) => (
            <div key={s} className="uikit__spacer">
              <span style={{ width: `var(--lc-space-${s})` }} />
              <code>{s}</code>
            </div>
          ))}
        </Row>
      </Section>

      <Section
        id="buttons"
        title="Кнопки"
        note="Каждое состояние — отдельный столбец. Наведите и нажмите: hover 150 мс, нажатие — 0.98."
      >
        {(["filled", "outline", "light", "subtle"] as const).map((variant) => (
          <Row key={variant} label={variant}>
            <Button variant={variant}>Обычная</Button>
            <Button variant={variant} disabled>Недоступна</Button>
            <Button variant={variant} loading>Загрузка</Button>
            <Button variant={variant} color="green">Принять</Button>
            <Button variant={variant} color="red">Отклонить</Button>
          </Row>
        ))}
        <Row label="Размеры">
          <Button size="compact-xs">compact-xs</Button>
          <Button size="xs">xs</Button>
          <Button size="sm">sm</Button>
          <Button size="md">md</Button>
        </Row>
      </Section>

      <Section id="inputs" title="Поля ввода">
        <Row label="Состояния">
          <TextInput placeholder="Обычное" aria-label="Обычное" />
          <TextInput placeholder="С ошибкой" error="Так нельзя" aria-label="С ошибкой" />
          <TextInput placeholder="Недоступно" disabled aria-label="Недоступно" />
          <TextInput
            placeholder="С иконкой"
            leftSection={<Icons.IconSearch size={16} />}
            aria-label="С иконкой"
          />
        </Row>
        <Row label="Прочее">
          <Select data={["Новый", "В работе", "Закрыт"]} defaultValue="В работе" aria-label="Селект" />
          <Textarea placeholder="Многострочное" aria-label="Многострочное" />
        </Row>
        <Row label="Переключатели">
          <Checkbox label="Флажок" checked={checked} onChange={(e) => setChecked(e.currentTarget.checked)} />
          <Checkbox label="Недоступен" disabled />
          <Radio.Group value={radio} onChange={setRadio}>
            <Radio value="a" label="Первый" />
            <Radio value="b" label="Второй" />
          </Radio.Group>
          <Switch label="Тумблер" checked={sw} onChange={(e) => setSw(e.currentTarget.checked)} />
        </Row>
      </Section>

      <Section id="badges" title="Бейджи и подсказки">
        <Row label="Бейджи">
          <Badge>По умолчанию</Badge>
          <Badge color="green">Успех</Badge>
          <Badge color="red">Ошибка</Badge>
          <Badge color="amber">Внимание</Badge>
          <Badge variant="light">Мягкий</Badge>
          <Badge variant="outline">Контурный</Badge>
        </Row>
        <Row label="Всплывающее">
          <Tooltip label="Подсказка появляется через 400 мс">
            <Button variant="outline">Наведите</Button>
          </Tooltip>
          <Menu>
            <Menu.Target>
              <Button variant="outline">Меню</Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Действия</Menu.Label>
              <Menu.Item leftSection={<Icons.IconForward size={16} />}>Передать</Menu.Item>
              <Menu.Item leftSection={<Icons.IconArchive size={16} />}>В архив</Menu.Item>
              <Menu.Divider />
              <Menu.Item data-danger leftSection={<Icons.IconX size={16} />}>Заблокировать</Menu.Item>
            </Menu.Dropdown>
          </Menu>
          <Button variant="outline" onClick={() => setModal(true)}>Модальное окно</Button>
        </Row>
        <Row label="Тосты">
          <Button variant="light" color="green" onClick={() => toast.success("Диалог принят", "Клиент ждёт ответа")}>
            Успех
          </Button>
          <Button variant="light" onClick={() => toast.info("Диалог передан вам", "Николай Петров")}>
            Сообщение
          </Button>
          <Button variant="light" color="amber" onClick={() => toast.warning("Вебхук молчит", "Сообщения доедут с задержкой")}>
            Внимание
          </Button>
          <Button variant="light" color="red" onClick={() => toast.errorPersistent("Не удалось отправить", "Токен канала отозван")}>
            Ошибка (ждёт человека)
          </Button>
        </Row>
      </Section>

      <Section
        id="conv"
        title="Карточка диалога"
        note="Настоящий компонент списка. Действий по наведению у строки нет — только открыть диалог. Две последние строки — пороги ожидания: 5 и 15 минут."
      >
        <div className="uikit__list">
          <ConversationListItem row={conv({})} active={false} onOpen={() => {}} now={NOW} showChannel />
          <ConversationListItem row={conv({ id: "d2" })} active onOpen={() => {}} now={NOW} showChannel />
          <ConversationListItem
            row={conv({ id: "d3", unread_count: 0, tags: ["негатив"] })}
            active={false}
            onOpen={() => {}}
            now={NOW} showChannel
          />
          <ConversationListItem
            row={conv({ id: "d4", bot_active: true, unread_count: 0 })}
            active={false}
            onOpen={() => {}}
            now={NOW} showChannel
          />
          <ConversationListItem
            row={conv({ id: "d5", in_inbox: true, waiting_human: "12 мин", unread_count: 0 })}
            active={false}
            onOpen={() => {}}
            now={NOW} showChannel
          />
          <ConversationListItem
            row={conv({ id: "d6", escalated: true, unread_count: 0 })}
            active={false}
            onOpen={() => {}}
            now={NOW} showChannel
          />
          <ConversationListItem
            row={conv({ id: "d7", unread_count: 1, last_message_at: minutesAgo(7) })}
            active={false}
            onOpen={() => {}}
            now={NOW} showChannel
          />
          <ConversationListItem
            row={conv({ id: "d8", unread_count: 4, last_message_at: minutesAgo(23) })}
            active={false}
            onOpen={() => {}}
            now={NOW} showChannel
          />
        </div>
      </Section>

      <Section id="bubbles" title="Пузыри переписки" note="Пять типов; исходящий — градиент primary.">
        <div className="uikit__thread">
          <MessageBubble
            msg={msg({ id: "b1", direction: "in", sender_type: "client", body: "Здравствуйте! Холодильник не морозит, гудит и тёплый. Сколько будет диагностика?" })}
            prev={null}
            clientId="c1"
            clientName="Алексей Смирнов"
          />
          <MessageBubble
            msg={msg({ id: "b2", direction: "out", sender_type: "user", sender: { full_name: "Николай Петров" }, body: "Добрый день! Диагностика 500 ₽, при ремонте — бесплатно." })}
            prev={msg({ id: "b1", direction: "in", sender_type: "client" })}
            clientId="c1"
            clientName="Алексей Смирнов"
          />
          <MessageBubble
            msg={msg({ id: "b3", direction: "out", sender_type: "bot", body: "Здравствуйте! Я помощник Lead Partner. Уточните, пожалуйста, модель техники." })}
            prev={msg({ id: "b2", direction: "out", sender_type: "user" })}
            clientId="c1"
            clientName="Алексей Смирнов"
          />
          <MessageBubble
            msg={msg({ id: "b4", direction: "note", sender_type: "user", sender: { full_name: "Мария Кузнецова" }, body: "Клиент постоянный, был ремонт в марте — можно скидку." })}
            prev={msg({ id: "b3", direction: "out", sender_type: "bot" })}
            clientId="c1"
            clientName="Алексей Смирнов"
          />
          <MessageBubble
            msg={msg({ id: "b5", direction: "out", sender_type: "user", sender: { full_name: "Николай Петров" }, body: "Отправляю адрес мастера", delivery_status: "failed", delivery_error: "токен канала отозван" })}
            prev={msg({ id: "b4", direction: "note", sender_type: "user" })}
            clientId="c1"
            clientName="Алексей Смирнов"
            onRetry={() => {}}
          />
          <MessageBubble
            msg={msg({ id: "b6", direction: "system", sender_type: "system", body: "Диалог передан: Мария Кузнецова → Николай Петров" })}
            prev={msg({ id: "b5", direction: "out", sender_type: "user" })}
            clientId="c1"
            clientName="Алексей Смирнов"
          />
        </div>
        {/* Компонент нарисован, но в продукте не подключён — см. chat-thread.css */}
        <Row label="Набор текста (не подключён)">
          <div className="msg-typing">
            <div className="msg-typing__bubble">
              <span className="msg-typing__dot" />
              <span className="msg-typing__dot" />
              <span className="msg-typing__dot" />
            </div>
          </div>
        </Row>
      </Section>

      <Section id="empty" title="Пустые состояния">
        <div className="uikit__grid">
          {(
            [
              ["done", "Очередь пуста", "Новые обращения появятся здесь"],
              ["chat", "Выберите диалог слева", "или нажмите Ctrl+K для поиска"],
              ["search", "Ничего не нашлось", "Ищем по имени, телефону и тексту сообщений"],
              ["error", "Не получилось загрузить", undefined],
              ["plug", "Подключите первый аккаунт", "И диалоги появятся через минуту"],
              ["bot", "Ботов пока нет", undefined],
              ["bolt", "Быстрых ответов нет", undefined],
              ["archive", "Закрытых диалогов нет", undefined],
            ] as Array<[Illustration, string, string | undefined]>
          ).map(([ill, title, text]) => (
            <div key={ill} className="uikit__card">
              <EmptyState illustration={ill} title={title} description={text} />
            </div>
          ))}
        </div>
      </Section>

      <Section id="icons" title="Иконки" note="Сетка 24, обводка 2, цвет — currentColor.">
        <div className="uikit__icons">
          {Object.entries(Icons)
            .filter(([n]) => n.startsWith("Icon"))
            .map(([name, Cmp]) => {
              const C = Cmp as (p: { size?: number }) => JSX.Element;
              return (
                <div key={name} className="uikit__icon">
                  <C size={20} />
                  <code>{name.replace("Icon", "")}</code>
                </div>
              );
            })}
        </div>
      </Section>

      <Modal opened={modal} onClose={() => setModal(false)} title="Передать диалог">
        <p className="uikit__note">
          Заголовок 18/600, тело 24px отступа, подложка затемнена и размыта.
        </p>
        <div style={{ display: "flex", gap: "var(--lc-space-2)", justifyContent: "flex-end" }}>
          <Button variant="subtle" onClick={() => setModal(false)}>Отмена</Button>
          <Button onClick={() => setModal(false)}>Передать</Button>
        </div>
      </Modal>
    </div>
  );
}
