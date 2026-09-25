import { Menu } from "@mantine/core";
import { usePermissions } from "@/shared/auth/usePermissions";
import { canCloseConversation } from "../../lib/closeRule";
import { useSessionStore } from "@/shared/stores/sessionStore";
import type { ConversationDetailDto } from "@/shared/api/types";
import {
  IconArchive,
  IconBan,
  IconForward,
  IconInbox,
  IconMore,
  IconPin,
  IconUserPlus,
} from "@/shared/ui/Icon";
import { useTogglePin } from "../../hooks/usePins";
import { useReleaseConversation } from "../../inbox/useInbox";
import "./thread-actions.css";

/**
 * Меню «…» — ЕДИНСТВЕННОЕ место вторичных действий над диалогом
 * (перекройка от 12 августа, разбор живого экрана владельцем).
 *
 * ЧТО БЫЛО. Одни и те же действия жили В ЧЕТЫРЁХ местах: ряд из пяти иконок
 * здесь, кнопки «Взять в работу / Передать / Позвать» в правой карточке,
 * нижняя плашка «Принять диалог / Отклонить» и ховер-иконка «Передать» в
 * строке списка слева. Плюс селект «Статус» делал то же, что кнопка «Взять в
 * работу». Человек, которому надо передать диалог, каждый раз выбирал, каким
 * из четырёх способов это сделать, — а выбор без разницы это просто задержка.
 *
 * ПРАВИЛО ПЕРЕКРОЙКИ: одно действие живёт в одном месте. Первичное действие
 * («принять диалог») — внизу, в плашке, и больше нигде. Всё вторичное и
 * редкое — здесь, за «…»: закрепить, вернуть в очередь, позвать, передать,
 * пометить нежелательным, закрыть.
 *
 * ПОЧЕМУ МЕНЮ, А НЕ ИКОНКИ. Иконки без подписей были отдельным дефектом
 * (SCEN-07): подпись появлялась подсказкой лишь через 400 мс наведения, то
 * есть узнать «что это за перечёркнутый круг» можно было, только зависнув над
 * ним. Ровно поэтому же нельзя было отличить «пометить нежелательным» от
 * «закрыть» — а перепутать их дорого: первое портит карточку клиента.
 * В меню у каждого пункта СЛОВА, и место в шапке они не занимают.
 *
 * ПУСТОГО МЕНЮ НЕ БЫВАЕТ. У наблюдателя прав нет ни на одно действие, и
 * кнопка «…» ему не рисуется вовсе (было FUNC-21: скринридер объявлял группу
 * «Действия с диалогом» без единого действия внутри).
 */
