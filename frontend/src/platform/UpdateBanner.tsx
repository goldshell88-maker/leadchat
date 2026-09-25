import { Button, Text } from "@mantine/core";
import { pageActions } from "@/shared/lib/pageActions";
import { getBridgeOrNull } from "./bridge";
import { selectBannerUpdate, useUpdateStore } from "./updateStore";
import "./platform.css";

/**
 * Полоса «есть обновление» — одна на обе платформы, но поводы разные.
 *
 * ДЕСКТОП (04 §6.3): мост принёс новую версию приложения. Ставится только по
 * клику, «Позже» прячет полосу на сутки — у оператора мог быть недописанный
 * ответ, а установка перезапускает приложение целиком.
 *
 * ВЕБ (SHELL-02): на сервере лежит другая сборка фронта — см.
 * `platform/buildVersion.ts`. Лечится обычной перезагрузкой вкладки.
 *
 * ПОЧЕМУ У ВЕБ-ПОЛОСЫ НЕТ «ПОЗЖЕ». Отложить нечего: вкладка УЖЕ работает на
 * коде, которого на сервере больше нет. Первый же переход в ленивый раздел
 * (статистика, разбор диалогов, настройки ботов) упрётся в 404 за чанком и
 * вкладка перезагрузится сама, только уже аварийно — с экраном отказа посреди
 * работы. Полоса — это шанс сделать то же самое в удобную секунду, и прятать
 * её на сутки значит отбирать этот шанс.
 *
 * ПОЧЕМУ ВТОРАЯ СТРОКА ПРО ЧЕРНОВИКИ. Без неё оператор не нажмёт: он посреди
 * ответа клиенту, а кнопка предлагает перезагрузить страницу. Утверждение
 * проверяется тестом (`test/draftSurvival.test.tsx`, M3) — это не обещание, а
 * следствие того, что черновики лежат в localStorage (chatUiStore, persist).
 */
export function UpdateBanner() {
  const update = useUpdateStore(selectBannerUpdate);
  const serverBuild = useUpdateStore((s) => s.serverBuild);
  const applying = useUpdateStore((s) => s.applying);
  const error = useUpdateStore((s) => s.error);
  const dismiss = useUpdateStore((s) => s.dismiss);

  if (!update) return serverBuild ? <NewBuildBanner /> : null;

  const apply = () => {
    void getBridgeOrNull()?.desktop?.installUpdate();
  };

  return (
    <div className="lc-update-banner" role="status" aria-label="Доступно обновление">
      <div className="lc-update-banner__text">
        <Text fz="sm" c="var(--lc-text-1)">
          Доступна версия {update.version}
        </Text>
        {update.notes && (
          <Text fz="xs" c="var(--lc-text-3)" lineClamp={2}>
            {update.notes}
          </Text>
        )}
        {error && (
          <Text fz="xs" c="red">
            {error}
          </Text>
        )}
      </div>
      <Button size="xs" loading={applying} onClick={apply}>
        Перезапустить
      </Button>
      <Button size="xs" variant="subtle" color="gray" onClick={dismiss}>
        Позже
      </Button>
    </div>
  );
}

/** Веб: сборка на сервере разошлась с этой вкладкой (SHELL-02). */
function NewBuildBanner() {
  return (
    <div className="lc-update-banner" role="status" aria-label="Вышло обновление">
      <div className="lc-update-banner__text">
        <Text fz="sm" c="var(--lc-text-1)">
          Вышло обновление
        </Text>
        <Text fz="xs" c="var(--lc-text-3)">
          Набранные ответы сохранятся
        </Text>
      </div>
      {/* Через объект: тест подменяет `pageActions.reload` — jsdom не умеет
          настоящую перезагрузку, а проверить кнопку надо. */}
      <Button size="xs" onClick={() => pageActions.reload()}>
        Обновить
      </Button>
    </div>
  );
}
