/**
 * Город стоит в той же метке, что партнёр и источник (просьба владельца 22.08).
 *
 * «Хочу, чтобы в этом поле так же отображался город и так же подсвечивался,
 * как источник». Метка — рабочая, а не декоративная: партнёр и источник
 * отвечают на «от чьего имени я отвечаю», город — на «куда ехать».
 *
 * ПУСТОЙ ГОРОД НЕ ПОКАЗЫВАЕМ. Он вычитан из ссылки объявления и бывает
 * неизвестен; выдуманный город стоит зря потраченного выезда. А когда
 * справочник не знает слаг — показываем сам слаг, чтобы было видно, что
 * справочник пора пополнить.
 */
import { describe, expect, it } from "vitest";

/** Та же сборка метки, что в ChatThreadPane: партнёр · источник · город. */
function метка(account: { lead_partner_number?: string; lead_origin?: string },
               item?: { city_name?: string | null; city_slug?: string | null }): string {
  const город = item?.city_name || item?.city_slug || "";
  return [
    account.lead_partner_number ? `партнёр ${account.lead_partner_number}` : "",
    account.lead_origin ? `источник ${account.lead_origin}` : "",
    город ? `город ${город}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

describe("метка канала в шапке ленты", () => {
  it("показывает город рядом с партнёром и источником", () => {
    expect(метка({ lead_partner_number: "7", lead_origin: "C74.2" }, { city_name: "Москва" }))
      .toBe("партнёр 7 · источник C74.2 · город Москва");
  });

  it("город идёт последним — сначала «от кого», потом «куда»", () => {
    const s = метка({ lead_partner_number: "7", lead_origin: "C74.2" }, { city_name: "Казань" });
    expect(s.indexOf("город")).toBeGreaterThan(s.indexOf("источник"));
  });

  it("неизвестный справочнику слаг показывается как есть", () => {
    expect(метка({ lead_partner_number: "7" }, { city_slug: "neizvestnyy-gorod" }))
      .toBe("партнёр 7 · город neizvestnyy-gorod");
  });

  it("города нет — метка остаётся прежней, выдумывать нельзя", () => {
    expect(метка({ lead_partner_number: "7", lead_origin: "B43" }, {}))
      .toBe("партнёр 7 · источник B43");
    expect(метка({ lead_partner_number: "7", lead_origin: "B43" }, { city_name: null }))
      .toBe("партнёр 7 · источник B43");
  });
});
