import { useEffect, useState } from "react";
import { useMediaQuery } from "@mantine/hooks";
import { useParams } from "react-router-dom";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { ClientCardPane } from "./components/card/ClientCardPane";
import { ChatListPane } from "./components/list/ChatListPane";
import { ChatThreadPane } from "./components/thread/ChatThreadPane";
import { NotifyNudge } from "@/shared/ui/ПредложитьУведомления";
import { useDialogPin } from "./выборДиалога";
import { MOBILE_QUERY, узкоеМесто } from "@/shared/lib/breakpoints";
import { useWorkspaceWidth } from "./ширинаМеста";
import "./chats-page.css";

/**
 * ПОДЛОЖКА ПОД ОВЕРЛЕЕМ КАРТОЧКИ КЛИЕНТА — ВЫХОД, А НЕ УКРАШЕНИЕ.
 *
 * Карточка на узком экране ложится `position: fixed` на правые 380px и
 * накрывает собой всё, что там было. Замер со стенда на 1280: карточка
 * занимает x 900–1280, а под ней целиком остаются «Принять диалог»
 * (x 1009–1150) и «Отклонить» (x 1158–1264) — `document.elementFromPoint` в
 * этих точках отдаёт содержимое карточки, то есть диалог из очереди взять
 * НЕВОЗМОЖНО. Накрыты и все действия шапки, включая саму кнопку «Клиент»,
 * которой карточку открыли: закрыть её было нечем. Esc работал, но о нём
 * нигде не написано, а операторов тринадцать и приходят они из Jivo, а не из
 * документации.
 *
 * Подложка возвращает выход, который человек находит сам: клик мимо карточки
 * её закрывает. Кнопку закрытия внутрь карточки не ставим — она общая с
 * трёхколоночной раскладкой, где закрывать нечего.
 *
 * z-index ТОТ ЖЕ, что у карточки: слои равны, и порядок решает разметка —
 * подложка идёт перед карточкой, значит карточка рисуется поверх неё. Меньшее
 * значение здесь было бы вернее по смыслу, но потребовало бы второго токена
 * рядом с `--lc-z-overlay`, и любое расхождение этих двух чисел прятало бы
 * карточку под собственную подложку.
 */
function ClientCardBackdrop({ onClose }: { onClose: () => void }) {
  return (
    <button
      type="button"
      className="client-card-backdrop"
      aria-label="Закрыть карточку клиента"
      title="Закрыть карточку клиента"
      onClick={onClose}
      style={{
        position: "fixed",
        top: "var(--lc-header-h)",
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: "var(--lc-z-overlay)",
        border: "none",
        padding: 0,
        background: "var(--lc-overlay-scrim)",
        cursor: "pointer",
      }}
    />
  );
}

/**
 * Рабочее место /chats (11 §2.0, адаптив 03 §8.2): список 320px │ лента flex-1 │
 * карточка клиента 340px. Когда рабочему месту (окну за вычетом рельсы)
 * остаётся меньше 1360px, карточка сворачивается в оверлей по кнопке «Клиент»
 * в шапке ленты; ниже 768px ОКНА — стек из двух экранов. Сами пороги живут в
 * shared/lib/breakpoints.ts: их знают и CSS, и React.
 * URL — источник истины по активному диалогу; стор — зеркало для WS/unread.
 */
