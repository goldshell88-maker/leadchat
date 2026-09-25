import { useQuery } from "@tanstack/react-query";
import { http } from "@/shared/api/http";

const КЛЮЧ = ["presence", "release"] as const;

/**
 * Через сколько минут в «Отошёл» или вне сети освобождаются мои диалоги.
 * `null` — не освобождаются (правило выключено или я в исключениях),
 * `undefined` — пока не знаем.
 */
export function useReleaseRule(): number | null | undefined {
  const q = useQuery({
    queryKey: КЛЮЧ,
    queryFn: () =>
      http.get<{ release_after_minutes: number | null }>("/presence/release"),
    staleTime: 5 * 60_000,
  });
  return q.data?.release_after_minutes;
}

/** Что «Отошёл» значит для этого человека — одной фразой, без обещаний, которых сервер не держит. */
export function смыслОтошёл(минут: number | null | undefined): string {
  const основа = "«Отошёл» — новые обращения вам не раздаются";
  if (минут === null) return `${основа}, начатые диалоги остаются за вами.`;
  if (минут === undefined) return `${основа}.`;
  return (
    `${основа}. Через ${минут} минут в «Отошёл» или вне сети ваши диалоги ` +
    "освобождаются: ждущие ответа уходят во «Входящие», отвеченные закрываются."
  );
}
