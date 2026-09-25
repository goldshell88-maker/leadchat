import { useEffect } from "react";
import { PageHeader } from "@/shared/ui/PageHeader";
import { RELEASES, type ChangeKind } from "./changelog";
import { markUpdatesSeen } from "./seen";
import "./updates.css";

/**
 * «Что нового» (/updates).
 *
 * Экран нужен не ради полноты, а ради перехода с Jivo: команда переезжает на
 * незнакомый интерфейс, который к тому же меняется каждый день. Без такого
 * списка человек узнаёт об изменении в тот момент, когда привычное действие
 * сработало иначе, — и решает, что сломалось. Одна прочитанная строчка
 * «теперь наверху тот, кто ждёт дольше» снимает этот испуг целиком.
 *
 * Отметка «прочитано» ставится по факту ОТКРЫТИЯ страницы, а не по кнопке:
 * кнопку «отметить прочитанным» нажимать никто не будет, а точка на пункте
 * меню, которая не гаснет, перестаёт что-либо значить через неделю.
 */

const KIND_LABEL: Record<ChangeKind, string> = {
  новое: "Новое",
  улучшено: "Улучшено",
  исправлено: "Исправлено",
};

const MONTHS = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];

function humanDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

export function UpdatesPage() {
  useEffect(() => {
    markUpdatesSeen();
  }, []);

  return (
    <div className="lc-page updates">
      {/* Общая шапка: свой заголовок отличался от настроек кеглем и ничем
          больше. Описание сокращено до одной строки — таков канон шапки, а
          вторая половина прежней фразы («технические работы сюда не
          попадают») говорила о том, чего на экране нет. */}
      <PageHeader
        title="Что нового"
        description="Всё, что изменилось в вашей работе и видно на экране"
      />

      {RELEASES.map((release) => (
        <section key={release.version} className="lc-card updates__release">
          <div className="updates__release-head">
            <h2 className="updates__release-title">{release.title}</h2>
            <time className="updates__date" dateTime={release.date}>
              {humanDate(release.date)}
            </time>
          </div>

          <ul className="updates__list">
            {release.changes.map((c) => (
              <li key={c.what} className="updates__item">
                <span className="updates__kind" data-kind={c.kind}>
                  {KIND_LABEL[c.kind]}
                </span>
                <div className="updates__body">
                  <p className="updates__what">{c.what}</p>
                  {c.why && <p className="updates__why">{c.why}</p>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