export function ChatsPage() {
  const { id } = useParams();
  const setActive = useChatUiStore((s) => s.setActive);
  const clientCardOpen = useChatUiStore((s) => s.clientCardOpen);
  const setClientCardOpen = useChatUiStore((s) => s.setClientCardOpen);
  const isMobile = useMediaQuery(MOBILE_QUERY, false, { getInitialValueInEffect: false });
  /*
   * ⚠ УЗКУЮ РАСКЛАДКУ РЕШАЕТ ЗАМЕР МЕСТА, А НЕ МЕДИАЗАПРОС ПО ОКНУ (08.09).
   *
   * Медиазапрос не умеет вычесть левую рельсу, а она развёрнута по умолчанию и
   * забирает 218 пикселей: на окне 1440 ленте оставалось 478 вместо
   * объявленных 616, и так на всей полосе 1360–1699. CSS перешёл на
   * контейнерный запрос по `.lc-main`; здесь — наблюдатель размера на том же
   * прямоугольнике, чтобы слои не разошлись (разбор в `ширинаМеста.ts`).
   *
   * Ссылка на элемент берётся состоянием, а не `useRef`: между мобильной и
   * настольной ветками `.chats-page` пересоздаётся, и наблюдатель, привязанный
   * в эффекте по `ref`, остался бы висеть на выброшенном узле. Обратный вызов
   * в `ref` срабатывает на каждую подмену узла.
   *
   * Порог телефона остаётся на окне намеренно: по нему сворачивается САМА
   * рельса (`useRailExpanded`), и считать его от места значило бы замкнуть
   * круг — довод записан в `shared/lib/breakpoints.ts`.
   */
  const [место, setМесто] = useState<HTMLDivElement | null>(null);
  const isNarrow = узкоеМесто(useWorkspaceWidth(место));

  useChatHotkeys();

  // Замок «с недописанного ответа уводит только человек» — разбор в модуле.
  useDialogPin(id);

  // Зеркалим :id в стор (03 §6): WsClient/unreadStore не знают про роутер.
  useEffect(() => {
    setActive(id ?? null);
    return () => setActive(null);
  }, [id, setActive]);

  // Смена диалога закрывает оверлей карточки — иначе он перекрывает новую ленту.
  useEffect(() => {
    setClientCardOpen(false);
    // Плашка «Диалог принял …» живёт ровно столько, сколько открыт тот диалог:
    // ушли к следующему — она не должна прикрывать поле ввода в чужой ленте.
    useInboxStore.getState().clearClaimed();
  }, [id, setClientCardOpen]);

  // Мобильный «стек» (03 §8.2): /chats — только список, /chats/:id — только лента.
  if (isMobile) {
    return (
      <>
        <NotifyNudge />
        <div className="chats-page chats-page--mobile" ref={setМесто}>
          {id ? <ChatThreadPane convId={id} /> : <ChatListPane />}
          {id && clientCardOpen && (
            <>
              <ClientCardBackdrop onClose={() => setClientCardOpen(false)} />
              <ClientCardPane key={id} convId={id} overlay />
            </>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      {/*
        Полоса «включите уведомления» — ПЕРЕД рабочим местом и вне `.chats-page`:
        та колонка раскладывает свои три панели в строку, и четвёртым ребёнком
        полоса встала бы рядом со списком. Здесь она сестра рабочего места в
        колонке `lc-main`, то есть отодвигает его вниз ровно на свою высоту.
        Всё остальное — показывать ли её вообще — решает она сама.
      */}
      <NotifyNudge />
      <div className="chats-page" ref={setМесто}>
        <ChatListPane />
        <ChatThreadPane convId={id ?? null} />
        {isNarrow ? (
          clientCardOpen && (
            <>
              <ClientCardBackdrop onClose={() => setClientCardOpen(false)} />
              <ClientCardPane key={id ?? "none"} convId={id ?? null} overlay />
            </>
          )
        ) : (
          /*
           * ⚠ КЛЮЧ ОБЯЗАТЕЛЕН, И ЭТО НЕ ОПТИМИЗАЦИЯ, А ЗАЩИТА ОТ ЗАПИСИ В ЧУЖУЮ
           * КАРТОЧКУ.
           *
           * На широком экране карточка висит в потоке постоянно и при смене
           * диалога НЕ пересоздавалась. Начатая правка имени живёт её локальным
           * состоянием: оператор нажал «изменить», набрал имя, не сохранил и
           * щёлкнул другой диалог. Если деталь второго уже в кэше (а она там
           * тридцать минут), карточка даже не мигнёт скелетоном — поле правки
           * останется открытым с набранным текстом, но `clientId` внутри будет
           * УЖЕ ДРУГОЙ. Нажатие «Сохранить» переписывает имя чужому клиенту.
           *
           * Лента этот приём применяет с самого начала (`key={convId}` в
           * `ChatThreadPane`); карточка просто осталась без него.
           */
          <ClientCardPane key={id ?? "none"} convId={id ?? null} />
        )}
      </div>
    </>
  );
}
