import type { AddressGeo } from "./clientApi";

/**
 * Слова, которыми поле объясняет вердикт карты. Ключ — статус сервера.
 *
 * С 18.09 фразы КОНСТАТИРУЮТ, а не просят решения: адрес пишет автоматика,
 * с экрана осталось только «изменить» у карточки и «Не адрес» у строки, и
 * «выберите, какой» звало бы к кнопке, которой нет.
 */
export function geoWords(
  geo: AddressGeo | null,
  locality: string | null,
  kind: "house" | "place" = "house",
  street: string | null = null,
): string | null {
  if (!geo) return null;
  switch (geo.status) {
    case "pending":
      return "проверяем по карте…";
    case "exact":
      // Место-улица («ул Пушкина, Тула», владелец 14.09): улица уже
      // названа — не хватает дома, а не улицы.
      if (kind === "place") {
        return street
          ? "улица найдена на карте — дом клиент ещё не назвал"
          : "место найдено на карте — улицу клиент ещё не назвал";
      }
      return "карта подтвердила";
    case "ambiguous":
      return "на карте несколько таких адресов";
    case "not_found":
      return "карта не нашла такой адрес";
    case "house_missing":
      return "карта нашла улицу, но не дом";
    case "house_mismatch":
      return "карта не подтвердила номер дома";
    case "street_mismatch":
      return "карта не подтвердила улицу";
    case "settlement_mismatch":
      return "карта не подтвердила посёлок";
    case "city_mismatch":
      return "карта нашла дом в другом городе";
    case "region_mismatch":
      return "карта нашла дом в другой области";
    case "other_city_in_text":
      return locality
        ? `клиент назвал другой город: ${locality}`
        : "клиент назвал другой город";
    case "no_city":
      return "город объявления неизвестен — карта не спрашивалась";
    case "elsewhere":
      return "в городе объявления такого дома нет, но он есть в области";
    case "blocked":
    case "error":
      return "карта недоступна";
    default:
      return null;
  }
}
