import { useState } from "react";
import { withApiRoot } from "@/shared/api/http";
import { clientColorIndex } from "@/shared/lib/clientColor";
import { initials } from "@/shared/lib/initials";

/**
 * Аватар клиента (10 §4.6): фото из Авито, а без него — инициалы на
 * детерминированном пастельном фоне (hash(client_id) % 8 → --lc-avatar-N-*).
 *
 * ⚠ ФОТО ПОЯВИЛОСЬ 15 АВГУСТА (просьба владельца со снимками Jivo: «чтобы
 * отображалось вот так, а не просто Клиент»). Авито отдаёт его в карточке
 * чата, мы храним ссылку на их CDN. Три состояния, и все три обязаны быть
 * покрыты, потому что ссылка ЧУЖАЯ:
 *  - ссылки нет — инициалы, как всегда было;
 *  - ссылка есть — картинка;
 *  - ссылка протухла (клиент сменил фото, CDN ответил ошибкой) — onError
 *    возвращает инициалы. Битая картинка в кружке читалась бы как поломка
 *    системы, хотя сломалась чужая ссылка.
 *
 * Откат живёт в состоянии, а не в CSS: img без src или с ошибкой рисует
 * рамку-плейсхолдер браузера, и спрятать её стилями надёжно нельзя.
 */
export function ClientAvatar({
  clientId,
  name,
  size = 40,
  src,
}: {
  clientId: string;
  name: string | null;
  size?: number;
  src?: string | null;
}) {
  const [broken, setBroken] = useState(false);
  const idx = clientColorIndex(clientId);

  if (src && !broken) {
    return (
      <img
        className="client-avatar client-avatar--photo"
        src={withApiRoot(src)}
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
        style={{ width: size, height: size }}
        /* ⚠ НЕ `lazy`, А `low`. Аватары стоят в видимой части списка с первого
           кадра: `loading="lazy"` откладывал загрузку ровно того, что человек
           уже видит, и строка секунду стояла с буквенной заглушкой. Приоритет
           снимает обратную беду — двадцать шесть картинок разом не отбирают
           канал у самих диалогов. */
        fetchPriority="low"
        /* Декодирование мимо главного потока: иначе каждая пришедшая картинка
           даёт микрозадержку прокрутки. */
        decoding="async"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
      />
    );
  }

  return (
    <span
      className="client-avatar"
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
