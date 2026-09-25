import { SegmentedControl } from "@mantine/core";
import { StatusDot } from "@/shared/ui/StatusDot";
import { смыслОтошёл, useReleaseRule } from "./releaseRule";
import { usePresenceStore, useSetPresence, type PresenceStatus } from "./usePresence";
import "./presence-self.css";

/**
 * «На месте» / «Отошёл» в шапке профиля.
 *
 * Два равноправных положения, а не тумблер: «Отошёл» — отдельное состояние, и
 * оба названы словами. Подпись говорит, что именно изменится для этого
 * человека: без неё слово «отошёл» читается как «диалоги отберут» — а отберут
 * ли, зависит от правила освобождения и списка исключений (`useReleaseRule`).
 */
export function PresenceBlock() {
  const status = usePresenceStore((s) => s.status);
  const setPresence = useSetPresence();
  const освобождение = useReleaseRule();

  return (
    <div className="presence-self">
      <span className="presence-self__label">Моё состояние</span>
      <SegmentedControl
        value={status}
        onChange={(v) => setPresence.mutate(v as PresenceStatus)}
        disabled={setPresence.isPending}
        aria-label="Моё состояние"
        data={[
          {
            value: "online",
            label: (
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: "var(--lc-space-2)" }}
              >
                <StatusDot tone="online" />
                На месте
              </span>
            ),
          },
          {
            value: "away",
            label: (
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: "var(--lc-space-2)" }}
              >
                <StatusDot tone="away" />
                Отошёл
              </span>
            ),
          },
        ]}
      />
      <p className="presence-self__note">
        {смыслОтошёл(освобождение)} Коллеги видят вас в списках с пометкой.
      </p>
      <p className="presence-self__fine">
        Состояние держится, пока приложение открыто. Закрыли — вы просто не в
        сети, и при следующем входе снова «на месте».
      </p>
    </div>
  );
}
