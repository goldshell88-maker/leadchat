/**
 * Стили Mantine — по компонентам, а не одним файлом.
 *
 * `@mantine/core/styles.css` весит 231 КБ (32 КБ gzip) и содержит стили всех
 * ~90 компонентов библиотеки; используем мы 43. Библиотека штатно раздаёт
 * `@mantine/core/styles/<Component>.css` ровно для этого случая — набор ниже
 * экономит ~11 КБ gzip и укладывает сборку в бюджет (09 §приёмка).
 *
 * ПОРЯДОК ЗДЕСЬ ЗНАЧИМ — И ЭТО НЕ ОЧЕВИДНО
 * ------------------------------------------
 * Раньше файлы стояли по алфавиту с пометкой «правила компонентов независимы,
 * порядок ни на что не влияет». Это оказалось неправдой, и цена была высокой:
 * `UnstyledButton.css` (основа всех кнопок) содержит
 * `background-color: transparent`, а селекторы Mantine намеренно держат
 * специфичность в один класс. Стоя ПОСЛЕ `Button.css`, он выигрывал по
 * каскаду и обнулял фон — во всём приложении ни одна кнопка Mantine не имела
 * заливки, все они выглядели простым текстом.
 *
 * Поэтому порядок ниже — не алфавитный, а ровно тот, в котором файлы идут
 * внутри `@mantine/core/styles.css`. За этим следит
 * src/test/mantineStyles.test.ts: он сверяет порядок с каноническим и падает,
 * если кто-то снова «наведёт алфавитный порядок».
 *
 * ВАЖНО: добавили в код новый компонент Mantine — добавьте сюда его стили.
 * Тот же тест сверяет список с фактическими импортами из "@mantine/core" по
 * всему src и падает на пропуске. Внутренние зависимости (Select →
 * Combobox/Input/Pill, Modal → ModalBase, Checkbox/Radio/Switch → InlineInput
 * и т.п.) в списке уже есть.
 */

import "@mantine/core/styles/baseline.css";
import "@mantine/core/styles/default-css-variables.css";
import "@mantine/core/styles/global.css";
import "@mantine/core/styles/ScrollArea.css";
import "@mantine/core/styles/UnstyledButton.css";
import "@mantine/core/styles/VisuallyHidden.css";
import "@mantine/core/styles/Paper.css";
import "@mantine/core/styles/Overlay.css";
import "@mantine/core/styles/Popover.css";
import "@mantine/core/styles/Loader.css";
import "@mantine/core/styles/ActionIcon.css";
import "@mantine/core/styles/CloseButton.css";
import "@mantine/core/styles/Group.css";
import "@mantine/core/styles/ModalBase.css";
import "@mantine/core/styles/Input.css";
import "@mantine/core/styles/Flex.css";
import "@mantine/core/styles/Accordion.css";
import "@mantine/core/styles/Alert.css";
import "@mantine/core/styles/Text.css";
import "@mantine/core/styles/Anchor.css";
import "@mantine/core/styles/AppShell.css";
import "@mantine/core/styles/Combobox.css";
import "@mantine/core/styles/InlineInput.css";
import "@mantine/core/styles/CheckboxCard.css";
import "@mantine/core/styles/CheckboxIndicator.css";
import "@mantine/core/styles/Checkbox.css";
import "@mantine/core/styles/Avatar.css";
import "@mantine/core/styles/Badge.css";
import "@mantine/core/styles/Button.css";
import "@mantine/core/styles/Center.css";
// `Code` — моноширинный блок для адреса и токена автозаявок
// (`features/settings/leads/`). Без своих стилей он выглядит
// обычным текстом, и токен не отличить от подписи вокруг.
import "@mantine/core/styles/Code.css";
import "@mantine/core/styles/Divider.css";
import "@mantine/core/styles/Drawer.css";
import "@mantine/core/styles/Menu.css";
import "@mantine/core/styles/Modal.css";
import "@mantine/core/styles/Pill.css";
import "@mantine/core/styles/PillsInput.css";
import "@mantine/core/styles/Notification.css";
import "@mantine/core/styles/NumberInput.css";
import "@mantine/core/styles/PasswordInput.css";
import "@mantine/core/styles/Progress.css";
import "@mantine/core/styles/RadioCard.css";
import "@mantine/core/styles/RadioIndicator.css";
import "@mantine/core/styles/Radio.css";
import "@mantine/core/styles/Tooltip.css";
import "@mantine/core/styles/SegmentedControl.css";
import "@mantine/core/styles/Skeleton.css";
import "@mantine/core/styles/Slider.css";
import "@mantine/core/styles/Stack.css";
import "@mantine/core/styles/Switch.css";
import "@mantine/core/styles/Table.css";
import "@mantine/core/styles/Title.css";
import "@mantine/dates/styles.css";
import "@mantine/notifications/styles.css";
