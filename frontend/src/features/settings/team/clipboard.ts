/**
 * Копирование в буфер. Clipboard API есть не везде (http без TLS, jsdom,
 * отказ в разрешении), поэтому запасной путь — скрытое поле и execCommand:
 * одноразовую ссылку приглашения админ обязан скопировать с первого раза.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Нет разрешения — падаем в запасной путь ниже.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand?.("copy") ?? false;
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
