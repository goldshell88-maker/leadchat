import { create } from "zustand";

/**
 * Цветовые пресеты — ВТОРАЯ ОСЬ оформления (просьба владельца 04.09: «сделай
 * несколько пресетов стиля, не только чёрные и белые»).
 *
 * ⚠ ПОЧЕМУ ОТДЕЛЬНАЯ ОСЬ, А НЕ НОВЫЕ ЗНАЧЕНИЯ ТЕМЫ. Тему держит Mantine через
 * `data-mantine-color-scheme`, и значений у неё ровно три: light, dark, auto.
 * Четвёртого дать нельзя — тип `useMantineColorScheme` их и перечисляет, а из
 * этого же атрибута Mantine выводит свои переменные. Поэтому пресет живёт в
 * своём атрибуте `data-lc-preset` и УМНОЖАЕТСЯ на тему, а не заменяет её:
 * у каждого пресета есть и тёмный вид, и светлый.
 *
 * ⚠ ХРАНИТСЯ НА МАШИНЕ, А НЕ В ПРОФИЛЕ. Тот же довод, что у темы: за рабочим
 * компьютером в офисе и дома человек вполне может хотеть разного.
 *
 * ⚠ ПЕРВЫЙ КАДР СТАВИТ НЕ ЭТОТ ФАЙЛ, а встроенный скрипт в index.html — иначе
 * страница успевала бы мигнуть базовой палитрой. Здесь только смена по
 * нажатию. Список ключей обязан совпадать в четырёх местах: здесь, в скрипте
 * index.html, в блоках lc-vars.css и в сторожах; совпадение проверяет
 * `presets0409.test.ts`.
 */
export const ПРЕСЕТЫ = [
  { key: "base", label: "Изумруд" },
  { key: "indigo", label: "Индиго" },
  { key: "amethyst", label: "Аметист" },
  { key: "copper", label: "Медь" },
] as const;

export type ПресетКлюч = (typeof ПРЕСЕТЫ)[number]["key"];

const КЛЮЧ_ХРАНИЛИЩА = "lc-preset";

function прочитать(): ПресетКлюч {
  if (typeof document === "undefined") return "base";
  const стоит = document.documentElement.getAttribute("data-lc-preset");
  return ПРЕСЕТЫ.some((п) => п.key === стоит) ? (стоит as ПресетКлюч) : "base";
}

interface Состояние {
  preset: ПресетКлюч;
  setPreset: (key: ПресетКлюч) => void;
}

export const usePresetStore = create<Состояние>((set) => ({
  preset: прочитать(),
  setPreset: (key) => {
    set({ preset: key });
    if (typeof document === "undefined") return;
    /*
     * Базовый пресет — ОТСУТСТВИЕ атрибута, а не значение «base». Так базовая
     * палитра остаётся ровно тем, чем была до появления пресетов: одним
     * набором правил без лишнего слоя, который пришлось бы держать в голове
     * при каждом разборе цвета.
     */
    if (key === "base") document.documentElement.removeAttribute("data-lc-preset");
    else document.documentElement.setAttribute("data-lc-preset", key);
    try {
      if (key === "base") localStorage.removeItem(КЛЮЧ_ХРАНИЛИЩА);
      else localStorage.setItem(КЛЮЧ_ХРАНИЛИЩА, key);
    } catch {
      /*
       * Приватный режим без хранилища: выбор действует до перезагрузки. Молча
       * — потому что человек ничего не сделал не так, а сообщение об этом в
       * настройках оформления он всё равно не сможет ни на что употребить.
       */
    }
  },
}));
