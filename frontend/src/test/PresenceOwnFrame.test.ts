import { beforeEach, describe, expect, it } from "vitest";
import { usePresenceStore } from "@/features/presence/usePresence";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { resetSessionStore } from "./helpers";

/**
 * Своё состояние, выставленное с другого устройства, приходит кадром
 * `presence:online`. Раньше кадр выбрасывался: экран показывал «На месте», а
 * общая запись «что отправляли» говорила «уже отправлено» — и действие здесь
 * не возвращало человека в «На месте», пока сторож забирал его диалоги.
 */
describe("Своё состояние с другого устройства", () => {
  beforeEach(() => {
    localStorage.clear();
    resetSessionStore({ user: { id: "me", full_name: "Я", role: "manager" } as never });
    usePresenceStore.getState().set("online");
    localStorage.setItem("lc:presence:pushed", "online");
  });

  it("видно и здесь — на экране и в общей записи", () => {
    applyWsEvent({
      type: "presence:online",
      ts: new Date().toISOString(),
      data: { user_id: "me", status: "away" },
    });

    expect(usePresenceStore.getState().status).toBe("away");
    expect(localStorage.getItem("lc:presence:pushed")).toBe("away");
  });

  it("чужой статус моё не трогает", () => {
    applyWsEvent({
      type: "presence:online",
      ts: new Date().toISOString(),
      data: { user_id: "colleague", status: "away" },
    });

    expect(usePresenceStore.getState().status).toBe("online");
  });
});
