import { useMemo, useState } from "react";
import { Button, Loader, Text, TextInput } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { http } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import { usePermissions } from "@/shared/auth/usePermissions";
import { showToast } from "@/shared/ui/toast";
import { describeError } from "@/shared/ui/errorToast";
import { PageHeader } from "@/shared/ui/PageHeader";
import { подписьКанала } from "@/shared/lib/channelLabel";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import "./operators-grid.css";

/**
 * «Люди и каналы» — решётка вместо тридцати пяти походов по карточкам.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 04.09, ДОСЛОВНО: «продумай, как мне быстро можно было
 * подключать или отключать людей на всех аккаунтах — сейчас я вручную по 30 раз
 * захожу и тыкаю, неудобно».
 *
 * ⚠ ПОЧЕМУ ЧЕЛОВЕК — СТРОКА, А КАНАЛ — КОЛОНКА, А НЕ НАОБОРОТ. Замер боя: 35
 * каналов, 35 человек, и в среднем один человек назначен на 29,7 канала из 35.
 * Норма здесь — «почти все на почти всех», и вопрос, который задают чаще
 * прочих, звучит «куда подключён этот человек», а не «кто на этом канале».
 * Прежний экран отвечал только на второй.
 *
 * ⚠ ПРАВКИ КОПЯТСЯ И УХОДЯТ ОДНИМ ЗАПРОСОМ. Пятьдесят щелчков по решётке — это
 * пятьдесят изменений в одной транзакции, а не пятьдесят запросов: половина,
 * упавшая на середине, оставила бы доступы в состоянии, которого никто не
 * задумывал.
 */
interface ГридКанал {
  id: string;
  title: string;
  lead_origin: string | null;
  status: string;
  is_service: boolean;
}

interface ГридЧеловек {
  id: string;
  full_name: string;
  role: string;
  can_be_operator: boolean;
  reason: string | null;
  /** Отдел — подписью «(ОКК)» рядом с именем (04.09). `null` — не заполнен. */
  department?: string | null;
}

interface Грид {
  accounts: ГридКанал[];
  users: ГридЧеловек[];
  assigned: { account_id: string; user_id: string }[];
}

const ключ = (accountId: string, userId: string) => `${accountId}|${userId}`;

/**
 * Короткая подпись колонки — то, чем канал называют вслух.
 *
 * ⚠ ПОЛНОЕ ИМЯ В ШАПКЕ НЕ ПОМЕЩАЕТСЯ НИКАК, И ОБА ПРЕЖНИХ ОТВЕТА БЫЛИ ПЛОХИ.
 * Первый — развернуть подпись боком: ширину держало, но читать приходилось с
 * поворотом головы, а смотрят сюда столько же раз, сколько ставят галочек.
 * Второй — `lead_origin || title`: у каналов с источником выходило коротко и
 * ясно («B43»), а у прочих обрезанное имя целиком — «GLEB…», «Алек…»,
 * «Анат…». Тридцать четыре таких огрызка рядом владелец назвал «слишком всё
 * слитно», и отличить «Алек…» от «Алек…» действительно нельзя.
 *
 * Поэтому у канала без источника берём ПЕРВОЕ СЛОВО имени: им канал и
 * называют («GLEB», «Матвей», «Никита»), и соседей оно различает. Полное имя —
 * в подсказке и в доступном имени кнопки.
 */
function короткоКанал(a: { title: string; lead_origin: string | null }): string {
  const источник = (a.lead_origin ?? "").trim();
  if (источник) return источник;
  const первое = a.title.trim().split(/\s+/)[0] ?? a.title;
  return первое.length > 8 ? `${первое.slice(0, 8)}…` : первое;
}

