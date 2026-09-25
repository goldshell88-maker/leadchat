import { useEffect, useMemo, useState } from "react";
import { Alert, Badge, Button, Checkbox, Group, Loader, Modal, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ROLE_LABELS } from "@/features/settings/team/roles";
import { ApiError } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type { AvitoAccountDto, ChannelOperatorCandidate } from "@/shared/api/types";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import { channelOperatorsKey, fetchChannelOperators, saveChannelOperators } from "./api";
import "./accounts.css";
import { showToast } from "@/shared/ui/toast";

/**
 * Экран «Назначить операторов на канал» — по образцу Jivo (15 §2.2), план 7.2.
 *
 * Зачем он вообще. У заказчика девять каналов Авито и тринадцать операторов,
 * и у каждого канала СВОЙ набор людей. Пока набора нет, очередь «Входящие»
 * (7.1) показывает всем всё: оператор листает чужие обращения и берёт не свои.
 * Этот экран — то место, где набор задаётся; всё остальное (фильтрация очереди
 * и состав тех, от чьего отказа зависит эскалация) считается по нему на сервере.
 *
 * Загружается ЛЕНИВЫМ чанком из карточки аккаунта: экран нужен одной роли
 * (администратору) и открывается редко — в стартовом бандле рабочего места ему
 * делать нечего (03 §7), там же живут боты, статистика и журнал.
 *
 * Правило совместимости, ради которого в форме есть отдельное предупреждение:
 * СНЯТЬ ВСЕХ — значит открыть канал всем операторам, а не закрыть его для
 * всех. Администратор, снимающий последнюю галочку, должен видеть это до
 * сохранения, а не узнавать по последствиям.
 */

const NO_OPERATOR_REASON = "не может отвечать клиентам";

/** Поиск без оглядки на регистр и на «ё» — по-русски это одна и та же буква. */
function normalize(s: string): string {
  return s.toLowerCase().replace(/ё/g, "е").trim();
}

function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  return a.size === b.size && [...a].every((id) => b.has(id));
}

/**
 * Порядок списка: сначала те, кого можно назначить (по имени), затем те, кого
 * нельзя. Иначе руководитель и наблюдатель, которых назначить всё равно
 * невозможно, встают посреди списка и мешают глазу искать нужного.
 */
function orderCandidates(items: ChannelOperatorCandidate[]): ChannelOperatorCandidate[] {
  return [...items].sort((a, b) => {
    if (a.can_be_operator !== b.can_be_operator) return a.can_be_operator ? -1 : 1;
    return a.full_name.localeCompare(b.full_name, "ru");
  });
}

function CandidateRow({
  candidate,
  checked,
  disabled,
  onToggle,
}: {
  candidate: ChannelOperatorCandidate;
  checked: boolean;
  disabled: boolean;
  onToggle(next: boolean): void;
}) {
  const assignable = candidate.can_be_operator;
  /**
   * Назначить нельзя — а СНЯТЬ можно, и это не послабление, а условие того,
   * что экран вообще работает. Сотрудника отключают (уволили, отпуск), но
   * связь с каналом сервер намеренно не рвёт: `assigned_user_ids` отдаёт
   * «правду о галочках», включая отключённых, чтобы форма не снимала
   * отпускника молча. Такая галочка приходит СТОЯЩЕЙ, и сервер с 03.09 её
   * не пересуживает (`set_operators` проверяет только добавляемых): набор
   * с отпускником сохраняется, а после включения тот снова получает канал.
   *
   * Запрет односторонний: снять можно, назад не вернёшь, пока сотрудника не
   * включат, — добавить негодного сервер не даст. Ровно то, что он разрешает.
   */
  const locked = !assignable && !checked;
  // Отдел появится в 7.4; пока сервер отдаёт null и в подстроке стоит роль.
  const group = candidate.department || ROLE_LABELS[candidate.role];
  const reason = candidate.reason || NO_OPERATOR_REASON;

  return (
    <div className="channel-operator" data-disabled={assignable ? undefined : "true"}>
      <Checkbox
        color="lp"
        checked={checked}
        disabled={disabled || locked}
        onChange={(e) => onToggle(e.currentTarget.checked)}
        label={
          <span className="channel-operator__label">
            <span className="channel-operator__name">
              {/* Отдел в скобках (04.09): на канал назначают по отделу, а не
                  по человеку — «все чатеры на этот канал». Имя без отдела
                  заставляло сверяться со вкладкой «Команда» в соседнем окне. */}
              {подписьСотрудника(candidate)}
              {assignable && (
                <Badge size="xs" variant="light" color="lp" ml="var(--lc-space-2)">
                  оператор
                </Badge>
              )}
            </span>
            <Text component="span" fz="xs" c="var(--lc-text-3)" display="block">
              {/* Причина стоит текстом, а не только тултипом: список читают
                  глазами сверху вниз, и наводить мышь на каждую серую строку,
                  чтобы понять, почему она серая, — не работа. */}
              {assignable ? group : `${group} · ${reason}`}
              {/* Тому, у кого галочка стоит вопреки запрету, говорим не только
                  «нельзя», но и что делать: снять — единственный выход. */}
              {!assignable && checked ? " · снимите галочку, чтобы сохранить аккаунт" : ""}
            </Text>
          </span>
        }
      />
    </div>
  );
}

