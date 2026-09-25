import { avatarColorIndex } from "@/shared/lib/clientColor";
import { initials } from "@/shared/lib/initials";

/**
 * Аватар сотрудника: инициалы на паре «фон/чернила» из палитры аватаров.
 *
 * БЫЛО: `<Avatar color="lp" variant="light">` — светло-голубые инициалы на
 * синем, контраст 2.08:1. Это ниже любого порога: инициалы читались хуже
 * фона, а на экране «Команда» их тринадцать подряд, и различить, кто есть
 * кто, было нельзя.
 *
 * СТАЛО: та же палитра, что у аватара клиента, — восемь пар, у каждой
 * контраст подобран заранее. Цвет выводится из имени, поэтому у человека он
 * всегда один и тот же: в списке сотрудников это узнаваемость, а не украшение.
 */
export function UserAvatar({ name, size = 32 }: { name: string; size?: number }) {
  const idx = avatarColorIndex(name);
  return (
    <span
      className="lc-avatar"
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.35),
        background: `var(--lc-avatar-${idx}-bg)`,
        color: `var(--lc-avatar-${idx}-ink)`,
      }}
    >
      {initials(name)}
    </span>
  );
}
