import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Modal, Table, Text } from "@mantine/core";
import { usePermissions } from "@/shared/auth/usePermissions";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { hotkeysFor } from "./catalog";
import { hasMoreBelow } from "./scrollHint";
import "./hotkeys-modal.css";


/**
 * Справка по сочетаниям — окном поверх любого экрана.
 *
 * ЗАЧЕМ ОКНО, А НЕ РАЗДЕЛ НАСТРОЕК. Сочетание вспоминают не тогда, когда
 * сидят в настройках, а посреди работы: диалог открыт, клиент ждёт, и надо
 * вспомнить, чем закрывают. Уйти за этим в профиль — значит потерять место,
 * а вернувшись, вспоминать заново, на чём остановился.
 *
 * Открывается по «?» и кнопкой в шапке. «?» выбран не случайно: это общий
 * знак справки в почте, таблицах и мессенджерах, то есть его попробуют
 * наугад — и он сработает.
 *
 * ПРО ПРОКРУТКУ СПИСКА — в hotkeys-modal.css: короче говоря, на невысоком
 * окне две последние строки уходили за нижний край без единого признака, что
 * список продолжается.
 */
export function HotkeysModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [more, setMore] = useState(false);
  // Каждому — только то, что он может сделать (FUNC-35): наблюдатель читал
  // «Ctrl+R — принять диалог», жал и не получал ничего.
  const { can } = usePermissions();
  // Справка читает ЛИЧНЫЕ сочетания человека, а не умолчания: иначе после
  // переназначения шпаргалка учила бы тому, что у него уже не работает, — а это
  // хуже отсутствия шпаргалки.
  const hotkeys = useSessionStore((s) => s.hotkeys);
  const rows = useMemo(() => hotkeysFor(can, hotkeys), [can, hotkeys]);

  const measure = useCallback(() => setMore(hasMoreBelow(boxRef.current)), []);

  useEffect(() => {
    if (!opened) return;
    /*
     * Мерить приходится ПОСЛЕ отрисовки окна: Mantine монтирует содержимое
     * вместе с открытием, и в момент первого эффекта у списка ещё нет высоты.
     * Кадр отрисовки — самый дешёвый способ дождаться настоящих размеров.
     */
    const frame = window.requestAnimationFrame(measure);
    // Окно браузера меняют и с открытой справкой: перетащили на второй экран,
    // развернули — список стал выше, и «ниже ещё есть» перестало быть правдой.
    window.addEventListener("resize", measure);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", measure);
    };
  }, [opened, measure]);

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Горячие клавиши"
      centered
      size={560}
      classNames={{ content: "hotkeys-modal__content", body: "hotkeys-modal__body" }}
    >
      <Text fz="sm" c="var(--lc-text-2)" mb="var(--lc-space-3)">
        Основные сочетания совпадают с Jivo — переучиваться не нужно. Всё то же самое
        по-прежнему можно сделать мышью.
      </Text>
      <div className="hotkeys-modal__list" data-more={more || undefined}>
        <div className="hotkeys-modal__scroll" ref={boxRef} onScroll={measure}>
          <Table verticalSpacing="xs" horizontalSpacing={0} withRowBorders={false}>
            <Table.Tbody>
              {rows.map(({ id, keys, what }) => (
                <Table.Tr key={id}>
                  <Table.Td className="hotkeys__keys">
                    <kbd className="lc-kbd">{keys}</kbd>
                  </Table.Td>
                  <Table.Td>
                    <Text fz="sm" c="var(--lc-text-2)">
                      {what}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </div>
      </div>
    </Modal>
  );
}
