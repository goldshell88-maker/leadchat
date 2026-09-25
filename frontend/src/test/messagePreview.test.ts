/**
 * Об одном сообщении система говорит ОДНО (правка 8).
 *
 * ЧТО БЫЛО. Сообщение 00000000-0811-… приехало 11 августа с боевого аккаунта:
 * `body=null`, `attachments=[]`. Лента показывала ему «…», строка списка —
 * «Вложение», всплывающее уведомление — тоже «Вложение». Три места, три
 * разных ответа, и два из них обещали файл, которого в сообщении нет.
 *
 * Причина была на сервере (разбор не знал нетекстовых видов и сохранял пустую
 * запись), и вылечена она там же. Здесь закрывается вторая половина беды:
 * слова о содержимом сообщения больше не сочиняются в каждом месте заново, а
 * приходят из одного модуля — и для записей, накопленных ДО правки сервера,
 * тоже.
 */

import { describe, expect, it } from "vitest";
import { bubbleText, previewText, UNSUPPORTED_MESSAGE_TEXT } from "@/shared/lib/messagePreview";

const photo = { media_id: "avito_image_1", kind: "image", name: "Фотография", url: "https://a/b.jpg" };
const voice = { media_id: "avito_voice_1", kind: "file", name: "Голосовое сообщение" };

describe("слова о содержимом сообщения", () => {
  it("настоящий текст не подменяется ничем", () => {
    const msg = { body: "Здравствуйте, когда мастер?", attachments: [] };
    expect(bubbleText(msg)).toBe("Здравствуйте, когда мастер?");
    expect(previewText(msg)).toBe("Здравствуйте, когда мастер?");
  });

  it("пустое сообщение называется одинаково в ленте и в строке", () => {
    const msg = { body: null, attachments: [] };
    // Главная проверка правки: «…» против «Вложение» больше нет.
    expect(bubbleText(msg)).toBe(UNSUPPORTED_MESSAGE_TEXT);
    expect(previewText(msg)).toBe(UNSUPPORTED_MESSAGE_TEXT);
    expect(bubbleText(msg)).toBe(previewText(msg));
  });

  it("пробелы вместо текста — это пустое сообщение", () => {
    expect(bubbleText({ body: "   ", attachments: [] })).toBe(UNSUPPORTED_MESSAGE_TEXT);
  });

  it("многоточия в ленте больше не бывает", () => {
    for (const msg of [
      { body: null, attachments: [] },
      { body: "", attachments: [] },
      { body: "  ", attachments: [] },
    ]) {
      expect(bubbleText(msg)).not.toBe("…");
    }
  });

  it("вложение говорит само за себя — в пузыре подписи нет", () => {
    // Иначе под фотографией стояло бы слово «Фотография», а под голосовым —
    // второе «Голосовое сообщение»: одно и то же дважды в одном пузыре.
    expect(bubbleText({ body: null, attachments: [photo] })).toBe("");
    expect(bubbleText({ body: null, attachments: [voice] })).toBe("");
  });

  it("в одну строку вложение называется своим именем", () => {
    // Строке списка и тосту пузыря не нарисовать — там имя вложения и есть
    // всё, что можно сказать. «Фотография» полезнее общего «Вложение»:
    // голосовое от фотографии отличается решением, открывать ли диалог сейчас.
    expect(previewText({ body: null, attachments: [photo] })).toBe("Фотография");
    expect(previewText({ body: null, attachments: [voice] })).toBe("Голосовое сообщение");
  });

  it("текст важнее вложения: подпись к фото — это и есть суть", () => {
    const msg = { body: "вот такая деталь", attachments: [photo] };
    expect(previewText(msg)).toBe("вот такая деталь");
    expect(bubbleText(msg)).toBe("вот такая деталь");
  });

  it("источник без сведений о вложениях говорит «Вложение»", () => {
    /*
     * `ConversationDto.last_message` несёт только `body`, `direction` и время —
     * вложений в нём нет и не будет (это чужая зона, `services/conversations.py`).
     * Пустое тело там означает «вложение»: сервер с 12 августа НЕ сохраняет
     * запись, у которой нет ни тела, ни вложений (adapter.py `_fallback_body`),
     * поэтому пустой `body` возможен ровно там, где вложение есть.
     */
    expect(previewText({ body: null })).toBe("Вложение");
  });
});
