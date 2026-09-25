import type { AnchorHTMLAttributes, ReactNode } from "react";
import { getBridgeOrNull } from "@/platform/bridge";

/**
 * Разбирается ли адрес как веб-ссылка. Адреса приходят и из чужих ответов
 * (Авито): не-веб схема в настольном приложении ушла бы в системный
 * обработчик, а он открывает не браузер, а что угодно.
 */
function isWebUrl(url: string): boolean {
  try {
    const scheme = new URL(url, window.location.origin).protocol;
    return scheme === "https:" || scheme === "http:";
  } catch {
    return false;
  }
}

/**
 * Внешняя ссылка через мост (03 §7): в настольном приложении `target=_blank`
 * открылся бы внутри WebView и подменил рабочее окно. `href` остаётся — он
 * даёт «копировать адрес» и правильную семантику для экранного чтеца.
 * Негодный адрес не рисуется вовсе: мёртвая ссылка хуже её отсутствия.
 */
/** Прочие атрибуты ссылки (класс, подпись, `download`, `data-*`) — как у `<a>`. */
type AnchorRest = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "target" | "rel" | "onClick">;

export function ExternalLink({
  url,
  children,
  ...rest
}: AnchorRest & {
  url: string;
  children: ReactNode;
}) {
  if (!isWebUrl(url)) return null;
  return (
    <a
      {...rest}
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => {
        const bridge = getBridgeOrNull();
        // В браузере обычный target=_blank ведёт себя правильно.
        if (bridge?.kind !== "tauri") return;
        e.preventDefault();
        void bridge.openExternal(url);
      }}
    >
      {children}
    </a>
  );
}
