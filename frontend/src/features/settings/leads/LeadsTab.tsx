import { useState } from "react";
import { Badge, Button, Group, Select, Stack, Text, TextInput } from "@mantine/core";

import { Link } from "react-router-dom";
import { ApiError } from "@/shared/api/http";
import { выбралСам } from "@/features/chats/выборДиалога";

import { useAvitoAccountsQuery } from "@/shared/api/reference";
import type { AvitoAccountDto } from "@/shared/api/types";
import { PageHeader } from "@/shared/ui/PageHeader";
import {
  useHandouts,
  useLeadsState,
  useRevokeToken,
  useSetChannelLeadFields,
  useSetChannelSrc,
  type LeadHandout,
} from "./leadsApi";
import "./leads.css";

/**
 * РАЗДЕЛ «АВТОЗАЯВКИ» — заявки заводятся сами, из диалогов с итогом «Выезд».
 *
 * КАК ЭТО РАБОТАЕТ ЦЕЛИКОМ. На офисном компьютере стоит расширение Chrome. Оно
 * само ходит сюда по расписанию (связь только исходящая: лид-центр доступен
 * лишь оттуда, а та машина за NAT), забирает лиды и заводит заявки в БТ / КП /
 * МНЧ под вашей учётной записью. Обратно присылает, что получилось.
 *
 * ЧТО СТАНОВИТСЯ ЗАЯВКОЙ. Диалог, которому ЧЕЛОВЕК поставил итог «Выезд», и у
 * клиента известен телефон. Не любой диалог с номером: иначе в лид-центр
 * поехали бы заявки на тех, кто спросил цену и пропал, а каждая лишняя — это
 * возможный выезд мастера впустую.
 *
 * ТРИ ЧАСТИ ЭКРАНА в порядке подключения: выдать токен → сказать каждому каналу,
 * в какой он лид-центр → смотреть, что уходит.
 */

