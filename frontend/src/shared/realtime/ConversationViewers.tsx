import { useOtherViewers, viewersLabel } from "./viewersStore";
import "./conversation-viewers.css";

/**
 * Постоянный признак «этот диалог открыт не только у вас» (SCEN-48).
 *
 * Тост в `applyWsEvent` кричит в момент столкновения и гаснет; этот признак
 * висит всё время, пока сосед в диалоге, — и отвечает на вопрос, который
 * человек задаёт себе прямо перед отправкой: «я тут один?». До правки ответа
 * на него не было вовсе, и двое диспетчеров писали клиенту по очереди, каждый
 * будучи уверен, что пишет он один.
 *
 * Компонент лежит рядом с механизмом, а не на экране чатов, намеренно: он
 * ничего не знает про раскладку и целиком определяется составом зрителей —
 * место, куда его поставить, выбирает экран диалога одной строкой
 * `<ConversationViewers conversationId={id} />`.
 */
export function ConversationViewers({ conversationId }: { conversationId: string | null }) {
  const others = useOtherViewers(conversationId);
  if (others.length === 0) return null;

  return (
    <div className="conv-viewers" role="status" data-testid="conversation-viewers">
      <span className="conv-viewers__dot" aria-hidden="true" />
      {/* Полный список — в подсказке: в строке трое уже не помещаются, а знать,
          кто именно сидит в диалоге, нужно, чтобы с ним и договориться. */}
      <span className="conv-viewers__text" title={others.map((v) => v.full_name).join(", ")}>
        {viewersLabel(others)}
      </span>
    </div>
  );
}
