import { useEffect, useState } from "react";
import { Anchor, Switch, Text, Title } from "@mantine/core";

/**
 * Уведомления браузера — единственный способ узнать о клиенте, не глядя в экран.
 *
 * ЧТО БЫЛО СЛОМАНО. Код системных уведомлений написан давно и корректен
 * (`platform/web.ts`): вкладка скрыта, разрешение выдано — показываем. Но
 * разрешение не запрашивал НИКТО. В комментарии там написано «сами разрешение не
 * спрашиваем — это делает переключатель в /settings/profile», а переключателя не
 * существовало: `Notification.requestPermission` не встречался в проекте ни разу.
 * Значит `permission` навсегда оставался `default`, и ни одно уведомление не
 * показывалось никогда — при полностью рабочем коде показа.
 *
 * Цена ровно такая, как в замере по живому корпусу: ответ за пять минут даёт
 * 19.6% телефонов, через час — 8.1%. Свёрнутая вкладка без уведомлений — это час.
 *
 * ПОЧЕМУ ПЕРЕКЛЮЧАТЕЛЬ, А НЕ ЗАПРОС ПРИ ВХОДЕ. Браузеры требуют жеста человека и
 * запоминают отказ навсегда: выскочивший при загрузке запрос почти всегда
 * закрывают не глядя, и второй раз спросить будет уже нельзя. Здесь человек сам
 * нажимает, понимая зачем.
 *
 * ЧЕСТНЫЕ ГРАНИЦЫ. Выключить разрешение из кода нельзя — только выдать. Поэтому
 * выключатель в положении «включено» объясняет, где это снимается, а не делает
 * вид, что управляет. И это разрешение для ЭТОГО браузера на этом компьютере:
 * телефон и второй компьютер спрашивают отдельно.
 *
 * ПЕРЕКЛЮЧАТЕЛЬ БОЛЬШЕ НЕ ЕДИНСТВЕННЫЙ (07.09). Спрашивать умеет и полоса в
 * рабочем месте (`shared/ui/ПредложитьУведомления.tsx`) — сюда диспетчер за
 * смену не заходит ни разу. Но полоса показывается ТОЛЬКО при `default`: у
 * запрета кнопка «Включить» была бы обманом, потому что второй раз браузер не
 * спросит. Объяснить запрет можно только здесь, и текст ниже — единственное
 * место во всём приложении, где человек узнаёт, что делать дальше.
 *
 * ⚠ ПРО «ЗНАЧОК ЗАМКА» БЫЛО НЕВЕРНО. Chrome убрал замок из адресной строки и
 * поставил на его место значок ползунков; в Yandex Browser замок остался. По
 * журналу nginx весь парк — Chromium (Chrome 4576 сеансов, Yandex Browser 484,
 * Firefox и Safari — ноль), то есть большинство искало значок, которого у них
 * нет. Поэтому в тексте названы оба вида значка и запасной путь через
 * «Настройки сайтов»: инструкция, которая не приводит к цели, хуже молчания —
 * человек решает, что виновато приложение.
 */

type Perm = NotificationPermission | "unsupported";

function currentPermission(): Perm {
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission;
}

const SAMPLE = {
  title: "LeadChat",
  body: "Так выглядит уведомление о новом сообщении",
  tag: "lc-sample",
};

/**
 * Пример — тем же путём, что боевые уведомления (проверка 24.09): сначала
 * сервис-воркер, затем конструктор. Здесь стоял голый `new Notification`, а на
 * Android конструктор запрещён платформой («Illegal constructor») — пример не
 * появлялся, и человек решал, что уведомления не работают. Отказ обоих путей
 * называется словами. `null` — пример показан. Модуль воркера — динамическим
 * импортом: общий код платформенный слой статически не тянет (webBundlePurity).
 */
async function showSample(): Promise<string | null> {
  const { показатьЧерезВоркер } = await import("@/platform/serviceWorker");
  if (await показатьЧерезВоркер(SAMPLE)) return null;
  try {
    new Notification(SAMPLE.title, { body: SAMPLE.body, icon: "/favicon.svg", tag: SAMPLE.tag });
    return null;
  } catch (e) {
    return e instanceof Error && e.message ? e.message : "браузер отказал";
  }
}

export function BrowserAlertsBlock() {
  const [perm, setPerm] = useState<Perm>(currentPermission);
  const [asking, setAsking] = useState(false);
  const [sampleError, setSampleError] = useState<string | null>(null);

  // Разрешение могли выдать или снять в настройках сайта, не перезагружая
  // вкладку. Пересматриваем при возвращении на неё — иначе переключатель
  // показывал бы состояние на момент открытия страницы.
  useEffect(() => {
    const sync = () => setPerm(currentPermission());
    document.addEventListener("visibilitychange", sync);
    return () => document.removeEventListener("visibilitychange", sync);
  }, []);

  const ask = async () => {
    if (typeof Notification === "undefined") return;
    setAsking(true);
    try {
      setPerm(await Notification.requestPermission());
    } catch {
      // Firefox в приватном окне и старые Safari бросают вместо отказа.
      setPerm(currentPermission());
    } finally {
      setAsking(false);
    }
  };

  const granted = perm === "granted";

  return (
    <section className="lc-card prof-card">
      <div className="prof-card__head">
        <Title order={2} fz="var(--lc-fz-section)" c="var(--lc-text-1)">
          Уведомления браузера
        </Title>
        <Text component="p" fz="sm" c="var(--lc-text-3)">
          Как узнать о клиенте, когда вкладка свёрнута
        </Text>
      </div>
      <Text fz="sm" c="var(--lc-text-2)">
        Всплывают, только когда вкладка LeadChat скрыта. Разрешение спрашивается для этого
        браузера на этом компьютере — на телефоне и на втором компьютере нужно разрешить
        отдельно.
      </Text>

      {perm === "unsupported" ? (
        <Text fz="sm" c="var(--lc-text-3)">
          Этот браузер системные уведомления не поддерживает.
        </Text>
      ) : (
        <Switch
          checked={granted}
          disabled={asking || perm === "denied" || granted}
          onChange={() => void ask()}
          label={granted ? "Разрешены" : "Разрешить уведомления"}
          aria-label="Уведомления браузера"
        />
      )}

      {perm === "denied" && (
        <Text fz="sm" c="var(--lc-warning-text)" mt="var(--lc-space-2)">
          Браузер запретил уведомления для этого сайта, и включить их отсюда нельзя. Пока
          запрет держится, о новом клиенте LeadChat не предупредит. Снять запрет можно только
          в его настройках: значок слева от адреса сайта (замок или ползунки) → «Уведомления»
          → «Разрешить», затем обновите страницу. Если такой строки в меню нет — «Настройки
          сайтов» там же.
        </Text>
      )}

      {granted && (
        <Text fz="xs" c="var(--lc-text-3)" mt="var(--lc-space-2)">
          Отключаются там же, где выдавались, — в настройках сайта у значка слева от
          адреса. Из приложения снять разрешение нельзя.{" "}
          <Anchor
            component="button"
            type="button"
            fz="xs"
            onClick={() => {
              setSampleError(null);
              void showSample().then(setSampleError);
            }}
          >
            Показать пример
          </Anchor>
        </Text>
      )}

      {granted && sampleError && (
        <Text fz="xs" c="var(--lc-danger-text)" mt="var(--lc-space-1)" role="alert">
          Браузер не показал пример ({sampleError}). Обновите страницу и попробуйте ещё раз:
          уведомлениям нужен фоновый обработчик, он ставится при загрузке.
        </Text>
      )}
    </section>
  );
}
