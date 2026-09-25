import { useEffect, useState } from "react";
import { Button, Switch, Table, Text } from "@mantine/core";
import { ApiError, http } from "@/shared/api/http";
import { usePermissions } from "@/shared/auth/usePermissions";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { toast } from "@/shared/ui/toast";
import {
  bindingOf,
  formatBinding,
  type Binding,
} from "@/features/hotkeys/binding";
import {
  ACTIONS,
  actionLabel,
  assignableActions,
  действующие,
  type ActionId,
} from "@/features/hotkeys/catalog";
import { записьСочетания } from "@/features/hotkeys/useChatHotkeys";

/**
 * ПЕРЕНАЗНАЧЕНИЕ ГОРЯЧИХ КЛАВИШ (требование заказчика от 13 августа).
 *
 * ПОЧЕМУ «НАЖМИТЕ СОЧЕТАНИЕ», А НЕ ВЫБОР ИЗ СПИСКА. Список клавиш — это две сотни
 * пунктов, из которых человек ищет ту, что уже лежит под пальцем. Нажать её быстрее
 * и честнее: сразу видно, что система поняла именно это сочетание, включая
 * раскладку и модификаторы.
 *
 * ⚠ КОНФЛИКТ ПОКАЗЫВАЕМ, НО НЕ ЗАПРЕЩАЕМ. Запрет означал бы, что человек обязан
 * сначала освободить сочетание у другого действия, а потом вспомнить, зачем он сюда
 * пришёл. Вместо этого сочетание отбирается у прежнего владельца сразу — так же, как
 * это делает любой редактор, — а в строке того действия видно, что оно осталось без
 * клавиши.
 *
 * ЧЕГО ЗДЕСЬ НЕТ. Esc и «?» не переназначаются и в таблице не показаны: первый —
 * договор с браузером, второй открывает саму эту справку. Причины записаны в реестре.
 */
