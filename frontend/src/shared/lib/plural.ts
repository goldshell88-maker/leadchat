/**
 * Русские окончания числительных: 1 минуту, 2 минуты, 5 минут.
 * Один экземпляр на приложение — правило одно, а мест, где счёт показывают
 * человеку, много (время уведомления, повторы, срок повторной попытки).
 */
export function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}
