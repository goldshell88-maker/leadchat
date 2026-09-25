import { describe, expect, it } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { ClientAvatar } from "@/features/chats/components/list/ClientAvatar";

/**
 * ФОТО КЛИЕНТА ИЗ АВИТО (просьба владельца со снимками Jivo, 15 августа).
 *
 * Ссылка на картинку ЧУЖАЯ — CDN Авито, — поэтому состояний три, и каждое
 * обязано быть покрыто: без ссылки инициалы (как всегда было), со ссылкой
 * картинка, с протухшей ссылкой — снова инициалы. Битая картинка в кружке
 * читалась бы как поломка системы, хотя сломалась чужая ссылка.
 */
describe("Аватар клиента: фото и откат на инициалы", () => {
  it("без ссылки — инициалы, как всегда было", () => {
    const { container } = render(<ClientAvatar clientId="c-1" name="Инна Петрова" />);

    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("ИП");
  });

  it("со ссылкой — картинка, а не инициалы", () => {
    const { container } = render(
      <ClientAvatar clientId="c-1" name="Инна Петрова" src="https://cdn/128.png" />,
    );

    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img).toHaveAttribute("src", "https://cdn/128.png");
    // Чужой CDN не должен получать адрес нашей системы в Referer.
    expect(img).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(container.textContent).toBe("");
  });

  it("ссылка протухла — молча возвращаемся к инициалам", () => {
    const { container } = render(
      <ClientAvatar clientId="c-1" name="Инна Петрова" src="https://cdn/dead.png" />,
    );

    fireEvent.error(container.querySelector("img") as HTMLImageElement);

    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("ИП");
  });
});
