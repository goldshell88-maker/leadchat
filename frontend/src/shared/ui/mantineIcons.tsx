import { IconCheck, IconEye, IconEyeOff } from "./Icon";

/*
 * Отрисовщики для компонентов Mantine, которым значок передаётся ФУНКЦИЕЙ, а
 * не готовым элементом: чекбокс отдаёт признак «часть выбрана», поле пароля —
 * признак «показать/скрыть».
 *
 * ⚠ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ ХВОСТ ТЕМЫ. В `theme.tsx` они соседствовали бы с
 * `theme` и `cssVariablesResolver` — файл, экспортирующий и компоненты, и
 * обычные значения, ломает горячую перезагрузку (правило
 * react-hooks/only-export-components). Оно ворчало предупреждением, а не
 * ошибкой, — тем легче было оставить и тем дольше пришлось бы гадать, почему
 * правка темы перезагружает страницу целиком.
 */
/**
 * Галочка чекбокса нашей фигурой.
 *
 * Mantine отдаёт сюда `indeterminate` и класс — второе состояние («часть
 * выбрана») рисуется чертой, а не галочкой, и путать их нельзя: в решётке
 * «люди и каналы» это разные ответы.
 */
export function LcCheckIcon({ indeterminate, ...rest }: { indeterminate?: boolean; className?: string }) {
  return indeterminate ? (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" aria-hidden="true" {...rest}>
      <path d="M6 12h12" />
    </svg>
  ) : (
    <IconCheck strokeWidth={3} {...rest} />
  );
}

/** Глаз в поле пароля: открытый — «показать», перечёркнутый — «скрыть». */
export function LcVisibilityIcon({ reveal, ...rest }: { reveal?: boolean; className?: string }) {
  return reveal ? <IconEyeOff size={18} {...rest} /> : <IconEye size={18} {...rest} />;
}