/** «13 августа, 02:41» — журнал читают по времени. */
function moment(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", {
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Тон строки журнала.
 *
 * Красным — только «заявки нет»: отклонил лид-центр либо ответ так и не пришёл.
 * «Не создана, уже висит живая» — жёлтым: это не поломка, а защита от дубля,
 * но посмотреть стоит. Успех остаётся серым, иначе журнал станет разноцветным
 * и настоящую беду в нём никто не найдёт.
 */
function toneOf(row: LeadHandout): "bad" | "warn" | undefined {
  if (!row.acked) return undefined;
  if (row.decision === "error") return "bad";
  if (row.decision === "blocked") return "warn";
  return undefined;
}

function ConnectionBlock() {
  const state = useLeadsState();
  const revoke = useRevokeToken();

  if (!state.data) return null;

  return (
    <section className="settings-block">
      <Text component="h2" fz="var(--lc-fz-section)" fw={600} c="var(--lc-text-1)" m={0}>
        Подключение расширения
      </Text>
      {/* 16.08, решение владельца: выпуск токена убран. Заявки бот-диалогов
          уже создаются через очередь ЛИД-БОТА и расширение «Автозаявки» —
          второй путь отсюда создавал бы КАЖДУЮ заявку в CRM дважды. Кнопка
          вернётся, только если поток осознанно переведут на LeadChat
          (предохранитель на сервере: POST /settings/leads/token → 409). */}
      <Text fz="sm" c="var(--lc-text-2)">
        Заявки из диалогов бота уходят в лид-центры через очередь лид-бота — расширение уже
        подключено к ней. Отдельный токен LeadChat не нужен: второй путь создавал бы каждую
        заявку дважды.
      </Text>

      <Group gap="sm" mt="xs">
        {state.data.configured ? (
          <>
            <Badge color="yellow" variant="light">
              Старый токен ещё задан
            </Badge>
            <Button
              className="lc-btn"
              variant="subtle"
              onClick={() => revoke.mutate()}
              loading={revoke.isPending}
            >
              Отозвать
            </Button>
          </>
        ) : (
          <Badge color="gray" variant="light">
            Отключено — так и задумано
          </Badge>
        )}
      </Group>
    </section>
  );
}

/*
 * ⚠ ЭТО ЗАПАСНОЙ ПУТЬ, А НЕ НАЗНАЧЕНИЕ НАПРАВЛЕНИЯ, и подписи обязаны говорить
 * это прямо. Владелец, увидев старую подпись пустого выбора (она обещала, что заявки не уйдут вовсе),
 * прочитал его дословно и был прав: «у аккаунта нет конкретно направления, сам
 * бот должен решать, на какое направление создавать заявку» — сказано дважды,
 * 14 и 15 августа. Бот так и решает с 14.08: направление читается из
 * ОБЪЯВЛЕНИЯ, в которое написал клиент (`lead_direction.detect`), а выбор
 * здесь срабатывает, только когда объявление неизвестно или его название ни
 * на что не похоже. Экран отставал от собственного сервера и пугал.
 */
const ЦЕНТРЫ = [
  { value: "", label: "Не выбран — по проблеме и объявлению" },
  { value: "bt", label: "БТ — бытовая техника" },
  { value: "kp", label: "КП — компьютерная помощь" },
  { value: "mnc", label: "МНЧ — муж на час" },
];


/**
 * Один канал: лид-центр и три поля заявки.
 *
 * ПОЛЯ СОХРАНЯЮТСЯ ПО УХОДУ ФОКУСА, А НЕ ПО КНОПКЕ. Кнопка «Сохранить» у каждой
 * строки означала бы четыре кнопки на канал и девять каналов на экране; а поле,
 * которое сохраняется само, человек заполняет и уходит. Ошибку показываем общей
 * строкой сверху — она и так одна на блок.
 *
 * ⚠ ИСТОЧНИК И ССЫЛКА НА ОТЗЫВ ПОЯВИЛИСЬ ЗДЕСЬ 14.08. Колонки жили с миграции 0040,
 * заявка их читала, а заполнить было нечем: ни ручка правки, ни экран о них не знали.
 * Владелец: «я не могу указать источник… и ссылку на отзыв».
 */
function ChannelRow({
  account,
  onError,
  saveSrc,
  saveFields,
}: {
  account: AvitoAccountDto;
  onError: (m: string | null) => void;
  saveSrc: ReturnType<typeof useSetChannelSrc>;
  saveFields: ReturnType<typeof useSetChannelLeadFields>;
}) {
  // ⚠ 18.08: поля были неуправляемыми (defaultValue) — React не обновлял их
  // после ответа сервера и после чужой правки: два человека, открывшие экран
  // одновременно, тихо затирали друг друга, а свой откат при ошибке не был
  // виден вовсе. Черновик живёт до успешного сохранения (тот же приём, что
  // draftDept в «Команде»).
  const [draft, setDraft] = useState<Record<string, string>>({});
  const поле_значение = (поле: string, серверное: string) => draft[поле] ?? серверное;

  const сохранить = (поле: string, значение: string, было: string) => {
    if (значение.trim() === было) return; // ничего не менялось — не тревожим сервер
    onError(null);
    saveFields
      .mutateAsync({ id: account.id, [поле]: значение.trim() })
      .then(() => setDraft((m) => { const rest = { ...m }; delete rest[поле]; return rest; }))
      .catch((e) => {
        // откат виден: поле снова показывает серверное значение
        setDraft((m) => { const rest = { ...m }; delete rest[поле]; return rest; });
        onError(e instanceof ApiError ? e.message : "Не удалось сохранить");
      });
  };

  return (
    <div className="leads-channel">
      {/*
        ⚠ РЯД БОЛЬШЕ НЕ `wrap="nowrap"`, И ЭТО НЕ ВКУСОВЩИНА.

        Замер в браузере на 375: имени канала доставалось 136 px при нужных
        279–419 — на экране стояло «Бригада Андрея …», а так начинаются все
        три бригады подряд. Полю выбора при этом оставалось 184 px из
        объявленных 320: `flex-shrink` отбирал ровно ту ширину, которую ему
        подняли 29.08, чтобы подпись не обрезалась молча.

        Теперь ряд переносится: пока места хватает — имя и выбор рядом, когда
        нет — в две строки, и обе части целые. Раскладка живёт в
        `.leads-channel__head` (leads.css), потому что вместе с ней там
        объясняется, почему у имени именно такой минимум.
      */}
      <div className="leads-channel__head">
        <Text fz="sm" className="leads-channel__name">
          {account.title}
        </Text>
        <Select
          className="lc-field"
          /*
           * 320, А НЕ 280 (замер на бою 29.08). Самая длинная подпись —
           * «Не выбран — по проблеме и объявлению» — просит 287 px, поле
           * давало 278: на экране стояло «…и объявлени», без последней буквы
           * и без многоточия. Тридцать одна строка подряд с обрезанным
           * словом и есть то самое «всё едет».
           *
           * Запас взят на 11%: шрифт и масштаб у людей разные, впритык
           * поставить нельзя. Если подпись когда-нибудь станет длиннее
           * запаса, страховка в `.lc-field` (lc-base.css) допишет многоточие
           * — обрезка хотя бы перестанет выдавать себя за целое слово.
           */
          w={320}
          size="xs"
          aria-label={`Запасной лид-центр канала ${account.title}`}
          data={ЦЕНТРЫ}
          value={account.lead_src_key ?? ""}
          allowDeselect={false}
          onChange={(v) => {
            onError(null);
            saveSrc
              .mutateAsync({ id: account.id, srcKey: v ?? "" })
              .catch((e) => onError(e instanceof ApiError ? e.message : "Не удалось сохранить"));
          }}
        />
      </div>
      <Group gap="sm" wrap="wrap" className="leads-channel__fields">
        <TextInput
          size="xs"
          w={160}
          label="Источник"
          placeholder="В95"
          aria-label={`Источник канала ${account.title}`}
          value={поле_значение("lead_origin", account.lead_origin ?? "")}
          onChange={(e) => setDraft((m) => ({ ...m, lead_origin: e.currentTarget.value }))}
          onBlur={(e: React.FocusEvent<HTMLInputElement>) => сохранить("lead_origin", e.currentTarget.value, account.lead_origin ?? "")}
        />
        <TextInput
          size="xs"
          w={140}
          label="Номер партнёра"
          placeholder="723"
          aria-label={`Номер партнёра канала ${account.title}`}
          value={поле_значение("lead_partner_number", account.lead_partner_number ?? "")}
          onChange={(e) => setDraft((m) => ({ ...m, lead_partner_number: e.currentTarget.value }))}
          onBlur={(e: React.FocusEvent<HTMLInputElement>) =>
            сохранить(
              "lead_partner_number",
              e.currentTarget.value,
              account.lead_partner_number ?? "",
            )
          }
        />
        <TextInput
          size="xs"
          w={280}
          label="Ссылка на отзыв"
          placeholder="https://..."
          aria-label={`Ссылка на отзыв канала ${account.title}`}
          value={поле_значение("review_url", account.review_url ?? "")}
          onChange={(e) => setDraft((m) => ({ ...m, review_url: e.currentTarget.value }))}
          onBlur={(e: React.FocusEvent<HTMLInputElement>) => сохранить("review_url", e.currentTarget.value, account.review_url ?? "")}
        />
      </Group>
    </div>
  );
}

function ChannelsBlock() {
  const accounts = useAvitoAccountsQuery();
  const save = useSetChannelSrc();
  const saveFields = useSetChannelLeadFields();
  const [error, setError] = useState<string | null>(null);

  const каналы = (accounts.data?.items ?? []).filter((a) => !a.is_service);

  return (
    <section className="settings-block">
      <Text component="h2" fz="var(--lc-fz-section)" fw={600} c="var(--lc-text-1)" m={0}>
        Куда заводить заявки
      </Text>
      <Text fz="sm" c="var(--lc-text-2)">
        Направление заявки бот определяет сам — по проблеме клиента в переписке:
        телевизоры и техника уходят в БТ, компьютеры и принтеры — в КП. Не нашлось слов
        техники — смотрит объявление, в которое написал клиент. Выбор здесь —
        запасной: он срабатывает, только когда не сработали оба пути выше, а название ни
        на что не похоже. Если не сработали оба пути, заявка придерживается — и это видно
        в журнале ниже, а не пропадает молча.
      </Text>

      {/* Рамка ОДНА на весь список, разделители — у строк. Рамка на каждой
          строке складывалась бы с рамкой соседа в двойную линию: тот же довод,
          что у списка каналов (`.accounts-page__grid`). */}
      {каналы.length > 0 && (
        <div className="leads-channels">
          {каналы.map((account) => (
            <ChannelRow
              key={account.id}
              account={account}
              onError={setError}
              saveSrc={save}
              saveFields={saveFields}
            />
          ))}
        </div>
      )}
      <Stack gap="xs">
        {каналы.length === 0 && (
          <Text fz="sm" c="var(--lc-text-3)">
            Каналов пока нет
          </Text>
        )}
        {error && (
          <Text fz="sm" c="var(--lc-danger-text)" role="alert">
            {error}
          </Text>
        )}
      </Stack>
    </section>
  );
}

function JournalBlock() {
  const state = useLeadsState();
  const handouts = useHandouts();
  const rows = handouts.data?.items ?? [];
  const придержаны = handouts.data?.held_back ?? [];
  const stats = state.data?.stats ?? {};

  return (
    <section className="settings-block">
      <Text component="h2" fz="var(--lc-fz-section)" fw={600} c="var(--lc-text-1)" m={0}>
        Что ушло в лид-центры
      </Text>
      <Text fz="sm" c="var(--lc-text-2)">
        По строке на каждый отданный лид. «Отдали» и «заявка создана» — разные вещи, и здесь
        они не смешаны.
      </Text>

      {/*
        ЧИСЛО СВЕРХУ И КРУПНО, ПОДПИСЬ ПОД НИМ И МЕЛКО.

        Было наоборот по обоим счетам: подпись стояла первой, а число под ней
        набиралось `fz="lg"` — 18 пикселей, ступени, которой в шкале нет вовсе
        (есть 16 и 20). Полтора кегля разницы с подписью взгляд не ловит, и два
        единственных числа экрана читались как продолжение текста. Кегли и
        порядок теперь задаёт `.leads-stat*` (leads.css) — там же записано,
        почему именно `--lc-fz-metric`.
      */}
      <div className="leads-stats">
        <div className="leads-stat">
          <span className="leads-stat__value lc-num">
            {/* «—» вместо нуля: ноль здесь — утверждение, а мы его не знаем. */}
            {state.isError ? "—" : (stats.total ?? 0)}
          </span>
          <span className="leads-stat__label">Отдано всего</span>
        </div>
        <div className="leads-stat">
          <span className="leads-stat__value lc-num">
            {state.isError ? "—" : (stats.unacked ?? 0)}
          </span>
          <span className="leads-stat__label">Ждут ответа расширения</span>
        </div>
      </div>

      {/*
        ⚠ НУЛИ И «ПОКА НИЧЕГО НЕ ОТДАВАЛИ» — ЭТО УТВЕРЖДЕНИЕ О МИРЕ (28.08).
        Состояния запросов не читались вовсе: при отказе `data` остаётся
        undefined, и экран печатал «Отдано всего 0», «Ждут ответа расширения 0»
        и «Пока ничего не отдавали», да ещё объяснял, при каких условиях лид
        появится. Владелец приходит сюда ровно с вопросом «почему в лид-центрах
        мало заявок» — и уходит с ответом «заявок не было, всё спокойно», хотя
        панель просто не загрузилась. Признака неудачи не было нигде: блок связи
        при том же отказе молча исчезает.
      */}
      {handouts.isError || state.isError ? (
        <Text fz="sm" c="var(--lc-danger)" mt="sm">
          Не удалось загрузить журнал автозаявок — это сбой связи, а не отсутствие заявок.
          Числа выше показывать не с чего. Обновите страницу; если повторится, проверьте
          доступность сервера.
        </Text>
      ) : rows.length === 0 ? (
        <div className="leads-empty">
          <div className="leads-empty__title">Пока ничего не отдавали</div>
          <div className="leads-empty__hint">
            Лид появится, когда диалогу поставят итог «Выезд» и у клиента будет известен
            телефон.
          </div>
        </div>
      ) : (
        <ul className="leads-log">
          {rows.map((row) => (
            <li className="leads-log__item" key={row.conversation_id} data-tone={toneOf(row)}>
              <div className="leads-log__head">
                <time dateTime={row.handed_at}>{moment(row.handed_at)}</time>
                <span className="leads-log__src">{row.src_label}</span>
                <span className="leads-log__state">
                  {row.acked ? (row.decision_label ?? row.decision) : "ждём ответа расширения"}
                </span>
              </div>
              {/* Номер показываем, только если его нет в словах расширения:
                  оно обычно пишет «Заявка №1234567 создана…», и отдельная
                  строка с тем же номером читается сбоем, а не подробностью. */}
              {row.request_id && !(row.message ?? "").includes(row.request_id) && (
                <div className="leads-log__note">Заявка №{row.request_id}</div>
              )}
              {row.message && <div className="leads-log__note">{row.message}</div>}
              {/* Роутером, а не `<a href>`: обычная ссылка перезагружала всё
                  приложение (проверка 24.09). */}
              <Link
                className="leads-log__link"
                to={`/chats/${row.conversation_id}`}
                onClick={() => выбралСам(row.conversation_id)}
              >
                Открыть диалог
              </Link>
            </li>
          ))}
        </ul>
      )}

      {/*
        ⚠ ПРИДЕРЖАННЫЕ — ЗДЕСЬ, И РАНЬШЕ ИХ НЕ БЫЛО НИГДЕ (28.08).
        Подпись выше по странице обещает: «Если не сработали оба пути, заявка
        придерживается — и это видно в журнале ниже, а не пропадает молча».
        Обещание не выполнялось: придержанный лид не создаёт строки выдачи, и в
        списке над этим блоком его быть не могло, а сервер писал такие случаи
        только к себе в лог. По замеру владельца из шести диалогов с итогом
        «Выезд» телефон есть у двух — две трети заявок висели невидимыми, и
        человек, пришедший разбираться «почему в лид-центрах пусто», уходил ни с
        чем.

        Причина у каждой строки СВОЯ и написана словами: у «нет телефона» и «город
        не распознан» разная починка, и общее «не получилось» их бы склеило.
      */}
      {придержаны.length > 0 && (
        <>
          <Text component="h3" fz="sm" fw={600} c="var(--lc-text-1)" mt="lg" mb={4}>
            Придержаны — заявка не уйдёт, пока не поправить ({придержаны.length})
          </Text>
          <ul className="leads-log">
            {придержаны.map((h) => (
              <li className="leads-log__item" key={h.conversation_id} data-tone="warn">
                <div className="leads-log__head">
                  <span className="leads-log__src">{h.account_title}</span>
                  <span className="leads-log__state">{h.client_name ?? "Клиент"}</span>
                </div>
                <div className="leads-log__note">{h.reason}</div>
                <Link
                  className="leads-log__link"
                  to={`/chats/${h.conversation_id}`}
                  onClick={() => выбралСам(h.conversation_id)}
                >
                  Открыть диалог
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function LeadsTab() {
  return (
    <div className="settings-section">
      <PageHeader
        title="Автозаявки"
        description="Диалоги с итогом «Выезд» уходят в лид-центры заявками — без ручного переноса"
      />
      <ConnectionBlock />
      <ChannelsBlock />
      <JournalBlock />
    </div>
  );
}
