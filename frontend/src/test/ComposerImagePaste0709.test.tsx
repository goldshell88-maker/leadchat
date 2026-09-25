/**
 * СКРИНШОТ В ПОЛЕ ВВОДА (07.09).
 *
 * Приложить картинку можно было только скрепкой — то есть сначала сохранить
 * снимок экрана файлом на диск. Ctrl+V и перетаскивание не обрабатывались
 * нигде во всём фронте, а красная плашка вдобавок уверяла, что «Авито не
 * принимает файлы от нас»: неправда, картинки принимает двумя ручками
 * (docs/26-AVITO-API-CATALOG.md, строки 49/59/63), не принимает документы.
 *
 * ПОЧЕМУ ЗДЕСЬ ЕСТЬ СТОРОЖ НА ВСТАВКУ ТЕКСТА. Вставка текста в это поле —
 * самая частая операция за смену. Сторож «картинка попала во вложения» один
 * зеленел бы и на коде, который глотает ЛЮБУЮ вставку (`preventDefault` без
 * разбора буфера), — а такой код отнял бы у оператора Ctrl+V насовсем.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/** Ответ /media повторяет разбор сервера: pdf → kind "file" (services/media.py). */
function mediaResponse(file: File) {
  const картинка = file.type.startsWith("image/");
  return jsonResponse(201, {
    media_id: "m-1",
    kind: картинка ? "image" : "file",
    url: "/api/v1/media/m-1",
    name: file.name,
    size: file.size,
  });
}

describe("Композер: картинка из буфера и перетаскиванием", () => {
  let последнийФайл: File | null = null;

  beforeEach(() => {
    последнийФайл = null;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/media") && init?.body instanceof FormData) {
          const file = init.body.get("file") as File;
          последнийФайл = file;
          return mediaResponse(file);
        }
        return jsonResponse(200, { items: [], page: { next_cursor: null, prev_cursor: null } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const поле = () => screen.getByLabelText("Текст сообщения");
  const чипы = () => document.querySelectorAll(".composer__chip");

  /** Буфер/перетаскивание в jsdom: DataTransfer здесь не реализован. */
  const буфер = (files: File[], types = ["Files"]) => ({ files, items: [], types });

  it("вставка картинки из буфера кладёт её во вложения — с именем снимка экрана", async () => {
    /*
     * ДИВЕРСИЯ: убрать `onPaste={handlePaste}` у поля — тест краснеет
     * («ожидали чип, а вложений нет»). Вторая диверсия: вернуть имя как есть
     * (`return file` в начале `снимкуИмя`) — краснеет проверка имени.
     */
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    // Скриншот из Windows приезжает с ПУСТЫМ именем — самый частый случай.
    const снимок = new File(["png"], "", { type: "image/png" });
    fireEvent.paste(поле(), { clipboardData: буфер([снимок]) });

    await waitFor(() =>
      expect(document.querySelector('.composer__chip[data-state="ready"]')).not.toBeNull(),
    );
    expect(screen.getByText(/^Снимок экрана \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.png$/)).toBeTruthy();
    // На сервер ушёл файл с тем же именем, а не безымянный.
    expect(последнийФайл?.name).toMatch(/^Снимок экрана /);
  });

  it("имя из проводника сохраняется — датой его не затирает", async () => {
    /*
     * ДИВЕРСИЯ: переименовывать всегда (снять проверку `своё` в `снимкуИмя`) —
     * тест краснеет: вместо «счёт-1024.png» в чипе «Снимок экрана …».
     */
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    fireEvent.paste(поле(), {
      clipboardData: буфер([new File(["png"], "счёт-1024.png", { type: "image/png" })]),
    });

    expect(await screen.findByText("счёт-1024.png")).toBeTruthy();
  });

  it("вставка ТЕКСТА по-прежнему вставляет текст и вложения не создаёт", async () => {
    /*
     * ДИВЕРСИЯ: снять в `handlePaste` условие `if (картинки.length === 0)
     * return` — то есть глотать любую вставку — тест краснеет: поле остаётся
     * пустым. Это парный сторож к первому: без него «ест всё подряд» прошло бы.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.click(поле());
    await user.paste("ул. Ленина, 5");

    expect((поле() as HTMLTextAreaElement).value).toBe("ул. Ленина, 5");
    expect(чипы()).toHaveLength(0);
  });

  it("перетаскивание файла на панель кладёт его во вложения", async () => {
    /*
     * ДИВЕРСИЯ: убрать `onDrop={handleDrop}` у `<footer className="composer">`
     * — тест краснеет, вложений нет.
     */
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const панель = screen.getByLabelText("Панель отправки");

    const файл = new File(["png"], "деталь.png", { type: "image/png" });
    fireEvent.dragOver(панель, { dataTransfer: буфер([файл]) });
    // Пока файл над панелью — подсветка зоны, иначе непонятно, примут ли его.
    expect(document.querySelector(".composer__drop")).not.toBeNull();

    fireEvent.drop(панель, { dataTransfer: буфер([файл]) });

    expect(await screen.findByText("деталь.png")).toBeTruthy();
    expect(document.querySelector(".composer__drop")).toBeNull();
  });

  it("перетаскивание ТЕКСТА панель не перехватывает", () => {
    /*
     * ДИВЕРСИЯ: убрать `тащатФайл` из `handleDragOver` — краснеет: подсветка
     * появляется на перетаскивании выделенного текста внутри поля, то есть на
     * обычной правке набранного.
     */
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const панель = screen.getByLabelText("Панель отправки");

    fireEvent.dragOver(панель, { dataTransfer: буфер([], ["text/plain"]) });

    expect(document.querySelector(".composer__drop")).toBeNull();
  });

  describe("что уходит клиенту, а что только в заметку", () => {
    const приложить = async (user: ReturnType<typeof userEvent.setup>, file: File) => {
      const input = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(input, file);
      await waitFor(() =>
        expect(document.querySelector('.composer__chip[data-state="ready"]')).not.toBeNull(),
      );
    };

    const кнопкаОтправки = () =>
      screen.getByLabelText("Отправить сообщение").closest("button") as HTMLButtonElement;

    it("картинка отправку не гасит — Авито её принимает", async () => {
      /*
       * ДИВЕРСИЯ: вернуть прежнее `readyAttachments.length > 0 && !isNote` —
       * тест краснеет: кнопка отправки погашена при картинке.
       */
      const user = userEvent.setup();
      renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

      await user.type(поле(), "вот такая деталь");
      await приложить(user, new File(["png"], "деталь.png", { type: "image/png" }));

      expect(кнопкаОтправки()).not.toBeDisabled();
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("PDF отправку гасит, и плашка говорит именно про документ", async () => {
      /*
       * ДИВЕРСИЯ: заменить условие на `false` (никогда не гасим) — тест
       * краснеет дважды: кнопка активна и плашки нет. Парность с проверкой
       * выше обязательна: сторож «PDF гасит» в одиночку зеленел бы и на старом
       * коде, который гасил ВСЁ подряд.
       */
      const user = userEvent.setup();
      renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

      await user.type(поле(), "смета по ремонту");
      await приложить(user, new File(["%PDF-1.4"], "смета.pdf", { type: "application/pdf" }));

      expect(кнопкаОтправки()).toBeDisabled();
      const плашка = await screen.findByRole("alert");
      expect(плашка.textContent).toMatch(/PDF/);
      expect(плашка.textContent).toMatch(/заметке/);
    });
  });
});