export function AssignOperatorsModal({
  account,
  onClose,
}: {
  account: AvitoAccountDto;
  onClose(): void;
}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<Set<string> | null>(null);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const q = useQuery({
    queryKey: channelOperatorsKey(account.id),
    queryFn: () => fetchChannelOperators(account.id),
    staleTime: 60_000,
  });

  const baseline = useMemo(() => new Set(q.data?.assigned_ids ?? []), [q.data]);

  // Черновик рождается из ответа сервера ровно один раз: рефетч в фоне не
  // должен затирать галочки, которые администратор уже расставил.
  useEffect(() => {
    if (q.data) setDraft((current) => current ?? new Set(q.data.assigned_ids));
  }, [q.data]);

  const selected = draft ?? baseline;
  const dirty = draft !== null && !sameSet(selected, baseline);

  const candidates = useMemo(() => orderCandidates(q.data?.candidates ?? []), [q.data]);

  /**
   * Кто стоит в наборе, но вести канал сейчас не может (отключён либо роль
   * сменилась). Сохранению это НЕ мешает (проверка 24.09): сервер с 03.09
   * судит только добавляемых, а запрет здесь заставлял снимать отпускника
   * ради новичка — и тот после включения молча оставался без канала. Строка
   * ниже справочная: называет имена, потому что строка может быть спрятана
   * поиском.
   */
  const inactive = useMemo(
    () => candidates.filter((c) => !c.can_be_operator && selected.has(c.id)),
    [candidates, selected],
  );

  /**
   * Знаменатель счётчика — «сколько строк вообще может быть отмечено»: те, кого
   * назначать можно, плюс те, кто уже отмечен вопреки этому. Без второго
   * слагаемого счётчик выдаёт «Выбрано: 3 из 2».
   */
  const selectableCount = candidates.filter(
    (c) => c.can_be_operator || selected.has(c.id),
  ).length;

  const visible = useMemo(() => {
    const needle = normalize(search);
    if (!needle) return candidates;
    // Ищем по всей подписи: в строке видно «Ольга Ковалёва (ОКК)», и первое,
    // что напечатает человек, собирающий канал по отделу, — «ОКК».
    return candidates.filter((c) => normalize(подписьСотрудника(c)).includes(needle));
  }, [candidates, search]);

  const save = useMutation({
    mutationFn: () => saveChannelOperators(account.id, [...selected].sort()),
    onSuccess: () => {
      // Один invalidate по префиксу `["accounts"]` освежает и карточки, и этот экран.
      void qc.invalidateQueries({ queryKey: qk.accounts });
      showToast({
        title: "Операторы назначены",
        message:
          selected.size === 0
            ? `Канал «${account.title}» доступен всем операторам`
            : `На аккаунт «${account.title}» назначено: ${selected.size}`,
        color: "lp",
      });
      onClose();
    },
    onError: (err) => {
      setFormError(
        err instanceof ApiError && err.message
          ? err.message
          : "Не получилось сохранить. Попробуйте ещё раз",
      );
    },
  });

  const toggle = (id: string, next: boolean) => {
    setFormError(null);
    setDraft((current) => {
      const copy = new Set(current ?? baseline);
      if (next) copy.add(id);
      else copy.delete(id);
      return copy;
    });
  };

  /** Крестик, Esc и клик мимо окна ведут сюда же, что и «Отмена» (11 §8: не терять ввод молча). */
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
      centered
      size="lg"
      title={`Назначить операторов на аккаунт «${account.title}»`}
    >
      <Stack gap="var(--lc-space-3)">
        <Text fz="sm" c="var(--lc-text-2)">
          Только сотрудники, назначенные операторами, могут общаться с клиентами этого аккаунта.
        </Text>

        {/* Роль у Alert нигде не задаём: Mantine ставит `role="alert"` сам, ПОВЕРХ
            переданного (Alert.cjs: role идёт после ...others). Передавать её —
            значит делать вид, что мы выбрали семантику, которой не управляем. */}
        {confirmingClose && (
          <Alert color="yellow" variant="light" title="Изменения не сохранены">
            <Stack gap="var(--lc-space-2)">
              <Text fz="sm">Закрыть экран и потерять расставленные галочки?</Text>
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

        {formError && (
          <Alert color="red" variant="light">
            {formError}
          </Alert>
        )}

        {q.isPending ? (
          <Group justify="center" p="var(--lc-space-4)">
            <Loader size="sm" color="lp" />
          </Group>
        ) : q.isError ? (
          <Alert color="red" variant="light">
            Не получилось загрузить список сотрудников
            <Group mt="var(--lc-space-2)">
              <Button size="xs" variant="outline" onClick={() => void q.refetch()}>
                Повторить
              </Button>
            </Group>
          </Alert>
        ) : (
          <>
            <TextInput
              label="Поиск по имени или отделу"
              placeholder="Имя или отдел"
              value={search}
              onChange={(e) => setSearch(e.currentTarget.value)}
              disabled={save.isPending}
            />

            <Text fz="xs" c="var(--lc-text-3)">
              Выбрано: {selected.size} из {selectableCount}
            </Text>

            {inactive.length > 0 && (
              // Над списком и с именами: строка может быть отфильтрована поиском.
              // Жёлтая, а не красная: сохранению это не мешает.
              <Alert color="yellow" variant="light" title="Сейчас не ведут аккаунт">
                {inactive.map((c) => подписьСотрудника(c)).join(", ")} —{" "}
                {inactive.length === 1 ? "отключён или сменил роль и остаётся" : "отключены или сменили роль и остаются"}{" "}
                в наборе: {inactive.length === 1 ? "снова получит" : "снова получат"} обращения
                канала, когда {inactive.length === 1 ? "станет" : "станут"} оператором. Снимите{" "}
                {inactive.length === 1 ? "галочку" : "галочки"}, если убрать нужно совсем.
              </Alert>
            )}

            <div className="channel-operators" role="group" aria-label="Сотрудники">
              {visible.length === 0 ? (
                <Text fz="sm" c="var(--lc-text-3)" p="var(--lc-space-3)">
                  Никого не нашли
                </Text>
              ) : (
                visible.map((c) => (
                  <CandidateRow
                    key={c.id}
                    candidate={c}
                    checked={selected.has(c.id)}
                    disabled={save.isPending}
                    onToggle={(next) => toggle(c.id, next)}
                  />
                ))
              )}
            </div>

            {selected.size === 0 && (
              // Правило совместимости в лицо, до сохранения: пустой набор
              // ОТКРЫВАЕТ канал, а не закрывает. Ошибиться здесь дорого —
              // «закрыл канал» и «открыл канал всем» дают ровно противоположный
              // состав очереди у тринадцати человек.
              <Alert color="yellow" variant="light" title="Никто не выбран">
                Аккаунт будет доступен <b>всем</b> операторам. Так задумано: аккаунт без назначенных не
                закрывается, а открывается для всех, иначе обращения повисли бы.
              </Alert>
            )}
          </>
        )}

        {/* Класс — ради прилипания подвала к низу окна: разбор и числа замера
            в accounts.css у `.assign-ops__foot`. */}
        <Group justify="flex-end" mt="var(--lc-space-2)" className="assign-ops__foot">
          <Button variant="default" onClick={requestClose} disabled={save.isPending}>
            Отмена
          </Button>
          <Button
            loading={save.isPending}
            disabled={!dirty || q.isPending || q.isError}
            onClick={() => {
              setFormError(null);
              save.mutate();
            }}
          >
            Сохранить
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
