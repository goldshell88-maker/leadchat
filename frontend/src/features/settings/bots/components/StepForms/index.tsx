import type { BotStep } from "@/shared/api/types";
import { AiAnswerForm } from "./AiAnswerForm";
import { AskForm } from "./AskForm";
import { CloseForm } from "./CloseForm";
import { ConditionForm } from "./ConditionForm";
import { HandoffForm } from "./HandoffForm";
import { MenuForm } from "./MenuForm";
import { NoteForm } from "./NoteForm";
import { SendForm } from "./SendForm";
import { TagForm } from "./TagForm";

/**
 * Диспетчер форм по типу шага. Switch исчерпывающий по union `BotStep` —
 * добавить десятый тип и забыть форму компилятор не даст.
 */
export function StepForm({
  step,
  stepIds,
  vars,
  onChange,
}: {
  step: BotStep;
  stepIds: string[];
  vars: string[];
  onChange: (next: BotStep) => void;
}) {
  const common = { stepIds, vars, onChange };
  switch (step.type) {
    case "send":
      return <SendForm step={step} {...common} />;
    case "ask":
      return <AskForm step={step} {...common} />;
    case "menu":
      return <MenuForm step={step} {...common} />;
    case "condition":
      return <ConditionForm step={step} {...common} />;
    case "ai_answer":
      return <AiAnswerForm step={step} {...common} />;
    case "handoff":
      return <HandoffForm step={step} {...common} />;
    case "close":
      return <CloseForm step={step} {...common} />;
    case "tag":
      return <TagForm step={step} {...common} />;
    case "note":
      return <NoteForm step={step} {...common} />;
  }
}
