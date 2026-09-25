/**
 * Превью строки диалога не съедается чипом и бейджем разом.
 *
 * ⚠ ПРАВИЛО «ОДИН ЧИП НА СТРОКУ» существует с UX-аудита (docs/17 §Т8): два
 * чипа съедали превью до многоточия, а именно по превью оператор узнаёт
 * диалог, не открывая его. Бейдж непрочитанного был ВТОРЫМ чипом, которого это
 * правило не заметило.
 *
 * Считано: под превью 186 пикселей, чип «никто не берёт» — 93, бейдж с зазором
 * — 26. На текст остаётся полсотни, то есть «Ма…».
 *
 * ⚠ ГРАНИЦА ПРАВИЛА ВАЖНЕЕ САМОГО ПРАВИЛА. Бейдж снят ТОЛЬКО у брошенного
 * диалога: от него отказались все, кому он доступен, значит он непрочитан по
 * определению и число ничего не добавляет. У «негатива» и «не отправлено»
 * бейдж остаётся — они про настроение и доставку, а не про чтение.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import type { ConversationDto } from "@/shared/api/types";

function строка(over: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id: "conv-1",
    status: "new",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: "c1", name: "Мария Ковалёва", phone: null, avito_rating: null },
    assignee: null,
    item: { title: "Ремонт стиральной машины", url: null, price: null },
    last_message: {
      body: "Машинка не сливает воду, шумит при отжиме",
      direction: "in",
      created_at: "2026-09-04T09:40:12Z",
    },
    unread_count: 3,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-04T09:40:12Z",
    ...over,
  } as ConversationDto;
}

function нарисовать(row: ConversationDto) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>
        <ConversationListItem
          row={row}
          active={false}
          onOpen={() => {}}
          showChannel={false}
          now={Date.parse("2026-09-04T10:00:00Z")}
        />
      </MemoryRouter>
    </MantineProvider>,
  );
}

describe("Место под превью в строке диалога", () => {
  it("у брошенного диалога бейджа непрочитанного нет", () => {
    const { container } = нарисовать(строка({ escalated: true } as Partial<ConversationDto>));
    expect(screen.getByText("никто не берёт")).toBeInTheDocument();
    expect(container.querySelector(".conv-card__unread"), "бейдж остался рядом с чипом").toBeNull();
  });

  it("у «негатива» бейдж остаётся: чип про настроение, а не про чтение", () => {
    const { container } = нарисовать(строка({ tags: ["негатив"] }));
    expect(container.querySelector(".conv-card__unread")).not.toBeNull();
  });

  it("без чипа бейдж на месте", () => {
    const { container } = нарисовать(строка());
    expect(container.querySelector(".conv-card__unread")).not.toBeNull();
  });
});