export function OperatorsGridPage() {
  const { can } = usePermissions();
  const править = can("accounts:manage");
  const qc = useQueryClient();
  const [правки, setПравки] = useState<Map<string, boolean>>(new Map());
  const [поиск, setПоиск] = useState("");
  const [поискКанала, setПоискКанала] = useState("");

  const q = useQuery({
    queryKey: [...qk.accounts, "grid"],
    queryFn: () => http.get<Грид>("/avito-accounts/operators/grid"),
    staleTime: 30_000,
  });

  const всеКаналы = useMemo(
    // Служебная заглушка проверочного набора — не канал Авито, обращения через
    // неё не идут. Колонка под неё была бы обещанием, которого не выполнить.
    () => (q.data?.accounts ?? []).filter((a) => !a.is_service),
    [q.data],
  );
  /*
   * ⚠ ФИЛЬТР КАНАЛОВ — ЭТО НЕ УДОБСТВО, А УСЛОВИЕ ЧИТАЕМОСТИ. Тридцать четыре
   * колонки не помещаются ни на один монитор: чтобы поставить одному человеку
   * один канал, приходилось листать решётку вбок и терять из виду строку.
   * Оставив на экране два-три нужных канала, человек видит всю задачу целиком.
   */
  const каналы = useMemo(() => {
    const запрос = поискКанала.trim().toLowerCase();
    if (!запрос) return всеКаналы;
    return всеКаналы.filter((a) => подписьКанала(a).toLowerCase().includes(запрос));
  }, [всеКаналы, поискКанала]);
  const люди = useMemo(() => {
    const запрос = поиск.trim().toLowerCase();
    const все = q.data?.users ?? [];
    // По подписи целиком: решётку собирают отделами («все чатеры на этот
    // канал»), и «ОКК» в поиске обязано находить то, что видно в строке.
    return запрос ? все.filter((u) => подписьСотрудника(u).toLowerCase().includes(запрос)) : все;
  }, [q.data, поиск]);

  const исходно = useMemo(() => {
    const s = new Set<string>();
    for (const a of q.data?.assigned ?? []) s.add(ключ(a.account_id, a.user_id));
    return s;
  }, [q.data]);

  const стоит = (accountId: string, userId: string): boolean => {
    const k = ключ(accountId, userId);
    return правки.get(k) ?? исходно.has(k);
  };

  const переключить = (accountId: string, userId: string, next: boolean) => {
    setПравки((prev) => {
      const m = new Map(prev);
      const k = ключ(accountId, userId);
      // Вернули как было — правка исчезает, а не копится: иначе «Сохранить»
      // осталось бы живым после того, как человек передумал.
      if (исходно.has(k) === next) m.delete(k);
      else m.set(k, next);
      return m;
    });
  };

  const рядом = (userId: string, next: boolean) => {
    setПравки((prev) => {
      const m = new Map(prev);
      for (const a of каналы) {
        const k = ключ(a.id, userId);
        if (исходно.has(k) === next) m.delete(k);
        else m.set(k, next);
      }
      return m;
    });
  };

  const сохранить = useMutation({
    mutationFn: () =>
      http.post<{ applied: number; accounts_touched: number; opened_to_all: string[] }>(
        "/avito-accounts/operators/bulk",
        {
          changes: [...правки].map(([k, assigned]) => {
            const [account_id, user_id] = k.split("|");
            return { account_id, user_id, assigned };
          }),
        },
      ),
    onSuccess: (итог) => {
      setПравки(new Map());
      void qc.invalidateQueries({ queryKey: qk.accounts });
      showToast({
        title: "Готово",
        message:
          `Изменений: ${итог.applied}, каналов: ${итог.accounts_touched}` +
          // ⚠ ПУСТОЙ НАБОР ОТКРЫВАЕТ КАНАЛ ВСЕМ, А НЕ ЗАКРЫВАЕТ ЕГО. Сняли
          // последнего — обращения канала увидит вся смена. Человек думает,
          // что сузил доступ, и об обратном обязан узнать сразу.
          (итог.opened_to_all.length
            ? `. Без операторов остались, значит открыты всем: ${итог.opened_to_all.join(", ")}`
            : ""),
        color: итог.opened_to_all.length ? "yellow" : "lp",
      });
    },
    onError: (error) =>
      showToast({ ...describeError({ where: "назначении операторов", error }), color: "red" }),
  });

  if (q.isPending) return <Loader size="sm" color="lp" />;
  if (q.isError)
    return (
      <div className="op-grid__state">
        <Text fz="sm" c="var(--lc-text-2)">
          Не получилось загрузить
        </Text>
        <Button variant="subtle" size="compact-xs" onClick={() => void q.refetch()}>
          Повторить
        </Button>
      </div>
    );

  /*
   * ⚠ КОЛОНКА ЦЕЛИКОМ — ТОЖЕ ОДНО ДЕЙСТВИЕ. Строка отвечает «куда подключён
   * человек», колонка — «кто на этом канале»; вторая половина работы была
   * доступна только на поканальном экране, ради ухода с которого решётку и
   * делали.
   */
  const колонкой = (accountId: string, next: boolean) => {
    setПравки((prev) => {
      const m = new Map(prev);
      for (const u of люди) {
        if (!u.can_be_operator && next) continue; // ставить негодному нельзя
        const k = ключ(accountId, u.id);
        if (исходно.has(k) === next) m.delete(k);
        else m.set(k, next);
      }
      return m;
    });
  };

  return (
    <section className="op-grid" aria-label="Люди и каналы">
      {/*
        ⚠ У ЭКРАНА НЕ БЫЛО НАЗВАНИЯ ВООБЩЕ.
        Раздел открывался полем поиска: ни заголовка первого уровня, ни строки
        о том, что здесь делают. Для читалки с экрана страница начиналась с
        безымянного ввода, а глазами раздел отличался от соседнего только
        подсветкой пункта в меню слева. Шапка общая, как у всех разделов
        (PageHeader.tsx), — свой кегль здесь был бы четвёртым размером
        заголовка на продукт.
      */}
      <PageHeader
        title="Люди и каналы"
        description="Кто из сотрудников ведёт какие каналы — вся раскладка одним экраном"
      />
      <header className="op-grid__head">
        <TextInput
          className="op-grid__find"
          size="sm"
          placeholder="Сотрудник или отдел"
          aria-label="Поиск по сотрудникам и отделам"
          value={поиск}
          onChange={(e) => setПоиск(e.currentTarget.value)}
        />
        <TextInput
          className="op-grid__find"
          size="sm"
          placeholder="Канал"
          aria-label="Поиск по каналам"
          value={поискКанала}
          onChange={(e) => setПоискКанала(e.currentTarget.value)}
        />
        {/*
          ⚠ ЕДИНСТВЕННАЯ ЦИФРА ЭКРАНА — И ОНА БЫЛА КЕГЛЯ ПОДПИСИ. «14 × 34»
          отвечает «сколько людей и каналов сейчас передо мной», то есть
          насколько велика правка, уходящая одним «Сохранить». Оно стояло тем
          же размером, что и всё вокруг, и терялось между двумя полями поиска.
          Число крупное, слова под ним мелкие — размеры в operators-grid.css.
        */}
        <p className="op-grid__scale">
          <span className="op-grid__scale-num">
            {люди.length} × {каналы.length}
          </span>
          <span className="op-grid__scale-cap">сотрудников × каналов</span>
        </p>
        {править && (
          <Button
            size="sm"
            color="lp"
            loading={сохранить.isPending}
            disabled={правки.size === 0}
            onClick={() => сохранить.mutate()}
          >
            Сохранить ({правки.size})
          </Button>
        )}
      </header>

      {/*
        ⚠ ПРОКРУТКА ВНУТРИ ТАБЛИЦЫ, А НЕ У СТРАНИЦЫ. Иначе прилипшая шапка
        прилипать не к чему: она держится за ближайшего прокручиваемого предка.
        Тридцать пять строк без неё означают, что к середине списка человек уже
        не знает, над каким каналом стоит галочка.
      */}
      <div className="op-grid__scroll">
        <table className="op-grid__table">
          <thead>
            <tr>
              <th className="op-grid__corner" scope="col">
                Сотрудник
              </th>
              {каналы.map((a, i) => {
                const подпись = подписьКанала(a);
                const всеСтоят =
                  люди.length > 0 && люди.every((u) => стоит(a.id, u.id));
                return (
                  <th
                    key={a.id}
                    scope="col"
                    className="op-grid__col"
                    data-fifth={(i + 1) % 5 === 0 || undefined}
                  >
                    {/*
                      ⚠ ПОДПИСЬ ГОРИЗОНТАЛЬНАЯ И КОРОТКАЯ (жалоба владельца
                      05.09: «чтобы не пришлось читать боком»).
                      
                      ЗДЕСЬ БЫЛА ВЕРТИКАЛЬНАЯ, и довод у неё был верный лишь
                      наполовину. Верно: полное имя «Стас КП · B81» шириной в
                      восемьдесят пикселей растягивает колонку, галочка в ней
                      занимает восемнадцать, колонки разъезжаются — ровно то,
                      что владелец назвал «всё криво». Неверно: из этого не
                      следует, что подпись надо разворачивать. Достаточно её
                      УКОРОТИТЬ.
                      
                      Код источника («B43», «M3 5T», «c79.2») — это то, чем
                      каналы называют между собой; он влезает горизонтально в
                      44 пикселя. Полное имя никуда не делось: оно в подсказке
                      по наведению и в доступном имени кнопки, то есть доступно
                      и мыши, и читалке с экрана.
                      
                      Тридцать четыре подписи боком стоили поворота головы на
                      каждую — при том что смотрят на них не один раз, а
                      столько же, сколько ставят галочек.
                    */}
                    {править ? (
                      <button
                        type="button"
                        className="op-grid__col-btn"
                        title={`${подпись} — ${всеСтоят ? "снять всех" : "назначить всех"}`}
                        aria-label={`${подпись}: ${всеСтоят ? "снять всех" : "назначить всех"}`}
                        onClick={() => колонкой(a.id, !всеСтоят)}
                      >
                        <span className="op-grid__col-text">{короткоКанал(a)}</span>
                      </button>
                    ) : (
                      // Подпись обрезается на 150 пикселях высоты (замер: имя
                      // длиннее двадцати знаков теряет хвост), и у кнопки
                      // полное имя лежит в `title`. Руководителю кнопки не
                      // положено — а прочесть колонку целиком нужно так же.
                      <span className="op-grid__col-text" title={подпись}>
                        {короткоКанал(a)}
                      </span>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {люди.map((u) => {
              const сколько = всеКаналы.filter((a) => стоит(a.id, u.id)).length;
              const виднеются = каналы.filter((a) => стоит(a.id, u.id)).length;
              return (
                <tr key={u.id} data-off={!u.can_be_operator || undefined}>
                  <th scope="row" className="op-grid__name">
                    {/* Отдел — В ХВОСТЕ подписи, и это не украшение: ячейка
                        обрезает содержимое многоточием, и при узкой колонке
                        первым уходит отдел, а имя остаётся целым. Полная
                        подпись — в подсказке. */}
                    <span className="op-grid__person" title={подписьСотрудника(u)}>
                      {подписьСотрудника(u)}
                    </span>
                    <span className="op-grid__sub">
                      {u.can_be_operator ? `${сколько} из ${всеКаналы.length}` : u.reason}
                    </span>
                    {/*
                      ⚠ «ВЕЗДЕ/НИГДЕ» ПЕРЕЕХАЛИ К ИМЕНИ (жалоба владельца 04.09:
                      «всё криво, неудобно, мелко и некрасиво»). Кнопка стояла
                      ПОСЛЕ тридцати четырёх колонок — то есть за краем экрана:
                      чтобы нажать её для человека, которого видишь, надо было
                      сначала уехать вбок и потерять его строку из виду.
                    */}
                    {править && (
                      <button
                        type="button"
                        className="op-grid__row-btn"
                        disabled={!u.can_be_operator && сколько === 0}
                        title={
                          виднеются < каналы.length
                            ? `Назначить ${подписьСотрудника(u)} на все показанные каналы`
                            : `Снять ${подписьСотрудника(u)} со всех показанных каналов`
                        }
                        // Слово «везде» само по себе не говорит читалке с
                        // экрана ни на кого, ни куда: тридцать пять одинаковых
                        // «везде» подряд неразличимы.
                        aria-label={
                          виднеются < каналы.length
                            ? `Назначить ${подписьСотрудника(u)} на все показанные каналы`
                            : `Снять ${подписьСотрудника(u)} со всех показанных каналов`
                        }
                        onClick={() => рядом(u.id, виднеются < каналы.length)}
                      >
                        {виднеются < каналы.length ? "везде" : "нигде"}
                      </button>
                    )}
                  </th>
                  {каналы.map((a, i) => {
                    const стоитЗдесь = стоит(a.id, u.id);
                    const нельзя = !править || (!u.can_be_operator && !стоитЗдесь);
                    return (
                      <td
                        key={a.id}
                        className="op-grid__cell"
                        data-fifth={(i + 1) % 5 === 0 || undefined}
                        data-changed={правки.has(ключ(a.id, u.id)) || undefined}
                      >
                        {/*
                          ⚠ РОДНАЯ ГАЛОЧКА, А НЕ КОМПОНЕНТ БИБЛИОТЕКИ. Ячеек
                          здесь тысяча двести, и каждая была отдельным
                          компонентом Mantine со своим состоянием и стилями —
                          отсюда и заметная задержка на каждое нажатие. Цвет
                          берётся из той же палитры (`accent-color`), а ЦЕЛЬ
                          НАЖАТИЯ — вся ячейка целиком, а не восемнадцать
                          пикселей посередине.
                        */}
                        <label className="op-grid__hit">
                          <input
                            type="checkbox"
                            checked={стоитЗдесь}
                            disabled={нельзя}
                            aria-label={`${подписьСотрудника(u)} на канале ${подписьКанала(a)}`}
                            onChange={(e) => переключить(a.id, u.id, e.currentTarget.checked)}
                          />
                        </label>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
