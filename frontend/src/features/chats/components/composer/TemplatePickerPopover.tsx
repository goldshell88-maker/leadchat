import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Loader, Text } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import type { TemplateDto } from "@/shared/api/types";
import { useTemplates } from "@/features/templates/useTemplates";
import { подготовить } from "@/features/templates/списокПикера";
import "./template-picker.css";
import { IconSettings } from "@/shared/ui/Icon";

/**
 * Пикер быстрых ответов (11 §3.1). Открытие: `⚡` или `/` в ПУСТОМ поле
 * (10 §5.1) — фокус сразу в поиске; набор фильтрует список на лету;
 * ↑↓ — выбрать, Enter — вставить, Esc — закрыть. Подстановку переменных
 * делает вызывающая сторона (Composer) — попап отдаёт «сырой» body.
 */

export function TemplatePickerPopover({
  initialQuery = "",
  запрос,
  метка,
  onМетка,
  onPick,
  onClose,
  triggerRef,
}: {
  /** Текст, набранный после «/» до открытия попапа. */
  initialQuery?: string;
  /**
   * Запрос СНАРУЖИ — режим «/команды» в поле ввода (как в Jivo).
   *
   * ⚠ ЗАЧЕМ ДВА РЕЖИМА, А НЕ ДВА КОМПОНЕНТА. Список один и тот же, и правила
   * его порядка — тоже. Разведи их по разным компонентам — они разойдутся:
   * ходовые поднимутся в одном и не поднимутся в другом, а человек будет
   * видеть разный порядок в зависимости от того, чем открыл.
   *
   * Задан — строку ввода рисовать не надо: она живёт в самом сообщении, и
   * фокус остаётся там же. Не задан (открыли кнопкой «⚡») — попап
   * по-прежнему держит свой поиск и свою метку.
   */
  запрос?: string;
  /** Метка снаружи. Осмысленна только вместе с `запрос`. */
  метка?: number;
  onМетка?: (i: number) => void;
  onPick: (template: TemplateDto) => void;
  onClose: () => void;
  /**
   * Кнопка, которой попап открывают.
   *
   * ⚠ БЕЗ НЕЁ ПОВТОРНОЕ НАЖАТИЕ НЕ ЗАКРЫВАЛО, А МИГАЛО. Порядок событий такой:
   * `mousedown` мимо попапа закрывает его (`onClose` → состояние `false`), а
   * следом приходит `click` по той же кнопке и переключает состояние обратно в
   * `true`. Пикер моргал и оставался открытым — закрыть его можно было только
   * щелчком в стороне или Esc.
   *
   * Кнопка-открывашка «мимо» не считается: её нажатие обрабатывает она сама.
   */
  triggerRef?: React.RefObject<HTMLElement | null>;
}) {
  const navigate = useNavigate();
  const снаружи = запрос !== undefined;
  const [свойЗапрос, setСвойЗапрос] = useState(initialQuery);
  const [свояМетка, setСвояМетка] = useState(0);
  const query = снаружи ? запрос : свойЗапрос;
  const cursor = снаружи ? (метка ?? 0) : свояМетка;
  const setCursor = (v: number | ((c: number) => number)) => {
    if (снаружи) onМетка?.(typeof v === "function" ? v(cursor) : v);
    else setСвояМетка(v);
  };
  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const q = useTemplates("all");

  const { groups, ordered } = useMemo(
    () => подготовить(q.data?.items ?? [], query),
    [q.data, query],
  );

  useEffect(() => {
    // В режиме «/команды» строки ввода тут нет: фокус остаётся в сообщении,
    // иначе человек начал бы набирать запрос второй раз — ровно то, за что
    // прежний пикер и назвали неудобным.
    if (!снаружи) inputRef.current?.focus();
  }, [снаружи]);

  /*
   * ⚠ ЩЕЛЧОК МИМО ЗАКРЫВАЕТ ПИКЕР (находка 23.08 №13).
   *
   * Закрыть его можно было только Escape или выбором строки. Щелчок по полю
   * ввода, по ленте, по соседнему диалогу — не закрывал ничего: панель
   * оставалась висеть поверх нижней части переписки (до 340 пикселей высоты) и
   * закрывала то, ради чего человек туда и щёлкнул. Это нарушение общего
   * поведения всплывающих слоёв продукта: у меню, дропдаунов и модалок Mantine
   * закрытие по щелчку мимо есть по умолчанию.
   *
   * Слушаем `mousedown`, а не `click`: закрыться нужно ДО того, как щелчок
   * дойдёт до цели, иначе первый щелчок тратится на закрытие.
   */
  useEffect(() => {
    const мимо = (e: MouseEvent) => {
      const корень = rootRef.current;
      const цель = e.target as Node;
      if (triggerRef?.current?.contains(цель)) return; // см. разбор у `triggerRef`
      if (корень && !корень.contains(цель)) onClose();
    };
    document.addEventListener("mousedown", мимо);
    return () => document.removeEventListener("mousedown", мимо);
  }, [onClose, triggerRef]);

  useEffect(() => {
    // Снаружи меткой распоряжается тот, кто держит запрос: сбрось мы её здесь,
    // сброс приходил бы вторым и гасил движение стрелками.
    if (!снаружи) setСвояМетка(0);
  }, [query, снаружи]);

  // Активная строка всегда видна при навигации стрелками.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>('[data-active="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  /*
   * ⚠ ОДИН АДРЕС ДЛЯ ВСЕХ (разбор 03.09). Здесь стояла развилка: у кого нет
   * права на общие — тому «/settings/profile». Но профиль открывается на
   * вкладке «Учётная запись», и кнопка «Управлять шаблонами» высаживала
   * диспетчера не туда, куда обещала. Раздел теперь открыт по `templates:own`
   * и сам показывает ровно то, чем человек распоряжается.
   */
  const manageHref = "/settings/templates";

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => (ordered.length === 0 ? 0 : (c + 1) % ordered.length));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => (ordered.length === 0 ? 0 : (c - 1 + ordered.length) % ordered.length));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const picked = ordered[cursor];
      if (picked) onPick(picked);
    }
  };

  return (
    <div
      className="tpl-popover"
      role="dialog"
      aria-label="Быстрые ответы"
      onKeyDown={handleKeyDown}
      ref={rootRef}
    >
      <div className="tpl-popover__head">
        <Text fz="sm" fw={600} c="var(--lc-text-2)">
          Быстрые ответы
        </Text>
        {снаружи ? (
          /*
           * ⚠ В РЕЖИМЕ «/КОМАНДЫ» СВОЕЙ СТРОКИ ВВОДА НЕТ. Запрос человек уже
           * набирает в сообщении, и он там виден: «/цена». Вторая строка ввода
           * поверх первой — это ровно та неудобность, ради которой всё и
           * переделано: набрать одно и то же дважды.
           */
          <Text fz="xs" c="var(--lc-text-3)">
            {query ? `/${query}` : "наберите название"} · <kbd>↑↓</kbd> · <kbd>Enter</kbd> —
            вставить
          </Text>
        ) : (
          <input
            ref={inputRef}
            className="tpl-popover__search"
            type="text"
            value={query}
            placeholder="найти шаблон…"
            aria-label="Поиск по быстрым ответам"
            onChange={(e) => setСвойЗапрос(e.currentTarget.value)}
          />
        )}
      </div>

      <div className="tpl-popover__list" role="listbox" aria-label="Список быстрых ответов" ref={listRef}>
        {q.isPending ? (
          <div className="tpl-popover__state">
            <Loader size="xs" color="lp" />
          </div>
        ) : q.isError ? (
          <div className="tpl-popover__state">
            <Text fz="sm" c="var(--lc-text-2)">
              Не получилось загрузить
            </Text>
            <Button variant="subtle" size="compact-xs" onClick={() => void q.refetch()}>
              Повторить
            </Button>
          </div>
        ) : ordered.length === 0 ? (
          <div className="tpl-popover__state">
            <Text fz="sm" c="var(--lc-text-2)">
              {query ? "Ничего не нашлось" : "Шаблонов пока нет"}
            </Text>
            <Button variant="subtle" size="compact-xs" onClick={() => navigate(manageHref)}>
              {query ? "Управлять шаблонами" : "Создать первый"}
            </Button>
          </div>
        ) : (
          groups.map((g) => (
            <div key={g.key} className="tpl-group">
              <div className="tpl-group__label">{g.label}</div>
              {g.items.map((t) => {
                const index = ordered.indexOf(t);
                const active = index === cursor;
                return (
                  <button
                    key={t.id}
                    type="button"
                    role="option"
                    aria-selected={active}
                    data-active={active || undefined}
                    className="tpl-row"
                    onMouseEnter={() => setCursor(index)}
                    /*
                     * ⚠ ФОКУС ТОЖЕ ДВИГАЕТ МЕТКУ (разбор дизайна 28.08).
                     * Enter обрабатывается на КОРНЕ попапа и вставляет строку
                     * под меткой, а метку двигали только стрелки и мышь. Строки
                     * — настоящие кнопки, ловушки фокуса в попапе нет: Tab из
                     * поля поиска доводил фокус до второй строки, а Enter
                     * вставлял первую — и `preventDefault` отменял «нажатие»
                     * той, на которой человек стоял. Оператор видел кольцо
                     * фокуса на одном шаблоне, а клиенту уходил другой.
                     */
                    onFocus={() => setCursor(index)}
                    onClick={() => onPick(t)}
                  >
                    {/*
                     * `title` — страховка на будущее, а не украшение.
                     * Колонка жёсткая (190 px под замеренный максимум), и
                     * заготовку с более длинным названием заведут рано или
                     * поздно. Тогда многоточие покажет, что текст обрезан, а
                     * наведение даст его целиком — вместо выбора вслепую.
                     */}
                    <span className="tpl-row__title" title={t.title}>
                      {t.title}
                    </span>
                    <span className="tpl-row__body">{t.body}</span>
                  </button>
                );
              })}
            </div>
          ))
        )}
      </div>

      <div className="tpl-popover__foot">
        <span>
          <kbd>↑↓</kbd> — выбрать · <kbd>Enter</kbd> — вставить · <kbd>Esc</kbd> — закрыть
        </span>
        <Button variant="subtle" size="compact-xs" onClick={() => navigate(manageHref)}>
          <IconSettings size={14} /> Управлять шаблонами
        </Button>
      </div>
    </div>
  );
}
