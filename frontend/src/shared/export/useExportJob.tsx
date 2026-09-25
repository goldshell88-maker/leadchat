import { useEffect, useState } from "react";
import { Anchor } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import type { ExportJob, ExportJobCreated } from "@/shared/api/types";

/**
 * Фоновая выгрузка (11 §6.3, контракт — 06 §4.5):
 * POST → 202 {job_id} → тост-прогресс → поллинг статуса раз в 2 с →
 * success-тост со ссылкой (живёт 24 ч) либо danger с текстом ошибки.
 * Мгновенные отказы: 409 «уже выполняется», 429 «лимит на сегодня».
 *
 * Общая для статистики и «Разбора диалогов» (24.09): у обеих кнопок один
 * слот и одна суточная квота на сервере, и вести себя им разумно одинаково.
 */

/** Имя файла из подписанной ссылки: /api/v1/media/exports/leadchat-stats_…xlsx?sig=… */
export function fileNameFromUrl(url: string): string {
  const path = url.split("?")[0];
  const name = path.slice(path.lastIndexOf("/") + 1);
  return name || "выгрузка";
}

/**
 * Текст отказа для человека. Сервер кладёт в `error` фразу, но воркер прошлой
 * версии писал код `internal`, и тост показывал его как есть (проверка 24.09):
 * машинный код заменяем общими словами.
 */
export function failureText(error: string | null | undefined): string {
  if (!error || /^[a-z_]+$/.test(error)) return "Не получилось собрать файл — повторите выгрузку позже";
  return error;
}

export function exportErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409 || err.code === "export_already_running") return "Предыдущий экспорт ещё готовится";
    if (err.status === 429) return "Лимит выгрузок на сегодня исчерпан";
    if (err.code === "period_too_long") return "Период больше 366 дней — сузьте диапазон";
    return err.message;
  }
  return "Не получилось запустить выгрузку";
}

export interface ExportJobSource<TRequest> {
  /** Id тоста: у каждой кнопки свой, чтобы две выгрузки не затирали друг друга. */
  toastId: string;
  create(body: TRequest): Promise<ExportJobCreated>;
  fetchJob(jobId: string): Promise<ExportJob>;
  jobKey(jobId: string): readonly unknown[];
}

export function useExportJob<TRequest>({ toastId: TOAST_ID, create, fetchJob, jobKey }: ExportJobSource<TRequest>) {
  const [jobId, setJobId] = useState<string | null>(null);

  const job = useQuery({
    queryKey: jobKey(jobId ?? ""),
    queryFn: () => fetchJob(jobId as string),
    enabled: Boolean(jobId),
    // Поллинг раз в 2 с — WebSocket тут не нужен, экспорт занимает секунды (06 §4.5).
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "done" || s === "failed" ? false : 2000;
    },
    staleTime: 0,
    gcTime: 0,
    retry: 1,
  });

  const start = useMutation<ExportJobCreated, Error, TRequest>({
    mutationFn: (body) => create(body),
    onSuccess: (data) => {
      setJobId(data.job_id);
      notifications.show({
        id: TOAST_ID,
        loading: true,
        title: "Готовим выгрузку…",
        message: "Файл появится здесь через несколько секунд",
        autoClose: false,
        // ⚠ КРЕСТИК ОБЯЗАТЕЛЕН (находка 23.08 №1). Без него тост со спиннером
        // нечем убрать: `autoClose` выключен намеренно (выгрузка длится дольше
        // любого разумного срока показа), и единственным способом закрыть его
        // была перезагрузка страницы.
        withCloseButton: true,
      });
    },
    onError: (err) => {
      notifications.show({ color: "red", title: "Выгрузка не запущена", message: exportErrorMessage(err) });
    },
  });

  /*
   * ⚠ УХОД С ЭКРАНА УБИРАЕТ ТОСТ ЗА СОБОЙ (находка 23.08 №1).
   *
   * Хук живёт внутри окна экспорта, а окно — внутри страницы статистики.
   * Ушёл человек в «Чаты» — размонтировались и окно, и хук: `jobId` исчез,
   * поллинг встал, и эффекты, которые превращают тост в «Выгрузка готова» или
   * «не удалась», больше не выполнятся НИКОГДА. Сам тост при этом живёт в
   * глобальном `<Notifications>` и переживает навигацию — то есть в углу
   * навсегда оставался спиннер «Готовим выгрузку…», который нечем закрыть.
   *
   * Снимаем его при размонтировании: обещание, которое некому выполнить, лучше
   * убрать, чем оставить висеть.
   */
  useEffect(() => () => {
    notifications.hide(TOAST_ID);
  }, [TOAST_ID]);

  const data: ExportJob | undefined = job.data;
  const status = data?.status;

  useEffect(() => {
    if (!jobId || !data) return;
    if (status === "done") {
      const url = data.url;
      notifications.update({
        id: TOAST_ID,
        loading: false,
        color: "lp",
        title: "Выгрузка готова",
        message: url ? (
          <Anchor href={url} download>
            Скачать {fileNameFromUrl(url)}
          </Anchor>
        ) : (
          "Файл готов"
        ),
        autoClose: 30_000,
        withCloseButton: true,
      });
      setJobId(null);
    } else if (status === "failed") {
      notifications.update({
        id: TOAST_ID,
        loading: false,
        color: "red",
        title: "Выгрузка не удалась",
        message: failureText(data.error),
        autoClose: 15_000,
        withCloseButton: true,
      });
      setJobId(null);
    }
  }, [jobId, data, status, TOAST_ID]);

  // Сеть отвалилась во время поллинга — не оставляем вечный спиннер в тосте.
  useEffect(() => {
    if (!jobId || !job.isError) return;
    notifications.update({
      id: TOAST_ID,
      loading: false,
      color: "red",
      title: "Статус выгрузки недоступен",
      message: "Проверьте соединение и запустите выгрузку заново",
      autoClose: 15_000,
      withCloseButton: true,
    });
    setJobId(null);
  }, [jobId, job.isError, TOAST_ID]);

  return {
    start: start.mutate,
    isStarting: start.isPending,
    /** Активный job: пока он есть, повторную выгрузку запускать нельзя (лимит 1). */
    running: Boolean(jobId),
  };
}
