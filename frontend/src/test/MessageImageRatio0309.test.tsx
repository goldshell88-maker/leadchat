import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { theme } from "@/app/theme";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { AttachmentDto, MessageDto } from "@/shared/api/types";

/**
 * МЕСТО ПОД СНИМОК РЕЗЕРВИРУЕТСЯ ДО ЗАГРУЗКИ (03.09).
 *
 * ⚠ ЧИСЛА. Строка с фотографией занимает 57 px до загрузки и 210–332 px после:
 * всё, что ниже, уезжает под курсором ровно тогда, когда человек читает.
 * Вложения у 7,5 % входящих сообщений.
 *
 * ⚠ ПРОПОРЦИЯ СТАВИТСЯ НА ОБЁРТКУ, А НЕ НА `img`. Пузырь — flex-элемент с
 * shrink-to-fit: у незагруженной картинки нулевая внутренняя ширина, пузырь
 * схлопывается до 80 px, и `100%` внутри него равно 48. Проверено замером:
 * место резервировалось в шесть раз меньше нужного.
 */

function сообщение(a: AttachmentDto): MessageDto {
  return {
    id: "m1",
    conversation_id: "c1",
    direction: "in",
    sender_type: "client",
    body: "",
    attachments: [a],
    delivery_status: "delivered",
    created_at: "2026-09-03T10:00:00Z",
  } as unknown as MessageDto;
}

function нарисовать(a: AttachmentDto) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <MessageBubble msg={сообщение(a)} prev={null} clientId="cl1" clientName="К" />
    </MantineProvider>,
  );
}

const снимок = (over: Partial<AttachmentDto>): AttachmentDto =>
  ({ media_id: "a1", kind: "image", url: "https://x/y.jpg", name: "фото", ...over }) as AttachmentDto;

describe("резерв места под снимок", () => {
  it("пропорция уходит в стиль обёртки, а не картинки", () => {
    const { container } = нарисовать(снимок({ width: 1200, height: 900 }));
    const обёртка = container.querySelector(".msg__image-wrap") as HTMLElement | null;
    expect(обёртка, "обёртка снимка не отрисовалась").not.toBeNull();
    expect(
      обёртка!.style.getPropertyValue("--ar"),
      "без --ar на обёртке место не резервируется и лента прыгает",
    ).toBe(String(1200 / 900));
    const img = container.querySelector(".msg__image") as HTMLElement | null;
    expect(img?.style.getPropertyValue("--ar") ?? "", "пропорция на самой картинке не работает").toBe(
      "",
    );
  });

  it("без размеров разметка остаётся прежней", () => {
    /*
     * У старых сообщений размеров нет. Выдуманная пропорция хуже
     * отсутствующей: она зарезервирует НЕ ТО место.
     */
    const { container } = нарисовать(снимок({}));
    const обёртка = container.querySelector(".msg__image-wrap") as HTMLElement;
    expect(обёртка.style.getPropertyValue("--ar")).toBe("");
  });

  it("половинчатые размеры не годятся", () => {
    const { container } = нарисовать(снимок({ width: 1200 }));
    const обёртка = container.querySelector(".msg__image-wrap") as HTMLElement;
    expect(обёртка.style.getPropertyValue("--ar"), "по одному числу пропорцию не построить").toBe(
      "",
    );
  });
});
