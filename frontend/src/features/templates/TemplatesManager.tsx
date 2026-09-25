import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Alert, Button, Group, Menu, Modal, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { qk } from "@/shared/api/queryKeys";
import type { TemplateDto, TemplateInput } from "@/shared/api/types";
import { usePermissions } from "@/shared/auth/usePermissions";
import { EmptyState } from "@/shared/ui/EmptyState";
import { plural } from "@/shared/lib/plural";
import { createTemplate, deleteTemplate, updateTemplate } from "./api";
import { названиеПоТексту } from "./название";
import { разобратьПодстановки } from "./vars";
import { useTemplateFolders, useTemplates } from "./useTemplates";
import "./templates.css";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";
import { IconMore } from "@/shared/ui/Icon";

/**
 * Управление быстрыми ответами (11 §3.2/§3.3). Один и тот же табличный UI в двух
 * местах продукта: раздел `/settings/templates` (право `templates:shared`) и
 * вкладка «Быстрые ответы» в `/settings/profile` (`personalOnly`).
 *
 * ПРЕЖНИЙ КОММЕНТАРИЙ ЗДЕСЬ ВРАЛ, и врал дважды. Он утверждал, что общие живут в
 * разделе, а личные — «блоком» в профиле. На деле в разделе есть вкладка «Мои»
 * (`canShare` → две вкладки ниже), и она читает ТОТ ЖЕ ключ кэша
 * `qk.templates.list("personal")`, что и профиль, — то есть личные быстрые
 * ответы у администратора и руководителя лежат в двух местах одновременно
 * (TPL-06). А в профиле это давно не блок, а вкладка. Вкладку в разделе завели
 * позже, комментарий не поправили — типовая история этого кода.
 *
 * Что здесь можно, а чего нельзя: единственный честный выход — убрать вкладку из
 * профиля и открыть раздел по праву `templates:own`, чтобы менеджер нашёл
 * быстрые ответы там, где их называют. Оба места правки — `ProfilePage.tsx` и
 * `router.tsx`/`SettingsLayout.tsx` — за пределами этого компонента, поэтому
 * дубль пока жив. Здесь сделана та половина, что принадлежит компоненту:
 * `personalOnly` схлопывает интерфейс до одной вкладки без единого упоминания
 * общих, и комментарий больше не врёт.
 *
 * Различие областей — только в переключателе «Общий»: перевести личный в общий
 * нельзя (01 §7.4).
 *
 * СЛОВАРЬ (10 §7.2). Сущность здесь называется БЫСТРЫМ ОТВЕТОМ, и только так:
 * «шаблон» словарь разрешает ровно в одном месте — как имя раздела настроек.
 * Внутри экрана это слово стояло одиннадцать раз («+ Шаблон», «Шаблон создан»,
 * «Общих шаблонов нет»), а на соседнем экране, в панели ввода, та же вещь
 * называлась быстрым ответом — то есть два имени у одной кнопки, и оператор
 * смены не мог понять, одно это или разное. Заодно ушёл «пикер» из
 * предупреждения об удалении: слова нет ни в словаре, ни в русском языке
 * диспетчерской.
 */

const VARIABLES = ["{имя}", "{менеджер}", "{объявление}"];

/**
 * Текст заготовки с подсвеченными подстановками.
 *
 * ⚠ ПОЧЕМУ ЯНТАРЬ И ПОЧЕМУ ИМЕННО ЭТОТ ТОКЕН. `--lc-note-placeholder` — тот же
 * цвет, которым в поле ввода светится заглушка заметки, то есть человек уже
 * знает: янтарным помечено то, что предстоит дописать. Заводить подсветке свой
 * цвет значило бы объяснять то же самое второй раз и другими словами.
 *
 * ⚠ БЕЗ ПОДСТАНОВОК ОТДАЁМ СТРОКУ СТРОКОЙ. Обернуть каждую заготовку в набор
 * кусков стоило бы лишних узлов на всём списке (двести строк — потолок
 * запроса) ради ничего: подстановки есть у меньшинства.
 */
function сПодсветкой(текст: string) {
  const куски = разобратьПодстановки(текст);
  if (!куски.some((к) => к.подстановка)) return текст;
  return куски.map((к, i) =>
    к.подстановка ? (
      <span key={i} className="tpl-table__ph">
        {к.текст}
      </span>
    ) : (
      к.текст
    ),
  );
}

export interface EditorState {
  /** null — создание (в т.ч. «Дублировать»: поля предзаполнены, id новый). */
  editingId: string | null;
  initial: { title: string; body: string; folder: string };
  /** Создаём общий (true) или личный (false) — при редактировании область не меняется. */
  shared: boolean;
}

