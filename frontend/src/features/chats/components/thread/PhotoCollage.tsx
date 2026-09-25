import { memo, type CSSProperties } from "react";
import { withApiRoot } from "@/shared/api/http";
import type { MessageDto } from "@/shared/api/types";
import { formatClock } from "@/shared/lib/formatTime";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import { ClientAvatar } from "../list/ClientAvatar";
import { DayDivider, DeliveryStatusIcon } from "./MessageBubble";
import { kindOf, колонок, сменаДня, тотЖеАвтор, type Снимок } from "./messageSeries";
import "./photo-collage.css";

/**
 * КОЛЛАЖ: ПОДРЯД ИДУЩИЕ СНИМКИ — ОДИН ПУЗЫРЬ (просьба владельца 05.09: «хочу,
 * чтобы фото собирались в коллаж в один и не растягивали переписку»).
 *
 * ЧТО БЫЛО. Клиент присылает фотографии пачкой, каждую отдельным сообщением
 * (за 60 дней: серий по 2 — 1414, по 3 — 583, по 7 — 19). Лента давала на них
 * столько же пузырей по 280 px: семь снимков — почти два экрана, после
 * которых вопрос клиента уезжает под обрез, а оператор листает вслепую.
 *
 * ЧТО СТАЛО. Одна сетка: две колонки при 2–4 снимках, три при 5 и больше.
 * Семь снимков занимают три ряда — около 380 px вместо двух тысяч.
 *
 * ⚠ ПОЧЕМУ ПЛИТКИ КВАДРАТНЫЕ, ХОТЯ ОДИНОЧНЫЙ СНИМОК МЫ НАРОЧНО НЕ РЕЖЕМ.
 * У одиночного снимка `object-fit: contain` — там важен край, где текст
 * ошибки. В сетке `contain` дал бы семь разных прямоугольников с полями:
 * ряды поехали бы по высоте, и место под коллаж нельзя было бы занять
 * заранее. Плитка — это не просмотр, а ВХОД в просмотр: нажатие открывает
 * снимок целиком и в своих пропорциях (`ImageViewer`), где ничего не обрезано.
 *
 * ⚠ МЕСТО ЗАНИМАЕТСЯ ДО ЗАГРУЗКИ, И ЗДЕСЬ ЭТО НАДЁЖНЕЕ, ЧЕМ У ОДИНОЧНОГО.
 * Одиночный снимок держит пропорцию сервера (`--ar`), а у сообщений старше
 * правки 03.09 размеров нет вовсе — там лента по-прежнему прыгает. Квадратной
 * плитке размеры не нужны: высота ряда известна из ширины сетки, то есть
 * коллаж не двигает ленту под курсором даже на старой истории.
 */
