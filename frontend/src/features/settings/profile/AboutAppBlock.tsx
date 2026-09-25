import { useEffect, useState } from "react";
import { Button, Group, Switch, Text, Title } from "@mantine/core";
import { getBridgeOrNull, type AppInfo } from "@/platform/bridge";
import { useUpdateStore } from "@/platform/updateStore";

/**
 * «О приложении» в /settings/profile (04 §6.3): версия, канал stable, ручная
 * проверка обновлений и автозапуск. В браузере блока нет вовсе — десктопного
 * API у веб-моста не существует.
 */
export function AboutAppBlock() {
  const desktop = getBridgeOrNull()?.desktop;
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [autostart, setAutostart] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [upToDate, setUpToDate] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);

  const checking = useUpdateStore((s) => s.checking);
  const applying = useUpdateStore((s) => s.applying);
  const available = useUpdateStore((s) => s.available);

  useEffect(() => {
    if (!desktop) return;
    let alive = true;
    void desktop.appInfo().then((i) => alive && setInfo(i));
    void desktop.autostart.isEnabled().then((v) => alive && setAutostart(v));
    return () => {
      alive = false;
    };
  }, [desktop]);

  if (!desktop) return null;

  const check = async () => {
    setUpToDate(false);
    setCheckError(null);
    try {
      const found = await desktop.checkForUpdates();
      setUpToDate(!found);
    } catch (e) {
      // Сбой проверки — не «последняя версия» (проверка 24.09): причину называем.
      setCheckError(e instanceof Error && e.message ? e.message : "проверка не состоялась");
    }
  };

  const toggleAutostart = async (enabled: boolean) => {
    setBusy(true);
    setAutostart(enabled); // оптимистично: переключатель не должен «залипать»
    try {
      await desktop.autostart.set(enabled);
    } catch {
      setAutostart(!enabled);
    } finally {
      setBusy(false);
    }
  };

  return (
    /*
     * Справка, а не настройка (`--ref`): версию смотрят раз в полгода, когда
     * что-то сломалось. В общей стопке блок занимал столько же места и весил
     * столько же, сколько «Оформление», которое трогают каждую смену.
     */
    <section className="lc-card prof-card prof-card--ref">
      <div className="prof-card__head">
        <Title order={2} fz="var(--lc-fz-section)" c="var(--lc-text-1)">
          О приложении
        </Title>
        <Text component="p" fz="sm" c="var(--lc-text-3)">
          LeadChat для Windows · версия <b>{info?.version ?? "…"}</b> · канал обновлений{" "}
          <b>{info?.channel ?? "stable"}</b>
        </Text>
      </div>

      <Group gap="var(--lc-space-3)">
        <Button size="xs" variant="outline" loading={checking} onClick={() => void check()}>
          Проверить обновления
        </Button>
        {available && (
          <Button size="xs" loading={applying} onClick={() => void desktop.installUpdate()}>
            Обновить до {available.version}
          </Button>
        )}
        {!available && upToDate && !checking && (
          <Text fz="sm" c="var(--lc-text-3)">
            У вас последняя версия
          </Text>
        )}
        {checkError && !checking && (
          <Text fz="sm" c="var(--lc-danger-text)" role="alert">
            Не удалось проверить обновления: {checkError}
          </Text>
        )}
      </Group>

      <Switch
        checked={autostart === true}
        disabled={autostart === null || busy}
        onChange={(e) => void toggleAutostart(e.currentTarget.checked)}
        label="Запускать при входе в Windows"
        color="lp"
      />
    </section>
  );
}