function editorFor(t: TemplateDto | null, shared: boolean, duplicate = false): EditorState {
  return {
    editingId: duplicate || !t ? null : t.id,
    initial: {
      title: t ? (duplicate ? `${t.title} (копия)` : t.title) : "",
      body: t?.body ?? "",
      folder: t?.folder ?? "",
    },
    shared,
  };
}

export function TemplateEditor({
  state,
  folders,
  onClose,
}: {
  state: EditorState;
  folders: string[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [title, setTitle] = useState(state.initial.title);
  const [body, setBody] = useState(state.initial.body);
  const [folder, setFolder] = useState(state.initial.folder);
  /*
   * ПОДСКАЗКА ПАПОК — С СЕРВЕРА, А НЕ ИЗ ЗАГРУЖЕННЫХ ДВУХСОТ.
   *
   * Список шаблонов берёт первые двести (`TEMPLATES_LIMIT`), и папки, собранные
   * из него, неполны ровно так же. Для ФИЛЬТРА это правильно: показать папку,
   * шаблонов которой на экране нет, значит обещать пустой результат. А вот в
   * редакторе неполнота дорога иначе — не увидев существующую папку в
   * подсказке, человек наберёт её заново и с опечаткой заведёт вторую:
   * «Возражения» и «Возражения ». Разъезд тихий, замечают его недели спустя.
   *
   * Ручка `/templates/folders` отдаёт ВСЕ папки и написана ровно под это
   * (01 §7.2), хук `useTemplateFolders` — тоже. Оба лежали неиспользованными:
   * найдено 27.08 обходом мёртвых экспортов.
   */
  const серверные = useTemplateFolders();
  const [confirmingClose, setConfirmingClose] = useState(false);

  const save = useMutation({
    mutationFn: (input: TemplateInput) =>
      state.editingId ? updateTemplate(state.editingId, input) : createTemplate(input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.templates.root });
      showToast({
        message: state.editingId ? "Быстрый ответ обновлён" : "Быстрый ответ создан",
        color: "lp",
      });
      onClose();
    },
    // Окно НЕ закрывается: текст остаётся в полях, и «Сохранить» рядом. Тост
    // говорит, что делать дальше, а не что сломалось внутри.
    onError: (err) =>
        showToast(describeError({ where: "Не сохранилось", error: err, fallback: "Проверьте соединение и нажмите «Сохранить» ещё раз — текст никуда не делся" })),
  });

  /*
   * ВСТАВКА ПЕРЕМЕННОЙ ИДЁТ В КАРЕТКУ, А НЕ В КОНЕЦ ТЕКСТА.
   *
   * Кнопки подписаны «вставить:», но раньше делали `body + v` — всегда в конец
   * (TPL-03). Поставил курсор после «Здравствуйте, », нажал {имя} — и переменная
   * уехала за последний абзац, в готовый ответ клиенту, который потом уходит на
   * Авито как есть. Подпись обещала одно, кнопка делала другое.
   *
   * Три подпорки, каждая закрывает свой промах:
   *
   * 1. `onMouseDown` с preventDefault на кнопке. Нажатие на кнопку по умолчанию
   *    забирает фокус из поля, и к моменту onClick каретка в `selectionStart`
   *    уже недостоверна. Гасим — фокус остаётся в тексте, и человек видит, куда
   *    попала переменная.
   * 2. `lastCaret` — где стояла каретка, когда из поля всё-таки ушли фокусом
   *    (например, по Tab к кнопке с клавиатуры). Иначе вставка у клавиатурного
   *    пользователя валилась бы в начало: у расфокусированного поля
   *    `selectionStart` равен нулю, а не «там, где я остановился».
   * 3. Если в поле не заходили ни разу, каретки нет вовсе — дописываем в конец,
   *    как и раньше. Это единственный случай, где прежнее поведение верное.
   *
   * Каретку после вставки ставим в эффекте, а не сразу: `setSelectionRange` до
   * перерисовки упрётся в СТАРУЮ длину значения и обрежется по ней.
   */
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const lastCaret = useRef<{ from: number; to: number } | null>(null);
  const caretAfterInsert = useRef<number | null>(null);

  useEffect(() => {
    const pos = caretAfterInsert.current;
    if (pos === null) return;
    caretAfterInsert.current = null;
    const el = bodyRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(pos, pos);
  }, [body]);

  const insertVariable = (v: string) => {
    const el = bodyRef.current;
    const live =
      el && document.activeElement === el ? { from: el.selectionStart, to: el.selectionEnd } : null;
    const { from, to } = live ?? lastCaret.current ?? { from: body.length, to: body.length };
    setBody(`${body.slice(0, from)}${v}${body.slice(to)}`);
    caretAfterInsert.current = from + v.length;
    lastCaret.current = { from: from + v.length, to: from + v.length };
  };

  /*
   * НЕ ТЕРЯТЬ НАБРАННОЕ. Раньше Esc, щелчок мимо окна и «Отмена» закрывали
   * редактор молча, а черновик не сохранялся нигде. В это поле вбивают готовый
   * ответ клиенту на несколько абзацев — его пишут один раз, и промах мимо
   * окна стоил всего текста, причём человек узнавал об этом только по пустому
   * списку.
   *
   * Способ ровно тот же, что в «Назначить операторов на аккаунт»
   * (features/settings/accounts/AssignOperatorsModal.tsx): крестик, Esc, клик
   * мимо и «Отмена» ведут в одну ручку, а при непустых правках вместо закрытия
   * поднимается предупреждение внутри окна. Второго способа в разделе заводить
   * нельзя: два разных диалога про одно и то же читаются как два разных
   * последствия.
   *
   * Сравниваем с `state.initial`, а не с «поле непустое»: при редактировании
   * поля заполнены с самого начала, и «непустое» означало бы вопрос на каждом
   * закрытии, в том числе когда человек ничего не трогал.
   */
  const dirty =
    title !== state.initial.title ||
    body !== state.initial.body ||
    folder !== state.initial.folder;

  const requestClose = () => {
    if (save.isPending) return;
    if (dirty) {
      setConfirmingClose(true);
      return;
    }
    onClose();
  };

  return (
    <Modal
      opened
      onClose={requestClose}
      title={state.editingId ? "Редактировать быстрый ответ" : "Новый быстрый ответ"}
      centered
      size="lg"
    >
      {confirmingClose && (
        <Alert color="yellow" variant="light" title="Изменения не сохранены" mb="var(--lc-space-3)">
          <Stack gap="var(--lc-space-2)">
            <Text fz="sm">Закрыть редактор и потерять набранный текст?</Text>
            <Group gap="var(--lc-space-2)">
              <Button size="xs" variant="default" onClick={() => setConfirmingClose(false)}>
                Остаться
              </Button>
              <Button size="xs" color="red" onClick={onClose}>
                Закрыть без сохранения
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}

      {/*
        ⚠ НАЗВАНИЕ БОЛЬШЕ НЕ ОБЯЗАТЕЛЬНО (замер боя 03.09). Оно требовалось —
        и 25 личных заготовок из 26 ПОВТОРЯЛИ ТЕЛО слово в слово. То есть поле
        не несло смысла, оно несло работу: чтобы сохранить фразу, человек
        обязан был придумать ей ярлык и придумывал ровно ту же фразу заново.
        Личные заготовки за всё время завели двое из пятидесяти семи, и лишний
        обязательный шаг стоял ровно там, где люди отваливаются.

        Кому ярлык нужен, тот его пишет: «Территориально не подходит»
        читается с одного взгляда, первая строка длинного текста — нет.
      */}
      <TextInput
        data-autofocus
        label="Название"
        description="Необязательно — можно оставить пустым, тогда возьмём начало текста"
        placeholder={названиеПоТексту(body) || "начало текста"}
        value={title}
        onChange={(e) => setTitle(e.currentTarget.value)}
        mb="var(--lc-space-3)"
      />
      <Textarea
        label="Текст"
        autosize
        minRows={4}
        maxRows={10}
        ref={bodyRef}
        value={body}
        onChange={(e) => setBody(e.currentTarget.value)}
        onBlur={(e) => {
          lastCaret.current = { from: e.currentTarget.selectionStart, to: e.currentTarget.selectionEnd };
        }}
      />
      <Group gap="var(--lc-space-2)" mt={6}>
        {/*
          Счётчик склоняется: было «1 символов». Экран показывает его на каждой
          букве, то есть ошибка мозолила глаз чаще любой другой строки продукта.
          Один хелпер на приложение (shared/lib/plural.ts) — правило одно.

          `aria-hidden` — счётчик меняется на КАЖДОЙ букве, и живая область его
          бы зачитывала посимвольно; ограничения длины у поля всё равно нет.
        */}
        <Text fz="xs" c="var(--lc-text-3)" aria-hidden="true">
          {body.length} {plural(body.length, "символ", "символа", "символов")} · вставить:
        </Text>
        {VARIABLES.map((v) => (
          <Button
            key={v}
            size="compact-xs"
            variant="subtle"
            // Имя для скринридера: «{имя}» в фигурных скобках он зачитывает как
            // набор знаков, и из одной кнопки в ряду не понять, что она делает.
            aria-label={`Вставить переменную ${v}`}
            // Не отбирать фокус у поля — иначе каретка теряется, см. выше.
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => insertVariable(v)}
          >
            {v}
          </Button>
        ))}
      </Group>

      <TextInput
        label="Папка"
        description="Необязательно. В подсказке — все папки, которые уже есть"
        placeholder="без папки"
        list="lc-template-folders"
        value={folder}
        onChange={(e) => setFolder(e.currentTarget.value)}
        mt="var(--lc-space-3)"
      />
      <datalist id="lc-template-folders">
        {/* Локальные тоже оставляем: пока серверный список едет (или если ручка
            недоступна), подсказка не должна пропадать совсем. */}
        {/* Папки разделены по области: у общих шаблонов свои, у личных свои
            (`TemplateFolders`). Подсказывать чужую область нельзя — папка
            «Возражения» у соседа не сделает её моей. */}
        {Array.from(
          new Set([
            ...((state.shared ? серверные.data?.shared : серверные.data?.personal) ?? []),
            ...folders,
          ]),
        )
          .sort((a, b) => a.localeCompare(b, "ru"))
          .map((f) => (
            <option key={f} value={f} />
          ))}
      </datalist>

      {/*
        ⚠ ПРАВИЛО СТОИТ ТАМ, ГДЕ ПО НЕМУ МОГЛИ БЫ КЛИКНУТЬ.
        Область (общий или личный) не меняется после создания — 01 §7.4, PATCH
        поля `shared` не принимает вовсе. Сказано об этом было одной серой
        строкой в самом низу окна, под текстом и папкой: то есть ниже всего,
        что человек читает, и без единого намёка на то, ЧТО именно нельзя.
        Личных заготовок за всё время завели двое из пятидесяти семи — цена
        непонятого правила здесь не гипотетическая.

        Теперь обе половины видны рядом: выбранная и закрытая. Кликать не по
        чему намеренно — переключателя, который бы отказывал по нажатию, тут
        быть не должно: обещание, которое интерфейс не выполняет, хуже
        отсутствующего (тот же довод, что у вкладок выше про роли ARIA).
        Читалке состояние называется словами: цвет её не касается.
      */}
      <div className="tpl-scope" role="group" aria-labelledby="tpl-scope-label">
        <span className="tpl-scope__lbl" id="tpl-scope-label">
          Кто видит
        </span>
        <div className="tpl-scope__seg">
          <span className="tpl-scope__opt" data-active={state.shared || undefined}>
            <span className="tpl-scope__dot" />
            Общий
            <span className="lc-visually-hidden">{state.shared ? " — выбрано" : " — недоступно"}</span>
          </span>
          <span className="tpl-scope__opt" data-active={!state.shared || undefined}>
            <span className="tpl-scope__dot" />
            Личный
            <span className="lc-visually-hidden">{state.shared ? " — недоступно" : " — выбрано"}</span>
          </span>
        </div>
        <p className="tpl-scope__hint">
          {state.shared ? "Общий — его увидит вся команда" : "Личный — видите только вы"}.{" "}
          {state.editingId
            ? "Область у готового ответа не меняется: перевести личный в общий нельзя, только создать новый"
            : "Область задаёт вкладка, на которой вы стоите, и после сохранения она не меняется"}
        </p>
      </div>

      <Group justify="flex-end" mt="var(--lc-space-4)">
        <Button variant="subtle" onClick={requestClose} disabled={save.isPending}>
          Отмена
        </Button>
        <Button
          loading={save.isPending}
          disabled={!body.trim()}
          onClick={() =>
            save.mutate({
              // Сервер названия без текста не примет, и правильно: пустая
              // строка в списке — это строка, по которой ничего не найдёшь.
              title: title.trim() || названиеПоТексту(body),
              body: body.trim(),
              folder: folder.trim() || null,
              ...(state.editingId ? null : { shared: state.shared }),
            })
          }
        >
          Сохранить
        </Button>
      </Group>
    </Modal>
  );
}