export const PhotoCollage = memo(function PhotoCollage({
  shots,
  prev,
  clientId,
  clientName,
  clientAvatarUrl,
  onOpenImage,
  onReply,
}: {
  /** Снимки серии в порядке ленты: первый — самый старый. */
  shots: Снимок[];
  /** Предыдущее СООБЩЕНИЕ ленты (не строка) — как у пузыря. */
  prev: MessageDto | null;
  clientId: string;
  clientName: string;
  clientAvatarUrl?: string | null;
  onOpenImage?: (mediaId: string) => void;
  onReply?: (msg: MessageDto) => void;
}) {
  const первый = shots[0].msg;
  const последний = shots[shots.length - 1].msg;
  const kind = kindOf(первый);
  const showDivider = сменаДня(prev, первый);
  const collapsed = тотЖеАвтор(prev, первый);
  const cols = колонок(shots.length);

  /*
   * ⚠ ВРЕМЯ У СЕРИИ ОДНО — ПОСЛЕДНЕГО СНИМКА, И ЭТО НЕ ЛЕНЬ.
   *
   * Пузырь ставит часы под своим содержимым, и читаются они как «когда это
   * появилось на экране». У серии такой момент один — когда клиент прислал
   * последнее; по нему же считается пауза до следующего сообщения. Часы
   * первого снимка врали бы в обратную сторону: «прислано в 12:03», хотя
   * фотографии шли до 12:05, и оператор, который в 12:04 отвечал, решил бы,
   * что ответил на всё.
   *
   * Разброс серии не пропадает: при разном времени начала и конца он стоит в
   * подсказке. Замер боя — серии идут секундами, так что чаще подсказки нет.
   */
  const началось = formatClock(первый.created_at);
  const кончилось = formatClock(последний.created_at);
  const подсказкаВремени =
    началось === кончилось
      ? `${shots.length} снимков в ${кончилось}`
      : `${shots.length} снимков: с ${началось} до ${кончилось}`;

  /*
   * ⚠ ОТМЕТКА ДОСТАВКИ У СЕРИИ ЧЕСТНА ПОТОМУ, ЧТО В СЕРИЮ БЕРУТ ТОЛЬКО
   * ДОСТАВЛЕННОЕ. Ни `pending`, ни `failed` сюда не попадают (правило в
   * `messageSeries.снимокСообщения`), поэтому одна галочка на всю сетку
   * говорит ровно то, что говорят все семь по отдельности. Появись здесь хоть
   * одно недоставленное — галочка стала бы обещанием за него, и по этой самой
   * причине оно и не склеивается.
   */
  const showsDelivery = kind === "out";

  return (
    <>
      {showDivider && <DayDivider msg={первый} />}
      <div className={`msg msg--${kind}${collapsed ? " msg--cont" : ""}`} role="listitem">
        {kind === "in" &&
          (collapsed ? (
            // Место аватара сохраняем пустым: без него пузыри группы съезжают
            // влево и лесенка ломается.
            <div className="msg__avatar msg__avatar--placeholder" aria-hidden="true" />
          ) : (
            <div className="msg__avatar">
              <ClientAvatar clientId={clientId} name={clientName} src={clientAvatarUrl} size={32} />
            </div>
          ))}
        <div className="msg__bubble">
          {kind === "out" && !collapsed && (
            <span className="msg__author">{подписьСотрудника(первый.sender, "Оператор")}</span>
          )}
          <div
            className="msg__shots"
            /* Число колонок — единственное, что меняется от снимка к снимку;
               держать под него два класса значило бы прописать правило дважды. */
            style={{ "--shots-cols": cols } as CSSProperties}
          >
            {shots.map(({ att }, i) => (
              <button
                key={att.media_id}
                type="button"
                className="msg__shot"
                onClick={() => onOpenImage?.(att.media_id)}
                /* Порядковый номер в подписи — не украшение: без него читалка
                   с экрана произносит семь одинаковых «Открыть снимок», и
                   понять, где ты в сетке, нельзя. */
                aria-label={`Открыть снимок ${i + 1} из ${shots.length}: ${att.name}`}
                title={att.name}
                disabled={!onOpenImage}
              >
                <img
                  className="msg__shot-img"
                  src={withApiRoot(att.url as string)}
                  alt={att.name}
                  loading="lazy"
                  /* Мимо главного потока: снимок в ленте иначе даёт рывок
                     прокрутки ровно в момент, когда его дорисовали. */
                  decoding="async"
                  /*
                   * Ссылки Авито живут не вечно. Без этого место плитки
                   * занимал бы значок битой картинки — «клиент прислал
                   * пустоту», хотя он прислал фотографию, а просрочилась
                   * ссылка. Соседние плитки при этом целы, и место сетки не
                   * меняется: у сломанной остаётся её квадрат.
                   */
                  onError={(e) => {
                    e.currentTarget.closest(".msg__shot")?.setAttribute("data-broken", "true");
                  }}
                />
                <span className="msg__shot-broken">Не открылся</span>
              </button>
            ))}
          </div>
          <span className="msg__meta">
            {/*
              «Ответить» у серии ОДНО и ведёт на ПОСЛЕДНИЙ снимок.
              Цитата всё равно покажет «Вложение» — снимок в неё не влезает, —
              так что выбирать между семью одинаковыми цитатами не из чего.
              Последний выбран потому, что именно он на экране в тот момент,
              когда оператор решает ответить.
            */}
            {onReply && (
              <button
                type="button"
                className="msg__reply-btn"
                onClick={() => onReply(последний)}
                aria-label="Ответить на эти снимки"
                title="Ответить на эти снимки"
              >
                Ответить
              </button>
            )}
            <span title={подсказкаВремени}>{кончилось}</span>
            {showsDelivery && <DeliveryStatusIcon status="delivered" />}
          </span>
        </div>
      </div>
    </>
  );
});