export function HotkeysEditor() {
  const { can } = usePermissions();
  const saved = useSessionStore((s) => s.hotkeys);
  const [draft, setDraft] = useState<Record<string, Binding[]>>(() => ({
    ...saved,
  }));
  const [capturing, setCapturing] = useState<ActionId | null>(null);
  /*
   * ⚠ ЧТО СТОЯЛО ДО ВЫКЛЮЧЕНИЯ — ЧТОБЫ «ВКЛЮЧИТЬ» ВЕРНУЛО ИМЕННО ЭТО.
   *
   * Просьба владельца 02.09: «сделать возможность отдельно выключать комбинации
   * клавиш». Выключение — это пустой список сочетаний: разбор нажатия молчит на
   * нём сам (`dispatch.actionFor`), поэтому ни схема, ни сервер не менялись.
   *
   * Но у человека могло стоять СВОЁ сочетание. Верни мы при включении
   * умолчание, его настройка пропала бы молча — а он про неё узнает только
   * тогда, когда привычная клавиша перестанет работать. Помним и возвращаем то,
   * что было.
   */
  const [доВыключения, setДоВыключения] = useState<Record<string, Binding[]>>(
    {},
  );

  /*
   * ⚠ ПОКА ЖДЁМ НАЖАТИЯ, ГЛОБАЛЬНЫЕ СОЧЕТАНИЯ МОЛЧАТ. Иначе переназначение
   * клавиши приёма ПРИНИМАЕТ живой диалог: человек жмёт «изменить», нажимает
   * Ctrl+R — и вместо записи сочетания оказывается в чужой переписке, а
   * настройки закрываются. Заслон приёма живёт на фазе перехвата и висит на
   * всём приложении, поэтому спросить его больше неоткуда.
   */
  useEffect(() => {
    записьСочетания(capturing !== null);
    return () => записьСочетания(false);
  }, [capturing]);
  const [busy, setBusy] = useState(false);

  const действия = assignableActions().filter((a) => !a.need || can(a.need));
  const действие = (id: ActionId) => ACTIONS.find((a) => a.id === id)!;
  const текущие = (id: ActionId): readonly Binding[] =>
    действующие(действие(id), draft);

  /** Кто ещё занял это сочетание — по всей таблице, включая неизменённые. */
  const занято = (b: Binding, кроме: ActionId): ActionId | null => {
    for (const a of ACTIONS) {
      if (a.id === кроме) continue;
      if (текущие(a.id).includes(b)) return a.id;
    }
    return null;
  };

  /** Сказать, у кого отобрали сочетание: молча пропавшую клавишу человек найдёт, только когда она не сработает. */
  const reportFreed = (id: ActionId) =>
    toast.info(`Сочетание освободилось у действия «${actionLabel(действие(id))}»`);

  /** Выключить действие: пустой список — это и есть «не срабатывает». */
  const выключить = (id: ActionId) => {
    setДоВыключения((было) => ({ ...было, [id]: [...текущие(id)] }));
    setDraft((было) => ({ ...было, [id]: [] }));
    setCapturing(null);
  };

  /** Включить обратно — тем, что стояло до выключения. */
  const включить = (id: ActionId) => {
    const прежние = доВыключения[id];
    /*
     * ⚠ ВОЗВРАЩАЕМ ЯВНО, А НЕ УДАЛЕНИЕМ КЛЮЧА (02.09). Раньше здесь стояло
     * `delete`: «нет своего — пусть действуют умолчания». С решением владельца
     * «клавиши изначально неактивны» умолчание у большинства действий стало
     * ПУСТЫМ, и удаление ключа означало бы «оставить выключенным» — кнопка
     * «включить» молча ничего бы не делала.
     *
     * Поэтому: помним личное — возвращаем его; не помним — ставим то
     * сочетание, которым это действие включается (`defaults`).
     */
    const restored = прежние && прежние.length ? прежние : действие(id).defaults;
    // Пока действие было выключено, его сочетание могли отдать другому. Отбираем
    // так же, как при наборе в `поймать`, иначе две строки покажут одну клавишу,
    // а сработает только верхняя в реестре (проверка 24.09).
    const taken = restored.flatMap((b) => {
      const owner = занято(b, id);
      return owner ? [{ owner, binding: b }] : [];
    });
    setDraft((было) => {
      const стало = { ...было, [id]: [...restored] };
      for (const { owner, binding } of taken) {
        стало[owner] = (стало[owner] ?? [...текущие(owner)]).filter((x) => x !== binding);
      }
      return стало;
    });
    for (const owner of new Set(taken.map((t) => t.owner))) reportFreed(owner);
  };

  const поймать = (id: ActionId) => (e: React.KeyboardEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") {
      setCapturing(null);
      return;
    }
    const b = bindingOf(e.nativeEvent);
    if (!b) return; // нажат один модификатор — ждём саму клавишу
    const конфликт = занято(b, id);
    setDraft((d) => {
      const next = { ...d, [id]: [b] };
      // Сочетание отбирается у прежнего владельца сразу: два действия на одной
      // клавише — это молчаливый спор, в котором побеждает порядок в реестре.
      if (конфликт) next[конфликт] = текущие(конфликт).filter((x) => x !== b);
      return next;
    });
    setCapturing(null);
    if (конфликт) reportFreed(конфликт);
  };

  const сохранить = async () => {
    setBusy(true);
    try {
      // Отправляем ТОЛЬКО отличия от умолчаний: иначе человек заморозил бы у себя
      // всю сегодняшнюю таблицу, и завтрашняя правка умолчаний до него не доехала бы.
      const отличия: Record<string, string[]> = {};
      for (const a of ACTIONS) {
        const свои = draft[a.id];
        if (!свои) continue;
        /*
         * ⚠ СРАВНИВАЕМ С ДЕЙСТВУЮЩИМ УМОЛЧАНИЕМ, А НЕ С `defaults` (02.09).
         *
         * У выключенных по умолчанию действий `defaults` — это «каким
         * сочетанием оно включается», а действует у них ПУСТО. Сравни мы с
         * `defaults`, включение привычного Ctrl+D сочли бы «ничем не отличается
         * от умолчания», не отправили на сервер — и клавиша осталась бы
         * выключенной, хотя человек её только что включил.
         */
        const как_было = действующие(a).join("|");
        if (свои.join("|") !== как_было) отличия[a.id] = свои;
      }
      const me = await http.put<{ hotkeys: Record<string, string[]> }>(
        "/auth/me/hotkeys",
        {
          hotkeys: отличия,
        },
      );
      useSessionStore.setState({ hotkeys: me.hotkeys ?? {} });
      setDraft({ ...(me.hotkeys ?? {}) });
      toast.success("Сочетания сохранены");
    } catch (e) {
      toast.error(
        e instanceof ApiError ? e.message : "Не получилось сохранить",
      );
    } finally {
      setBusy(false);
    }
  };

  const вернуть = async () => {
    setBusy(true);
    try {
      await http.put("/auth/me/hotkeys", { hotkeys: {} });
      useSessionStore.setState({ hotkeys: {} });
      setDraft({});
      toast.success("Вернули сочетания по умолчанию");
    } catch (e) {
      toast.error(
        e instanceof ApiError ? e.message : "Не получилось сохранить",
      );
    } finally {
      setBusy(false);
    }
  };

  const изменено = JSON.stringify(draft) !== JSON.stringify(saved);

  return (
    /*
     * ⚠ БЕЗ `settings-block` (разбор 05.09). Редактор рисовался вложенным
     * блоком внутри такого же блока «Горячие клавиши»: внутренний приносил
     * свою черту снизу и свой отступ 24 px, и внутри одного раздела появлялась
     * лишняя горизонтальная линия ниоткуда. Теперь он просто стопка внутри
     * карточки, а рамку и заголовок держит карточка.
     */
    <div className="prof-hotkeys">
      {/*
        ⚠ ВВОДНАЯ СТРОКА НАЗЫВАЕТ ВКЛЮЧЁННОЕ ПОИМЁННО, И ЭТО НЕ ПРИДИРКА.
        Здесь стояло «работает только приём диалога» — неправдой это стало ещё
        02.09, когда рядом включили закрытие, и совсем неправдой 09.09, когда
        из шапки ленты убрали шевроны листания и пара `listNext`/`listPrev`
        поехала в умолчания. Читатель сверяет эту строку с таблицей ниже: не
        сойдясь, она учит не верить всему разделу.
      */}
      <Text fz="sm" c="var(--lc-text-2)">
        По умолчанию работают четыре сочетания: принять диалог, закрыть его и
        листание диалогов Ctrl + ↓ / Ctrl + ↑. Остальные выключены, пока вы их
        не включите. Нажмите «Включить», чтобы вернуть привычное сочетание, или
        «Изменить» и наберите своё. Настройка привязана к вам и работает на
        любом компьютере, где вы войдёте.
      </Text>

      {/*
        ⚠ ТАБЛИЦА ПРОКРУЧИВАЕТСЯ ВБОК, А НЕ ОБРЕЗАЕТСЯ (замер стендом 05.09).
        На 375 она просит 495 px, а `.settings-content` объявлен
        `overflow: hidden` — то есть третий столбец с кнопкой «изменить» и
        переключателем просто исчезал за краем экрана, без полосы прокрутки и
        без единого признака, что там что-то есть. Девятнадцать сочетаний с
        телефона было не включить и не переназначить.
      */}
      <div className="prof-hotkeys__scroll">
        <Table striped withRowBorders={false} fz="sm">
          <Table.Tbody>
            {действия.map((a) => {
              const свои = текущие(a.id);
              return (
                <Table.Tr key={a.id}>
                  <Table.Td>{actionLabel(a)}</Table.Td>
                  <Table.Td
                    style={{ whiteSpace: "nowrap" }}
                    c={свои.length ? undefined : "var(--lc-text-3)"}
                  >
                    {свои.length
                      ? свои.map(formatBinding).join(" / ")
                      : "выключено"}
                  </Table.Td>
                  <Table.Td style={{ width: 1 }}>
                    <div
                      style={{
                        display: "flex",
                        gap: "var(--lc-space-3)",
                        justifyContent: "flex-end",
                        alignItems: "center",
                      }}
                    >
                      <Button
                        size="compact-xs"
                        variant={capturing === a.id ? "filled" : "subtle"}
                        onKeyDown={
                          capturing === a.id ? поймать(a.id) : undefined
                        }
                        onClick={() =>
                          setCapturing(capturing === a.id ? null : a.id)
                        }
                        aria-label={`Изменить сочетание: ${actionLabel(a)}`}
                      >
                        {capturing === a.id ? "жду нажатия…" : "изменить"}
                      </Button>
                      {/*
                      ⚠ ПЕРЕКЛЮЧАТЕЛЬ, А НЕ КНОПКА (просьба владельца 02.09
                      «сделай ползунком»). И дело не только в красоте: кнопка
                      «включить» показывает ДЕЙСТВИЕ, а переключатель —
                      СОСТОЯНИЕ. В таблице из девятнадцати строк, где почти всё
                      выключено, состояние читается с одного взгляда, а
                      одинаковые надписи «включить» приходится вычитывать.

                      Набор сочетания включает действие сам — отдельного
                      «включить и назначить» не нужно.
                    */}
                      <Switch
                        size="sm"
                        checked={свои.length > 0}
                        onChange={() =>
                          свои.length ? выключить(a.id) : включить(a.id)
                        }
                        aria-label={`${свои.length ? "Выключить" : "Включить"} сочетание: ${actionLabel(a)}`}
                      />
                    </div>
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </div>

      <div style={{ display: "flex", gap: "var(--lc-space-2)" }}>
        <Button
          size="xs"
          loading={busy}
          disabled={!изменено}
          onClick={() => void сохранить()}
        >
          Сохранить
        </Button>
        <Button
          size="xs"
          variant="default"
          loading={busy}
          onClick={() => void вернуть()}
        >
          Вернуть по умолчанию
        </Button>
      </div>
    </div>
  );
}
