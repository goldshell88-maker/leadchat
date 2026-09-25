/**
 * Знак Lead Partner — фирменный зелёный квадрат с тёмной монограммой.
 *
 * Держится на --lc-brand, а не на --lc-primary: после редизайна primary —
 * синий, а знак компании цвет не меняет вслед за темой интерфейса. Монограмма
 * тёмная, потому что белый на фирменном зелёном даёт 2.36:1 (16 §1.4).
 */
export function LogoMark({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="8" fill="var(--lc-brand)" />
      <text
        x="16"
        y="21.5"
        fontSize="13"
        fontWeight="700"
        fill="var(--lc-on-brand)"
        textAnchor="middle"
        fontFamily="inherit"
      >
        LP
      </text>
    </svg>
  );
}
