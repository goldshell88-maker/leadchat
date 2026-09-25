import { Alert, Badge, Button, Group, Loader, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { usePermissions } from "@/shared/auth/usePermissions";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { подписьКанала } from "@/shared/lib/channelLabel";
import { fetchMyChannels, myChannelsKey } from "./api";
import "./my-channels.css";

/**
 * «Мои каналы» в профиле (план 7.2, п. 5) — только для чтения.
 *
 * Зачем блок нужен. С назначением сотрудников на каналы очередь «Входящие»
 * перестаёт показывать всем всё, и у менеджера появляется вопрос, которого
 * раньше не было: почему коллега видит диалог, а я нет. Без этого списка ответ
 * на него знает только администратор, и каждый такой вопрос идёт к нему.
 * Здесь же видно и состав, и ПРИЧИНУ попадания канала в список: «назначен»
 * либо «открыт всем» — то самое правило совместимости, по которому канал без
 * единого назначенного доступен каждому.
 *
 * Менять состав отсюда нельзя: он живёт в /settings/accounts у администратора.
 */
export function MyChannelsBlock() {
  const { can } = usePermissions();
  const role = useSessionStore((s) => s.user?.role);
  // Блок про очередь, а очередь — про тех, кто отвечает клиентам. Руководителю
  // и наблюдателю диалоги не распределяются, запрос за списком им не нужен.
  const answersClients = can("messages:send");

  /*
   * АДМИНИСТРАТОР ВИДИТ ВСЁ ПО РОЛИ, А НЕ ПО НАЗНАЧЕНИЯМ (PROF-03).
   *
   * Сервер отдаёт ему каждый канал с `access: "open"` — и так и задумано
   * (`sees_all_channels`, app/services/account_operators.py). Но подпись под
   * списком объясняла это ОДНОЙ причиной на всех: «на канал не назначен ни один
   * оператор, поэтому его диалоги видят все». Для администратора это ложь в обе
   * стороны: каналы у него расписаны по людям, а видит он их всё равно —
   * потому что администратор. Прочитав подпись буквально, он пойдёт искать
   * пропавшие назначения, которых никто не терял.
   *
   * Роль здесь — не признак прав (права спрашиваем у `can`), а признак того,
   * КАКОЙ текст правдив. Второго способа отличить эти два случая нет: в ответе
   * сервера они выглядят одинаково.
   */
  const seesEverything = role === "admin";

  const q = useQuery({
    queryKey: myChannelsKey,
    queryFn: fetchMyChannels,
    enabled: answersClients,
    staleTime: 5 * 60_000, // справочник, живёт долго (03 §2.4)
  });

  if (!answersClients) return null;

  const items = q.data?.items ?? [];
  const hasOpen = items.some((c) => c.access === "open");

  return (
    /*
     * Карточка, а не блок с чертой снизу (разбор 05.09). Заголовок отделён от
     * списка линией: раньше «Мои каналы» и сам список стояли в одной стопке с
     * одинаковыми зазорами, и заголовок читался как ещё одна строка списка.
     */
    <section className="lc-card prof-card">
      <div className="prof-card__head">
        <Title order={2} fz="var(--lc-fz-section)" c="var(--lc-text-1)">
          Мои каналы
        </Title>
        <Text component="p" fz="sm" c="var(--lc-text-2)">
          {seesEverything
            ? "Диалоги этих каналов попадают к вам во «Входящие». Вы администратор — вам приходят все каналы, назначения вас не сужают."
            : "Диалоги этих каналов попадают к вам во «Входящие». Состав настраивает администратор."}
        </Text>
      </div>

      {q.isPending ? (
        <Group gap="var(--lc-space-2)">
          <Loader size="xs" color="lp" />
          <Text fz="sm" c="var(--lc-text-3)">
            Загружаем…
          </Text>
        </Group>
      ) : q.isError ? (
        /*
         * ОШИБКА С ВЫХОДОМ, А НЕ ТУПИК (PROF-06). Была одна красная строка без
         * кнопки: единственный способ повторить — перезагрузить всю страницу
         * профиля, потеряв заодно набранное в форме «Написать администратору»,
         * которая стоит прямо над этим блоком. У всех соседей раздела кнопка
         * «Повторить» есть — здесь её просто забыли.
         */
        <Group gap="var(--lc-space-3)" role="alert">
          <Text fz="sm" c="var(--lc-danger-text)">
            Список каналов не загрузился. Нажмите «Повторить»
          </Text>
          <Button variant="outline" size="xs" onClick={() => void q.refetch()}>
            Повторить
          </Button>
        </Group>
      ) : items.length === 0 ? (
        // Пусто — это не «данных нет», а рабочая проблема: новые диалоги такому
        // сотруднику не придут вовсе. Поэтому предупреждением, а не серым
        // «список пуст», и сразу с тем, что делать.
        // Роль Alert'у не передаём: Mantine ставит `role="alert"` сам, поверх.
        <Alert color="yellow" variant="light" title="Каналы не назначены">
          Новые диалоги во «Входящие» к вам не попадут. Напишите администратору формой «Написать
          администратору» на этой вкладке.
        </Alert>
      ) : (
        <>
          <ul className="my-channels" aria-label="Каналы, диалоги которых приходят вам">
            {items.map((c) => (
              <li key={c.id} className="my-channels__item">
                <Text fz="sm" c="var(--lc-text-1)">
                  {подписьКанала(c)}
                </Text>
                {/* Администратору признак не рисуем вовсе: у него все каналы
                    приходят как «открыт всем», и тринадцать одинаковых серых
                    плашек не сообщают ничего, кроме шума. Почему он видит всё —
                    сказано один раз, в подписи наверху. */}
                {seesEverything ? null : c.access === "open" ? (
                  <Badge size="xs" variant="light" color="gray">
                    открыт всем
                  </Badge>
                ) : (
                  <Badge size="xs" variant="light" color="lp">
                    назначен
                  </Badge>
                )}
              </li>
            ))}
          </ul>
          {hasOpen && !seesEverything && (
            <Text fz="xs" c="var(--lc-text-3)">
              «Открыт всем» — на канал не назначен ни один сотрудник, поэтому его диалоги видят все.
            </Text>
          )}
        </>
      )}
    </section>
  );
}