export function TemplatesManager({ personalOnly = false }: { personalOnly?: boolean }) {
  const qc = useQueryClient();
  const { can } = usePermissions();
  const canShare = can("templates:shared") && !personalOnly;

  const [tab, setTab] = useState<"shared" | "personal">(canShare ? "shared" : "personal");
  const [query, setQuery] = useState("");
  const [folder, setFolder] = useState<string | null>(null);

  /*
   * ПАПКА СБРАСЫВАЕТСЯ ПРИ СМЕНЕ ВКЛАДКИ, ПОИСК — НЕТ. Разница не прихоть.
   *
   * Полоса папок собирается из шаблонов ТЕКУЩЕЙ вкладки, а папки у общих и у
   * личных свои. Выбрал «Отказы» в «Общих», перешёл в «Мои» — фильтр остаётся
   * включённым, но кнопки с такой папкой в полосе уже нет: ни одна не подсвечена,
   * «Все» тоже не подсвечена, а список пуст. Отключить фильтр нечем — его на
   * экране физически нет, и это читалось как «личных шаблонов нет» (TPL-01).
   *
   * Поиск другое дело: его строка стоит на месте вместе с набранным текстом,
   * невидимым он не бывает. И ходят с ним осмысленно — «где у меня ответ про
   * гарантию, в общих или в моих»; сбрасывать такой поиск на каждом переходе
   * значило бы заставлять набирать его заново.
   */
  const switchTab = (next: "shared" | "personal") => {
    setTab(next);
    setFolder(null);
  };
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<TemplateDto | null>(null);

  const scope = personalOnly ? "personal" : tab;
  const list = useTemplates(scope);

  const remove = useMutation({
    mutationFn: (id: string) => deleteTemplate(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.templates.root });
      showToast({ message: "Быстрый ответ удалён", color: "lp" });
      setConfirmDelete(null);
    },
    onError: (err) =>
        showToast(describeError({ where: "Не удалилось", error: err, fallback: "Проверьте соединение и нажмите «Удалить» ещё раз" })),
  });

  const items = useMemo(() => list.data?.items ?? [], [list.data]);

  const folders = useMemo(() => {
    const set = new Set<string>();
    for (const t of items) if (t.folder) set.add(t.folder);
    return Array.from(set).sort((a, b) => a.localeCompare(b, "ru"));
  }, [items]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return items.filter((t) => {
      if (folder !== null && (t.folder ?? "") !== folder) return false;
      if (!needle) return true;
      return t.title.toLowerCase().includes(needle) || t.body.toLowerCase().includes(needle);
    });
  }, [items, query, folder]);

  /** Пустота из-за фильтра, а не из-за отсутствия быстрых ответов. */
  const filtering = query.trim() !== "" || folder !== null;

  /*
   * СПИСОК МОЖЕТ БЫТЬ НЕПОЛНЫМ, И ОБ ЭТОМ НАДО СКАЗАТЬ (TPL-05).
   *
   * Запрос берёт первые двести (`TEMPLATES_LIMIT`), а поиск и папки считаются
   * ЛОКАЛЬНО — по тем самым двумстам. С двести первой записи часть библиотеки
   * переставала существовать для экрана: поиск по ней ничего не находил и
   * показывал «Ничего не нашлось» — то есть отвечал уверенно и неверно.
   * Серверный поиск, обещанный комментарием в `useTemplates`, не подключён
   * нигде.
   *
   * Подключить его здесь — отдельная работа (нужен запрос на каждую букву с
   * задержкой, свой ключ кэша, объединение с папками). Пока её нет, экран
   * обязан хотя бы не врать: он честно говорит, сколько всего записей и что
   * поиск идёт не по всем. Молчаливый неверный ответ хуже громкого неполного.
   */
  const total = list.data?.page?.total ?? items.length;
  const truncated = total > items.length;

  const resetFilters = () => {
    setQuery("");
    setFolder(null);
  };

  const folderName = folder === "" ? "без папки" : `«${folder}»`;
  const emptyReason =
    query.trim() && folder !== null
      ? `По запросу «${query.trim()}» в папке ${folderName} пусто`
      : query.trim()
        ? `По запросу «${query.trim()}» ничего нет`
        : `В папке ${folderName} пусто`;

  /*
   * ВКЛАДКИ — НАСТОЯЩИЕ ВКЛАДКИ, А НЕ ДВЕ КНОПКИ С `role="tab"`.
   *
   * Было: `role="tablist"` и два `role="tab"` — и всё. Для программы чтения с
   * экрана это обещание, которое интерфейс не выполнял: у вкладки не было
   * `aria-controls`, панели с `role="tabpanel"` не существовало вовсе, а
   * стрелками между вкладками не переключиться (по образцу WAI-ARIA внутри
   * tablist ходят стрелками, а Tab уводит СРАЗУ в панель). Получалось хуже, чем
   * если бы ролей не было: объявлено «вкладка 1 из 2», а ведёт себя как кнопка,
   * и куда делось содержимое второй — неизвестно.
   *
   * Отсюда три вещи: перекатывающийся `tabIndex` (в списке ровно одна
   * достижимая Tab'ом вкладка), стрелки влево-вправо с закольцовкой, и панель
   * ниже, подписанная активной вкладкой.
   */
  const TAB_IDS = ["shared", "personal"] as const;

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    e.preventDefault();
    const i = TAB_IDS.indexOf(tab);
    const next = TAB_IDS[(i + step + TAB_IDS.length) % TAB_IDS.length]!;
    switchTab(next);
    // Фокус едет за выделением: иначе следующая стрелка уйдёт от старой
    // кнопки, и человек будет перебирать вкладки, стоя на одном месте.
    document.getElementById(`tpl-tab-${next}`)?.focus();
  };

  return (
    <div className="tpl-manager">
      <div className="tpl-manager__bar">
        {canShare && (
          <div className="tpl-manager__tabs" role="tablist" aria-label="Чьи быстрые ответы">
            {TAB_IDS.map((id) => (
              <button
                key={id}
                type="button"
                role="tab"
                id={`tpl-tab-${id}`}
                aria-controls="tpl-tabpanel"
                aria-selected={tab === id}
                tabIndex={tab === id ? 0 : -1}
                data-active={tab === id || undefined}
                onKeyDown={onTabKeyDown}
                onClick={() => switchTab(id)}
              >
                {id === "shared" ? "Общие" : "Мои"}
              </button>
            ))}
          </div>
        )}
        <TextInput
          size="xs"
          /*
           * «Поиск по названию и тексту», а не «поиск».
           *
           * Строчная буква была единственной на весь продукт среди подписей
           * полей: рядом «Поиск: имя, телефон, текст», «Имя или
           * email», «Статус: любой» — все с прописной. Разнобой в одном месте
           * из двадцати читается не как стиль, а как недоделка.
           *
           * Заодно слово перестало умалчивать: поле ищет и по названию, и по
           * тексту ответа (см. `visible` выше), а «поиск» об этом не говорил —
           * человек набирал название, не находил и делал вывод, что по тексту
           * искать нельзя.
           */
          placeholder="Поиск по названию и тексту"
          aria-label="Поиск по быстрым ответам"
          value={query}
          onChange={(e) => setQuery(e.currentTarget.value)}
        />
        <span className="tpl-manager__spacer" />
        <Button
          size="xs"
          onClick={() => setEditor(editorFor(null, scope === "shared"))}
        >
          {/* Кнопка называет ДЕЙСТВИЕ, а не предмет. «Быстрый ответ» рядом со
              списком быстрых ответов читается как заголовок или фильтр, и понять,
              что нажатие создаёт новый, можно было только нажав. */}
          Создать быстрый ответ
        </Button>
      </div>

      <div
        id="tpl-tabpanel"
        {...(canShare
          ? { role: "tabpanel" as const, "aria-labelledby": `tpl-tab-${tab}` }
          : {})}
        className="tpl-manager__panel"
      >
      {folders.length > 0 && (
        /* Полоса папок — фильтр, а не вкладки: одна кнопка нажата, остальные
           доступны, и роль здесь именно кнопочная. `aria-pressed` называет
           состояние вслух — иначе о выбранной папке говорит только цвет. */
        <div className="tpl-manager__folders" role="group" aria-label="Фильтр по папке">
          <button
            type="button"
            aria-pressed={folder === null}
            data-active={folder === null || undefined}
            onClick={() => setFolder(null)}
          >
            Все
          </button>
          {folders.map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={folder === f}
              data-active={folder === f || undefined}
              onClick={() => setFolder(f)}
            >
              {f}
            </button>
          ))}
          <button
            type="button"
            aria-pressed={folder === ""}
            data-active={folder === "" || undefined}
            onClick={() => setFolder("")}
          >
            без папки
          </button>
        </div>
      )}

      {truncated && (
        <Alert color="yellow" variant="light" role="status">
          Показаны первые {items.length} из {total} — поиск и папки работают только по ним.
          Удалите ненужные, чтобы список поместился целиком.
        </Alert>
      )}

      {list.isPending ? (
        <div className="tpl-manager__skeleton" aria-hidden="true">
          {Array.from({ length: 5 }, (_, i) => (
            <span key={i} />
          ))}
        </div>
      ) : list.isError ? (
        <EmptyState
          illustration="error"
          title="Не получилось загрузить быстрые ответы"
          description="Нажмите «Повторить». Если не помогает — обновите страницу"
          live="alert"
          action={
            <Button variant="outline" size="xs" onClick={() => void list.refetch()}>
              Повторить
            </Button>
          }
        />
      ) : visible.length === 0 && filtering ? (
        /*
         * ОТФИЛЬТРОВАННАЯ ПУСТОТА — НЕ ТО ЖЕ, ЧТО ПУСТОЙ РАЗДЕЛ. Раньше обе
         * ветки давали один экран «Общих шаблонов нет. Создайте первые» — и
         * человек, у которого просто висел фильтр, шёл заводить шаблон, который
         * у него уже есть (TPL-01). Здесь названа причина пустоты и рядом
         * стоит кнопка, которая её снимает: снять фильтр папки иначе бывает
         * нечем — см. `switchTab`.
         */
        <EmptyState
          illustration="search"
          title="Ничего не нашлось"
          description={emptyReason}
          action={
            <Button variant="outline" size="xs" onClick={resetFilters}>
              Сбросить фильтры
            </Button>
          }
        />
      ) : visible.length === 0 ? (
        /*
         * ПУСТОТА НАЗЫВАЕТ ЧЕСТНУЮ ПРИЧИНУ. Стояло «Быстрые ответы экономят
         * десятки минут в день» — обещание пользы вместо ответа на вопрос
         * «почему тут пусто». Причина ровно одна и она проста: ещё ни одного не
         * создали. Сказав её, дальше можно объяснить, чем эта область
         * отличается от соседней вкладки, — а это и есть то, чего человек в
         * этот момент не знает.
         *
         * ⚠ И ЗДЕСЬ ЖЕ — ЕДИНСТВЕННОЕ ДЕЙСТВИЕ (05.09). Из трёх пустых
         * состояний экрана два уже отвечали кнопкой: «Ничего не нашлось» —
         * «Сбросить фильтры», «Не получилось загрузить» — «Повторить». А
         * то, где человек оказывается ЧАЩЕ ВСЕГО — «ещё ни одного не
         * создали», — не отвечало ничем: половина экрана объясняла, зачем
         * нужны заготовки, и отправляла искать кнопку глазами наверх, в
         * правый угол панели. Личные заготовки за всё время завели двое из
         * пятидесяти семи; лишний шаг стоял ровно там, где отваливаются.
         */
        <EmptyState
          illustration="bolt"
          title={scope === "shared" ? "Общих быстрых ответов нет" : "Личных быстрых ответов нет"}
          description={
            scope === "shared"
              ? "Их ещё никто не создал. Общий увидит вся команда — заготовьте ответы, которые пишете каждый день"
              : "Вы ещё не создали ни одного. Личный видите только вы, в диалоге он вставляется по «/»"
          }
          action={
            <Button onClick={() => setEditor(editorFor(null, scope === "shared"))}>
              {/* Подпись та же, что у кнопки в панели: одно действие — одно
                  имя, иначе они читаются как два разных. Размер здесь
                  обычный, а не `xs`: это единственное действие на пустом
                  экране, ему и место главного. */}
              Создать быстрый ответ
            </Button>
          }
        />
      ) : (
        <table className="lc-table tpl-table lc-table--cards">
          {/*
            `lc-table--cards` — на узком экране строка становится карточкой
            (тот же приём, что у таблицы сотрудников).

            БЕЗ НЕГО НА ТЕЛЕФОНЕ ЭКРАН БЫЛ НЕРАБОЧИМ. У таблицы
            `table-layout: fixed`, а ширины колонок заданы жёстко: 200 + 140 +
            40 = 380 пикселей на три колонки из четырёх. На экране в 390
            пикселей тексту шаблона доставалось ДЕСЯТЬ, и он просто исчезал —
            при этом ни прокрутки, ни обрезанного края видно не было, так что
            никакого признака беды экран не подавал.

            Поймано, когда починили стенд: `?w=` задавал ширину блока, а
            медиазапросы смотрят на ширину окна, и «проверка на 390px» через
            него не включала ни одного мобильного правила.
          */}
          {/*
            Ширины колонок объявлены ЗДЕСЬ, а не на ячейках тела.
            При `table-layout: fixed` браузер берёт ширины из ПЕРВОЙ строки —
            то есть из шапки. Пока они стояли только на `td`, шапка не задавала
            ничего, и все четыре колонки делили ширину поровну: узкая «Папка»
            получала столько же, сколько текст шаблона.
          */}
          <colgroup>
            <col className="tpl-col--title" />
            <col className="tpl-col--body" />
            <col className="tpl-col--folder" />
            <col className="tpl-col--used" />
            <col className="tpl-col--actions" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Название</th>
              <th scope="col">Текст</th>
              <th scope="col">Папка</th>
              {/*
                ЧАСТОТА УЖЕ КОПИТСЯ, НО ВИДНА БЫЛА ТОЛЬКО ПОДСКАЗКЕ.
                `used_count` поднимает ходовые заготовки наверх в списке по «/»
                (features/templates/списокПикера.ts), а в настройках — там, где
                библиотеку чистят, — числа не было вовсе. Между двумя похожими
                «Здравствуйте» выбирали наугад: какое из них живое, а какое
                завели однажды и забыли, экран не говорил.

                ⚠ «ЗА ВСЁ ВРЕМЯ», А НЕ «ЗА 30 ДНЕЙ». Счётчик на сервере —
                накопительный (app/models/template.py), окна у него нет, и
                обещать окно значило бы приписать числу смысл, которого в нём
                нет. Заголовку короткое слово, подробность — в подсказке.
              */}
              <th scope="col" className="tpl-th--used" title="Сколько раз ответ вставили в диалог за всё время">
                Вставок
              </th>
              <th scope="col" aria-label="Действия" />
            </tr>
          </thead>
          <tbody>
            {visible.map((t) => (
              <tr key={t.id}>
                {/*
                  ОБРЕЗАННОЕ ОБЯЗАНО ДОГОВАРИВАТЬ (docs/39 §5). Обе колонки
                  режутся многоточием — «Название» жёстко на 200 пикселях,
                  «Текст» на остатке — и прочитать целиком было НЕГДЕ: строка не
                  раскрывается, наведение ничего не показывает, а открывать
                  редактор ради того, чтобы увидеть конец фразы, никто не станет.
                  Хуже всего это било по двум ответам с одинаковым началом
                  («Здравствуйте! Мастер приедет…»): в списке они выглядели
                  одной и той же строкой.
                */}
                <td className="tpl-table__title" data-label="Название" title={t.title}>
                  {t.title}
                </td>
                {/* `title` — весь текст целиком, без разметки: подсказка
                    браузера показывает простую строку, и подсветка ей не
                    мешает. */}
                <td className="tpl-table__body" data-label="Текст" title={t.body}>
                  {сПодсветкой(t.body)}
                </td>
                {/*
                  Папка тоже обрезается многоточием — колонка узкая намеренно
                  (см. разбор в templates.css: без обрезки «Бригада Андрея
                  Владиславовича КП · Б6» раскладывалась в три строки и делала
                  ряд в полтора раза выше соседних). Раз режем — обязаны
                  договаривать, как название и текст рядом.
                */}
                <td
                  className="tpl-table__folder"
                  data-label="Папка"
                  title={t.folder ?? undefined}
                >
                  {t.folder ?? "—"}
                </td>
                {/*
                  НОЛЬ — ЭТО ОТВЕТ, А НЕ ПУСТОТА: заготовка, которую ни разу не
                  вставили, и есть кандидат на чистку, поэтому ноль пишем
                  цифрой. Прочерк оставлен ровно для одного случая — поля нет в
                  ответе сервера (сборка фронта переживает сервер постарше), и
                  тогда «0» был бы неправдой: мы не знаем, сколько их было.
                */}
                {/*
                  `data-unknown` отделяет «не знаем» от величины: столбец
                  набран крупнее и жирнее остальной строки (это единственный
                  показатель экрана), и прочерк в том же весе читался бы как
                  ещё одно значение — см. разбор у правила в templates.css.
                */}
                <td
                  className="tpl-table__used"
                  data-label="Вставок"
                  data-unknown={t.used_count === undefined || undefined}
                >
                  {t.used_count === undefined ? "—" : t.used_count}
                </td>
                <td className="tpl-table__actions">
                  <Menu position="bottom-end" withArrow>
                    <Menu.Target>
                      <button type="button" className="tpl-table__menu" aria-label={`Действия: ${t.title}`}>
                        <IconMore size={16} />
                      </button>
                    </Menu.Target>
                    <Menu.Dropdown>
                      <Menu.Item onClick={() => setEditor(editorFor(t, t.owner_id === null))}>
                        Редактировать
                      </Menu.Item>
                      <Menu.Item onClick={() => setEditor(editorFor(t, t.owner_id === null, true))}>
                        Дублировать
                      </Menu.Item>
                      <Menu.Item color="red" onClick={() => setConfirmDelete(t)}>
                        Удалить
                      </Menu.Item>
                    </Menu.Dropdown>
                  </Menu>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      </div>

      {editor && (
        <TemplateEditor
          // key — чтобы поля редактора переинициализировались под другой шаблон
          key={editor.editingId ?? `new-${editor.initial.title}`}
          state={editor}
          folders={folders}
          onClose={() => setEditor(null)}
        />
      )}

      <Modal
        opened={Boolean(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
        title="Удалить быстрый ответ?"
        centered
      >
        {/*
          ПОСЛЕДСТВИЕ НАЗЫВАЕТСЯ ПО ОБЛАСТИ ОТВЕТА. Один текст на оба случая
          пугал личный быстрый ответ чужой бедой: «исчезнет из пикера у всех,
          кто им пользуется» — а личный (`owner_id != null`) видит только
          владелец, у остальных его нет и не было (TPL-04). Ложное «у всех»
          останавливает руку там, где терять нечего, и обесценивает
          предупреждение там, где оно настоящее: общий правда пропадёт у
          тринадцати человек.

          «Пикер» из текста убран: это слово из кода, а не из смены. Человек
          знает не «пикер», а «список по кнопке ⚡ и по „/“», — так и написано.
        */}
        <Text fz="sm" c="var(--lc-text-2)">
          {confirmDelete?.owner_id === null
            ? `«${confirmDelete.title}» пропадёт из списка быстрых ответов у всей команды — вставить его в диалоге больше не получится`
            : `«${confirmDelete?.title}» пропадёт из вашего списка быстрых ответов. Он личный — у остальных его нет`}
        </Text>
        <Group justify="flex-end" mt="var(--lc-space-4)">
          <Button variant="subtle" onClick={() => setConfirmDelete(null)}>
            Отмена
          </Button>
          <Button
            color="red"
            loading={remove.isPending}
            onClick={() => confirmDelete && remove.mutate(confirmDelete.id)}
          >
            Удалить
          </Button>
        </Group>
      </Modal>
    </div>
  );
}
