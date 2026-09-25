import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { ToastsBlock } from "@/features/settings/profile/ToastsBlock";
import {
  TOASTS_OFF_KEY,
  setToastsMuted,
  showToast,
  showUndoToast,
  toastsMuted,
} from "@/shared/ui/toast";

/**
 * ВЫКЛЮЧАЮТСЯ НОВОСТИ — ОТВЕТ НА СВОЁ ДЕЙСТВИЕ ПОКАЗЫВАЕТСЯ ВСЕГДА.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 28.08: «сделай так, чтобы все уведомления можно было
 * отключить визуально, и появлялись только в колокольчике».
 *
 * Выключатель стоит в ЕДИНСТВЕННОЙ точке входа `showToast` — через неё идут все
 * 87 мест, которые поднимают тост. Расставь мы проверку по вызовам, следующий
 * новый тост появился бы мимо неё, и настройка тихо перестала бы работать.
 *
 * ⚠ И ГРАНИЦА, РАДИ КОТОРОЙ ВСЁ ЭТО ПИШЕТСЯ. «Сообщение не ушло», «Состояние не
 * сохранилось», «Не подключилось» — единственная обратная связь на то, что
 * человек только что сделал сам. Спрячь её, и он уйдёт уверенный, что клиенту
 * ответили, а клиент останется без ответа. В колокольчике таких сообщений нет:
 * он про события системы, а не про судьбу нажатой кнопки.
 */
describe("Выключатель всплывашек", () => {
  beforeEach(() => {
    localStorage.removeItem(TOASTS_OFF_KEY);
    vi.spyOn(notifications, "show").mockReturnValue("id");
  });

  afterEach(() => {
    localStorage.removeItem(TOASTS_OFF_KEY);
    vi.restoreAllMocks();
  });

  it("включено — новость показывается", () => {
    showToast({ title: "Диалог принят", tone: "info", news: true });
    expect(notifications.show).toHaveBeenCalled();
  });

  it("выключено — новость молчит", () => {
    setToastsMuted(true);
    showToast({ title: "Диалог принят", tone: "info", news: true });
    expect(notifications.show).not.toHaveBeenCalled();
  });

  it("ОТКАЗ СВОЕГО ДЕЙСТВИЯ показывается даже при выключенных", () => {
    /*
     * Самая важная проверка файла. В колокольчике этого сообщения нет, и
     * приглушив его, мы отняли бы у человека единственный способ узнать, что
     * клиенту НЕ ответили.
     */
    setToastsMuted(true);
    showToast({ title: "Сообщение не ушло", tone: "danger", news: true });
    expect(notifications.show, "отказ проглочен вместе с новостями").toHaveBeenCalled();
  });

  it("старая запись цветом red считается отказом", () => {
    /*
     * Половина мест зовёт `showToast` с `color: "red"`, а не с `tone`. Разбирай
     * выключатель только `tone`, и эти отказы глохли бы — а их большинство.
     */
    setToastsMuted(true);
    showToast({ title: "Не подключилось", color: "red" });
    expect(notifications.show).toHaveBeenCalled();
  });

  it("новости-предупреждения и новости-успехи глушатся", () => {
    setToastsMuted(true);
    showToast({ title: "История загружена", color: "lp", news: true });
    showToast({ title: "Загружено частично", color: "yellow", news: true });
    expect(notifications.show).not.toHaveBeenCalled();
  });

  it("итог своего действия показывается и при выключенных — в колокольчике его нет", () => {
    // Проверка 24.09: выключатель глушил всё, кроме красного, и вместе с
    // новостями пропадали «Диалог отклонён · Вернуть» и предупреждения по
    // своему нажатию — отменить ошибку было нечем.
    setToastsMuted(true);
    showUndoToast({ title: "Диалог отклонён", actionLabel: "Вернуть", onAction: () => {} });
    showToast({ title: "Без операторов остались", tone: "warning" });
    showToast({ title: "Сохранено", color: "lp" });
    expect(notifications.show).toHaveBeenCalledTimes(3);
  });

  it("переключатель в профиле пишет и читает настройку", async () => {
    render(
      <MantineProvider>
        <ToastsBlock />
      </MantineProvider>,
    );
    await userEvent.click(screen.getByRole("switch"));
    expect(toastsMuted()).toBe(true);
    await userEvent.click(screen.getByRole("switch"));
    expect(toastsMuted()).toBe(false);
  });

  it("недоступное хранилище не ломает показ", () => {
    /*
     * Приватный режим и заблокированные сайтовые данные бросают на самом
     * обращении к хранилищу. Уронить из-за настройки показ уведомления — ровно
     * наоборот тому, зачем она заводилась.
     */
    /*
     * ⚠ ПОДМЕНЯЕМ САМО ХРАНИЛИЩЕ, А НЕ МЕТОД ПРОТОТИПА. Первая редакция
     * подменяла `Storage.prototype.getItem` — и не действовала: в этой среде
     * `localStorage` не берёт метод оттуда, диверсия «убрать try/catch» тест не
     * роняла, и проверка стерегла пустоту.
     */
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("доступ к хранилищу закрыт");
      },
      setItem: () => {
        throw new Error("доступ к хранилищу закрыт");
      },
      removeItem: () => {
        throw new Error("доступ к хранилищу закрыт");
      },
    });
    try {
      expect(() => showToast({ title: "Диалог принят", tone: "info" })).not.toThrow();
      expect(notifications.show).toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
