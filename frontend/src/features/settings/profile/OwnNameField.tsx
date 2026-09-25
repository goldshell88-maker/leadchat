import { useEffect, useRef, useState } from "react";
import { TextInput } from "@mantine/core";
import { useMutation } from "@tanstack/react-query";
import { ApiError, http } from "@/shared/api/http";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { showToast } from "@/shared/ui/toast";

/**
 * Своё имя — правкой на месте, как имя клиента (проверка 24.09).
 *
 * Ручка `PATCH /auth/me` есть с 07.08 («человек, которого завели как
 * „Сотрудник 3“, должен уметь исправить это сам»), а на экране имя было
 * обычным текстом: исправить опечатку в подписи, которую видят клиенты, мог
 * только администратор.
 *
 * Жесты те же, что у имени клиента: нажатие на имя открывает поле, Enter и
 * уход из поля сохраняют, Escape отменяет. Пустое имя сервер не примет — его
 * текст показывается под полем, а поле остаётся открытым.
 */
export function OwnNameField({ name }: { name: string }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(name);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** Escape закрыл поле — уход фокуса после него сохранять не должен. */
  const cancelled = useRef(false);

  const save = useMutation({
    mutationFn: (fullName: string) =>
      http.patch<{ full_name: string }>("/auth/me", { full_name: fullName }),
    onSuccess: ({ full_name }) => {
      // Имя живёт в сторе сессии: шапка приложения и подписи читают его оттуда.
      useSessionStore.setState((s) => (s.user ? { user: { ...s.user, full_name } } : {}));
      setEditing(false);
      showToast({ message: "Имя сохранено", color: "lp" });
    },
    onError: (e) =>
      setError(e instanceof ApiError ? e.message : "Не получилось сохранить. Попробуйте ещё раз"),
  });

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const submit = () => {
    if (value.trim() === name.trim()) {
      setEditing(false); // ничего не меняли — молча закрываем
      return;
    }
    save.mutate(value.trim());
  };

  if (!editing) {
    return (
      <button
        type="button"
        className="prof-hero__name prof-hero__name-trigger"
        title={`${name} — нажмите, чтобы изменить`}
        aria-label={`Изменить своё имя: ${name}`}
        onClick={() => {
          setValue(name);
          setError(null);
          cancelled.current = false;
          setEditing(true);
        }}
      >
        {name}
      </button>
    );
  }

  return (
    <TextInput
      ref={inputRef}
      className="prof-hero__name-edit"
      value={value}
      error={error}
      disabled={save.isPending}
      aria-label="Своё имя"
      maxLength={120}
      onChange={(e) => {
        setValue(e.currentTarget.value);
        setError(null);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submit();
        }
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          cancelled.current = true;
          setEditing(false);
        }
      }}
      onBlur={() => {
        if (cancelled.current) {
          cancelled.current = false;
          return;
        }
        submit();
      }}
    />
  );
}
