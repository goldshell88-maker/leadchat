/**
 * Точка состояния (docs/16-DESIGN-SYSTEM-2026.md §4).
 *
 * Замена цветным эмодзи 🟢 🔴 ⚪. Разница не косметическая: эмодзи рисует
 * операционная система своим цветом, из CSS он не управляется, и «зелёный»
 * кружок остаётся одинаковым в обеих темах и на всех платформах — то есть
 * перестаёт быть частью системы ровно там, где важнее всего быть ею.
 *
 * Цвет НИКОГДА не единственный носитель смысла: рядом всегда стоит подпись,
 * а для скринридера есть `label`. Дальтонику зелёный и красный кружки
 * различимы примерно никак.
 */
export type DotTone = "online" | "offline" | "away" | "danger" | "muted";

const TONE: Record<DotTone, string> = {
  online: "var(--lc-presence-online)",
  offline: "var(--lc-presence-offline)",
  away: "var(--lc-presence-away)",
  danger: "var(--lc-danger)",
  muted: "var(--lc-text-disabled)",
};

export function StatusDot({
  tone,
  label,
  size = 8,
}: {
  tone: DotTone;
  /** Текст для скринридера. Не задан — точка декоративна, смысл несёт сосед. */
  label?: string;
  size?: number;
}) {
  return (
    <span
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : "true"}
      style={{
        display: "inline-block",
        flex: "none",
        width: size,
        height: size,
        borderRadius: "50%",
        background: TONE[tone],
      }}
    />
  );
}
