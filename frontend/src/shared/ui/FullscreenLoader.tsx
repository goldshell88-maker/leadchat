import { Loader, Stack } from "@mantine/core";
import { LogoMark } from "./LogoMark";

/** Shown while the silent start-up refresh runs (11 §1.3 «Загрузка (bootstrap)»). */
export function FullscreenLoader() {
  return (
    <Stack
      align="center"
      justify="center"
      gap="var(--lc-space-4)"
      style={{ minHeight: "100dvh", background: "var(--lc-bg-1)" }}
    >
      <LogoMark size={48} />
      <Loader color="lp" size="sm" />
    </Stack>
  );
}
