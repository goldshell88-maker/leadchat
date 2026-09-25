import { useMemo } from "react";
import { Button, Loader, Text, TextInput } from "@mantine/core";
import type { Role } from "@/shared/auth/usePermissions";
import type { AssignableUser } from "@/shared/api/types";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import type { useAssignableUsers } from "../../hooks/useConversationActions";
import "./client-card.css";

const ROLE_LABELS: Record<Role, string> = {
  admin: "администратор",
  head: "руководитель",
  manager: "менеджер",
  observer: "наблюдатель",
};

type UsersQuery = ReturnType<typeof useAssignableUsers>;

/**
 * Выбор сотрудника: поиск + список с точками «в сети».
 *
 * Один на две модалки — «Передать» и «Позвать». Раньше список жил внутри
 * передачи, и приглашение скопировало бы его целиком: шестьдесят строк, из
 * которых половина — состояния загрузки и ошибки. Копия расходится с
 * оригиналом на первой же правке, а расхождение здесь выглядит как «в одном
 * окне человек есть, в другом нет».
 *
 * Компонент ничего не грузит сам: запрос отдаёт вызывающий. Ему виднее,
 * когда список нужен (модалка открыта) и кого исключить.
 *
 * ⚠ `onlineOnly` — ДЛЯ ПЕРЕДАЧИ (решение владельца 28.08). «Позвать» и
 * «Передать» пользуются одним списком, но правила у них разные: позвать
 * коллегу посмотреть можно и в офлайн — он прочтёт уведомление, когда придёт, а
 * диалог всё это время ведёт прежний человек. Передача же снимает диалог со
 * всех: пока получатель не примет её, диалог не виден НИКОМУ.
 */
/**
 * Три состояния, а не два (жалоба владельца 01.09).
 *
 * ⚠ ЧТО БЫЛО. Точка красилась по `is_online`, а он отвечает на вопрос
 * «приложение открыто и отвечает на пинги» — у ОТОШЕДШЕГО он тоже true. Значит
 * авто-«отошёл» исправно ставил статус, а список продолжал показывать
 * работающего и обедающего одинаково зелёными. Замер на бою 01.09: трое `away`,
 * двое `online`, в модалке все пятеро зелёные.
 *
 * ⚠ ПОЧЕМУ ОТОШЕДШЕГО НЕ ПРЯЧЕМ. Он за столом: видит диалоги, отвечает, ему
 * можно передать. Убрать его из списка значило бы соврать в другую сторону —
 * руководитель перестал бы ему передавать вовсе. Ровно этот довод записан у
 * `TeamUserDto.presence`, и второго ответа на один вопрос заводить нельзя.
 *
 * ⚠ ЦВЕТ НЕ ЕДИНСТВЕННЫЙ НОСИТЕЛЬ СМЫСЛА: подпись стоит рядом всегда — и ради
 * дальтоников, и ради скринридера, для которого точка скрыта.
 */
function состояние(u: AssignableUser): { tone: "online" | "away" | "offline"; label: string } {
  if (u.presence === "away") return { tone: "away", label: "отошёл" };
  // `presence` может не прийти от старого сервера — тогда падаем на прежний
  // признак, и список ведёт себя как раньше, а не показывает всех офлайн.
  if (u.presence === "online" || u.is_online) return { tone: "online", label: "" };
  return { tone: "offline", label: "не в сети" };
}

export function PeoplePicker({
  users,
  query,
  onQueryChange,
  selected,
  onSelect,
  disabledIds,
  onlineOnly = false,
  label = "Кому",
  emptyText = "Никого не нашлось",
}: {
  users: UsersQuery;
  query: string;
  onQueryChange: (v: string) => void;
  selected: string | null;
  onSelect: (id: string) => void;
  disabledIds?: ReadonlySet<string>;
  /** Показывать только тех, кто сейчас в сети. См. довод в шапке файла. */
  onlineOnly?: boolean;
  label?: string;
  emptyText?: string;
}) {
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    let items = users.data?.items ?? [];
    if (onlineOnly) items = items.filter((u) => u.is_online);
    // Ищем и по отделу, а не только по имени: с 04.09 в строке видно
    // «Ольга Ковалёва (ОКК)», и первое, что человек напечатает, увидев
    // это, — «ОКК». Список, который ничего не находит по тому, что сам же
    // показывает, читается как поломка поиска.
    return needle
      ? items.filter((u) => подписьСотрудника(u).toLowerCase().includes(needle))
      : items;
  }, [users.data, query, onlineOnly]);

  return (
    <>
      <TextInput
        data-autofocus
        label={label}
        placeholder="поиск по имени или отделу"
        value={query}
        onChange={(e) => onQueryChange(e.currentTarget.value)}
        mb="var(--lc-space-2)"
      />

      <div className="transfer-list" role="radiogroup" aria-label="Сотрудники">
        {users.isPending ? (
          <div className="transfer-list__state">
            <Loader size="xs" color="lp" />
          </div>
        ) : users.isError ? (
          <div className="transfer-list__state">
            <Text fz="sm" c="var(--lc-text-2)">
              Не получилось загрузить сотрудников
            </Text>
            <Button variant="subtle" size="compact-xs" onClick={() => void users.refetch()}>
              Повторить
            </Button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="transfer-list__state">
            <Text fz="sm" c="var(--lc-text-2)">
              {emptyText}
            </Text>
          </div>
        ) : (
          filtered.map((u) => (
            <button
              key={u.id}
              type="button"
              role="radio"
              aria-checked={selected === u.id}
              className="transfer-row"
              data-active={selected === u.id || undefined}
              disabled={disabledIds?.has(u.id)}
              onClick={() => onSelect(u.id)}
            >
              <span
                className="transfer-row__dot"
                data-state={состояние(u).tone}
                aria-hidden="true"
              />
              {/* Отдел в скобках (04.09): половина списка ведёт один и тот же
                  тип обращений из разных отделов, и по имени не выбрать. Роль
                  рядом осталась — она про права, а не про то, кто это. */}
              <span className="transfer-row__name">{подписьСотрудника(u)}</span>
              <span className="transfer-row__role">
                {ROLE_LABELS[u.role]}
                {состояние(u).label ? ` · ${состояние(u).label}` : ""}
              </span>
            </button>
          ))
        )}
      </div>
    </>
  );
}
