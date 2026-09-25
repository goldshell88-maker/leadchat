import { SegmentedControl, Switch, Text, Title, useMantineColorScheme } from "@mantine/core";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { ПРЕСЕТЫ, usePresetStore, type ПресетКлюч } from "@/shared/theme/preset";

/**
 * Оформление — выбор темы (docs/16-DESIGN-SYSTEM-2026.md §2).
 *
 * Тёмная стоит по умолчанию (бриф: «Dark Theme First»), но именно по
 * умолчанию, а не единственной. Смена у менеджеров начинается утром в
 * освещённом помещении, а из Jivo команда приходит со светлого интерфейса;
 * отнимать светлую тему заодно с редизайном значило бы поменять две вещи
 * там, где просили одну.
 *
 * Выбор хранит сам Mantine (localStorage), поэтому он переживает
 * перезагрузку и на следующем входе умолчание уже не применяется. Отдельного
 * поля в профиле на сервере для этого не заводим: настройка машинная, а не
 * сотрудника — за рабочим компьютером в офисе и дома человек вполне может
 * хотеть разного.
 *
 * ЗВУК ЖИВЁТ ЗДЕСЬ ЖЕ, а не отдельным блоком «Внешний вид». Заголовков было
 * два, и они означали одно: раскол блока начали и не довели. Человек видел
 * «Оформление» с темой и через экран «Внешний вид» с одним переключателем —
 * и не понимал, чем они отличаются. Имя оставлено «Оформление»: строка
 * «Настройки → Профиль → Оформление» уже отгружена в журнале релизов, и
 * переименование сделало бы старую запись истории неверной.
 */
export function AppearanceBlock() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const preset = usePresetStore((s) => s.preset);
  const setPreset = usePresetStore((s) => s.setPreset);
  const soundEnabled = useChatUiStore((s) => s.soundEnabled);
  const setSoundEnabled = useChatUiStore((s) => s.setSoundEnabled);

  return (
    <section className="lc-card prof-card">
      <div className="prof-card__head">
        <Title order={2} fz="var(--lc-fz-section)" c="var(--lc-text-1)">
          Оформление
        </Title>
        {/* Одна строка «зачем это» под заголовком — как у остальных карточек
            профиля. Раньше эта же фраза стояла вровень с контролами и читалась
            как ещё один пункт настройки. */}
        <Text component="p" fz="sm" c="var(--lc-text-3)">
          Как приложение выглядит и звучит на этом компьютере
        </Text>
      </div>
      <Text fz="sm" c="var(--lc-text-2)">
        Тема запоминается для этого компьютера.
      </Text>
      <SegmentedControl
        value={colorScheme}
        onChange={(v) => setColorScheme(v as "light" | "dark" | "auto")}
        data={[
          { value: "dark", label: "Тёмная" },
          { value: "light", label: "Светлая" },
          { value: "auto", label: "Как в системе" },
        ]}
        aria-label="Тема оформления"
      />
      {/*
        ⚠ ЦВЕТ — ОТДЕЛЬНАЯ СТРОКА, А НЕ ЧЕТЫРЕ НОВЫХ ЗНАЧЕНИЯ У ТЕМЫ. «Светлая»
        и «Индиго» отвечают на разные вопросы: первая — про освещение в
        комнате, вторая — про цвет. Сложи их в один ряд, и получится восемь
        кнопок, из которых человек каждый раз выбирает дважды.
      */}
      <Text fz="sm" c="var(--lc-text-2)" mt="var(--lc-space-3)">
        Цвет — тоже для этого компьютера. У каждого свой светлый и тёмный вид.
      </Text>
      <SegmentedControl
        value={preset}
        onChange={(v) => setPreset(v as ПресетКлюч)}
        data={ПРЕСЕТЫ.map((п) => ({ value: п.key, label: п.label }))}
        aria-label="Цвет оформления"
      />
      <Switch
        checked={soundEnabled}
        onChange={(e) => setSoundEnabled(e.currentTarget.checked)}
        label="Звук новых сообщений"
        mt="var(--lc-space-3)"
      />
    </section>
  );
}
