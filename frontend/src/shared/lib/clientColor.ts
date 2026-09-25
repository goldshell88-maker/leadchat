/**
 * Детерминированный цвет аватара (10 §4.6): hash(ключ) % 8 → одна из 8 пар
 * «фон/чернила» (--lc-avatar-N-* в lc-vars.css). Один ключ — всегда один
 * цвет: в списке это узнаваемость, а не украшение.
 *
 * Клиенту ключом служит его идентификатор, сотруднику — имя: у сотрудника имя
 * на экране и есть его опознавательный знак, а идентификатор до аватара не
 * доезжает.
 */
export function avatarColorIndex(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % 8;
}

/** Прежнее имя — у аватара клиента; оставлено, чтобы не трогать список чатов. */
export const clientColorIndex = avatarColorIndex;
