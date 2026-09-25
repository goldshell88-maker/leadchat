import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup, configure } from "@testing-library/react";

/*
 * ОЖИДАНИЕ ДОЛЖНО ПЕРЕЖИВАТЬ ЗАГРУЖЕННУЮ МАШИНУ.
 *
 * У testing-library ожидание по умолчанию — ОДНА СЕКУНДА. Пока набор был
 * небольшим, её хватало; к 12 августа он вырос до 140 файлов и тысячи с лишним
 * проверок, которые идут параллельно, и секунды перестало хватать под
 * нагрузкой. Проявлялось это как «мигающий тест»: `TeamStatusPager` падал в
 * общем прогоне и проходил в одиночку — трижды за день, каждый раз на разном
 * месте.
 *
 * Чинить это правкой одного теста — значит ждать, пока замигает следующий
 * медленный: поводок короткий у ВСЕХ, просто до сих пор натягивался у одного.
 *
 * Пять секунд не прячут настоящую поломку: сломанное утверждение всё равно
 * упадёт, только позже. Прячет — короткое ожидание, потому что заставляет
 * читать «сломалось» как «показалось».
 */
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
});

// Mantine needs matchMedia and ResizeObserver, which jsdom does not provide.
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  });
}

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (typeof window.ResizeObserver !== "function") {
  Object.defineProperty(window, "ResizeObserver", {
    writable: true,
    value: ResizeObserverMock,
  });
}

if (typeof window.HTMLElement.prototype.scrollIntoView !== "function") {
  window.HTMLElement.prototype.scrollIntoView = () => {};
}

// Node ≥22 ships an experimental global localStorage that is undefined without
// --localstorage-file and shadows jsdom's implementation — zustand/persist then
// crashes on storage.setItem. Install an in-memory Storage shim when broken.
class MemoryStorage implements Storage {
  private data = new Map<string, string>();
  get length() {
    return this.data.size;
  }
  clear() {
    this.data.clear();
  }
  getItem(key: string) {
    return this.data.has(key) ? (this.data.get(key) as string) : null;
  }
  key(index: number) {
    return Array.from(this.data.keys())[index] ?? null;
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
  setItem(key: string, value: string) {
    this.data.set(key, String(value));
  }
}

const localStorageBroken = (() => {
  try {
    const probe = globalThis.localStorage;
    if (!probe) return true;
    probe.setItem("__probe", "1");
    probe.removeItem("__probe");
    return false;
  } catch {
    return true;
  }
})();

if (localStorageBroken) {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: new MemoryStorage(),
  });
}
