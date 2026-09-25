import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { StatCard } from "@/features/stats/components/StatCard";
import { deltaArrow, deltaHint, deltaTone, isInvertedMetric } from "@/features/stats/lib/delta";
import { formatDelta, formatDuration, formatNumber, formatPercent } from "@/features/stats/lib/format";
import { renderWithProviders } from "./render";

/**
 * Семантика дельты (11 §6.2): рост — зелёный у всех метрик, КРОМЕ времени ответа,
 * где «меньше — лучше». Ошибка здесь = руководитель читает ухудшение как рост.
 */
describe("Дельта карточек — цвет по семантике метрики", () => {
  it("обычные метрики: рост зелёный, падение красное", () => {
    expect(deltaTone("conversations_new", 11.4)).toBe("good");
    expect(deltaTone("conversations_closed", -3.7)).toBe("bad");
    expect(deltaTone("phones_collected", 20)).toBe("good");
    expect(deltaTone("bot_closed", 3.4)).toBe("good");
  });

  it("инвертированные метрики (время ответа): падение зелёное, рост красный", () => {
    expect(isInvertedMetric("frt_operator")).toBe(true);
    expect(deltaTone("frt_operator", -20.8)).toBe("good");
    expect(deltaTone("frt_operator", 20.8)).toBe("bad");
    expect(deltaTone("frt_median_sec", 5)).toBe("bad");
    expect(deltaTone("frt_operator_median", -5)).toBe("good");
  });

  it("snapshot-метрики и нулевая дельта — нейтрально", () => {
    expect(deltaTone("in_progress_now", null)).toBe("neutral");
    expect(deltaTone("waiting_now", undefined)).toBe("neutral");
    expect(deltaTone("conversations_new", 0)).toBe("neutral");
    expect(deltaTone("frt_operator", Number.NaN)).toBe("neutral");
  });

  it("стрелка и пояснение «быстрее / медленнее»", () => {
    expect(deltaArrow(11.4)).toBe("▲");
    expect(deltaArrow(-4)).toBe("▼");
    expect(deltaArrow(null)).toBe("");
    expect(deltaHint("frt_operator", -20.8)).toBe("быстрее");
    expect(deltaHint("frt_operator", 20.8)).toBe("медленнее");
    expect(deltaHint("conversations_new", 20.8)).toBeNull();
  });

  it("форматирование значений и дельт", () => {
    expect(formatDuration(95)).toBe("1 м 35 с");
    expect(formatDuration(35)).toBe("35 с");
    expect(formatDuration(120)).toBe("2 м");
    expect(formatDuration(7500)).toBe("2 ч 05 м");
    expect(formatDuration(null)).toBe("—");
    expect(formatNumber(1834)).toContain("834");
    expect(formatPercent(18.6)).toBe("18,6 %");
    expect(formatDelta(11.44)).toBe("+11,4 %");
    expect(formatDelta(-3.7, "пп")).toBe("−3,7 пп");
    expect(formatDelta(null)).toBe("—");
  });
});

describe("StatCard — подача дельты", () => {
  it("время ответа снизилось: зелёная дельта с пометкой «быстрее»", () => {
    const { container } = renderWithProviders(
      <StatCard metricId="frt_operator" title="Первый ответ (медиана)" value="1 м 35 с" deltaPct={-20.8} />,
    );

    const delta = container.querySelector(".stat-card__delta");
    expect(delta).toHaveAttribute("data-tone", "good");
    expect(delta?.textContent).toContain("−20,8 %");
    expect(delta?.textContent).toContain("быстрее");
  });

  it("время ответа выросло — красная дельта", () => {
    const { container } = renderWithProviders(
      <StatCard metricId="frt_operator" title="Первый ответ" value="3 м" deltaPct={12} />,
    );
    expect(container.querySelector(".stat-card__delta")).toHaveAttribute("data-tone", "bad");
  });

  it("рост закрытых диалогов — зелёная дельта", () => {
    const { container } = renderWithProviders(
      <StatCard metricId="conversations_closed" title="Закрыто" value="290" deltaPct={8.2} />,
    );
    expect(container.querySelector(".stat-card__delta")).toHaveAttribute("data-tone", "good");
  });

  it("snapshot-карточка «Ждут ответа» — без дельты, с акцентом warning", () => {
    const { container } = renderWithProviders(
      <StatCard metricId="waiting_now" title="Ждут ответа" value="6" deltaPct={null} caption="сейчас ⚠" accent="warning" />,
    );

    expect(container.querySelector(".stat-card__delta")).toBeNull();
    expect(container.querySelector(".stat-card")).toHaveAttribute("data-accent", "warning");
    expect(screen.getByText("сейчас ⚠")).toBeInTheDocument();
  });
});