export function ThreadActions({
  conversation,
  onInvite,
  onTransfer,
  onBlock,
  onClose,
  closePending,
}: {
  conversation: ConversationDetailDto;
  onInvite: () => void;
  onTransfer: () => void;
  onBlock: () => void;
  /** Закрытие диалога — спрашивает исход, поэтому окно открывает вызывающий. */
  onClose: () => void;
  closePending: boolean;
}) {
  const { can } = usePermissions();
  const me = useSessionStore((s) => s.user);
  const pin = useTogglePin(conversation.id);
  const release = useReleaseConversation(conversation.id);

  const canSend = can("messages:send");
  const canManage = can("conversations:manage");
  // Держатель — именно ответственный. Позванный коллега диалог не держит и
  // вернуть его в очередь не может: сервер ответит 403.
  const iAmHolder = Boolean(me?.id) && conversation.assignee?.id === me?.id;

  const pinned = conversation.pinned === true;
  const closed = conversation.status === "closed";

  // Закрепить может хозяин и позванный — то же правило, что на сервере; гостю
  // (зашёл сам) он отвечает 422. `kind` без значения — старый сервер: позван.
  const canPin =
    iAmHolder ||
    (conversation.participants ?? []).some((p) => p.id === me?.id && p.kind !== "self");
  // ⚠ ТОЛЬКО У АДМИНИСТРАТОРА (решение владельца 28.08: «вернуть в очередь мог
  // только бот или администратор, у менеджеров эту функцию отключи и удали,
  // чтобы её не было»). Право отдельное — `conversations:release`; сервер
  // проверяет его же.
  const canRelease = can("conversations:release") && iAmHolder && !closed;
  const canCloseConv = canCloseConversation(conversation, me?.id ?? null, can);

  // Ни одного доступного пункта — кнопки нет. Меню, которое открывается
  // пустым, обещает возможность и тут же в ней отказывает. «Закрыть» в этой
  // проверке не участвует намеренно: оно живёт внутри `canManage`, и
  // упоминание его здесь означало бы, что у закрытого диалога меню исчезает
  // вместе с передачей и пометкой клиента.
  if (!canPin && !canRelease && !canSend && !canManage) return null;

  return (
    <Menu position="bottom-end" withArrow shadow="md" width={260}>
      <Menu.Target>
        <button type="button" className="thread-actions__more" aria-label="Действия с диалогом">
          <IconMore size={18} />
        </button>
      </Menu.Target>

      <Menu.Dropdown>
        {canPin && (
          <Menu.Item leftSection={<IconPin size={16} />} onClick={() => pin.mutate(!pinned)}>
            {pinned ? "Открепить" : "Закрепить у себя"}
          </Menu.Item>
        )}

        {/*
          «Вернуть в очередь» — только у администратора и только пока диалог не
          закрыт (решение владельца 28.08). Ровно то же правило проверяет сервер.

          У менеджера пункта нет вовсе: возврат снимает ответственного и отдаёт
          тринадцати диалог, с которым человек уже поговорил. Для «я сейчас
          занят» у оператора есть «Отклонить» — оно про диалог, ЕЩЁ не начатый.

          Руководителю пункт тоже не нужен: у него есть передача, и снимать
          ответственного он должен осознанно, а не тем же жестом, которым
          оператор исправляет свою описку.
        */}
        {canRelease && (
          <Menu.Item
            leftSection={<IconInbox size={16} />}
            disabled={release.isPending}
            onClick={() => release.mutate()}
          >
            Вернуть в очередь
          </Menu.Item>
        )}

        {canSend && (
          <Menu.Item leftSection={<IconUserPlus size={16} />} onClick={onInvite}>
            Позвать коллегу
          </Menu.Item>
        )}

        {/* Закрытый диалог получатель не увидит ни в «Моих», ни в очереди. */}
        {canManage && !closed && (
          <Menu.Item leftSection={<IconForward size={16} />} onClick={onTransfer}>
            Передать диалог
          </Menu.Item>
        )}

        {/* Право то же, что у сервера: руководитель пометить может, наблюдатель — нет. */}
        {canManage && (
          <Menu.Item
            leftSection={<IconBan size={16} />}
            color={conversation.client.blocked ? undefined : "red"}
            onClick={onBlock}
          >
            {conversation.client.blocked
              ? "Снять пометку «нежелательный»"
              : "Пометить как нежелательного"}
          </Menu.Item>
        )}

        {/*
          «Закрыть диалог» переехал сюда из шапки, где стоял отдельной зелёной
          кнопкой. Довод «это самое частое действие» проверен и не подтвердился:
          закрытие делается по разу на диалог, а сочетание Ctrl+D (оно же в
          шпаргалке под полем ввода и в справке «?») работает без всякой кнопки.
          Зато рядом с «Пометить как нежелательного» отдельная заметная кнопка
          провоцировала промах между двумя действиями с разными последствиями.
        */}
        {canCloseConv && (
          <>
            <Menu.Divider />
            <Menu.Item
              leftSection={<IconArchive size={16} />}
              disabled={closePending}
              onClick={onClose}
            >
              Закрыть диалог — Ctrl+D
            </Menu.Item>
          </>
        )}
      </Menu.Dropdown>
    </Menu>
  );
}
