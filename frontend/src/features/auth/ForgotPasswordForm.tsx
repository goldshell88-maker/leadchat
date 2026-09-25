import { useState, type FormEvent } from "react";
import { Alert, Button, Stack, Text, TextInput, Title } from "@mantine/core";
import { ApiError, NETWORK_ERROR } from "@/shared/api/http";
import { formatRetryAfter, retryAfterSec } from "@/shared/api/rateLimit";
import { requestPasswordReset } from "./api";

/**
 * «Не помню пароль» (14 §2.2). Восстановление пароля идёт через администратора,
 * и до сих пор сотруднику было нечем его попросить — форма создаёт заявку,
 * которая приходит админу в центр уведомлений.
 *
 * Ответ один и тот же всегда: «если такой сотрудник есть…». Ни успех, ни 404
 * не должны давать способ проверить, заведён ли email.
 *
 * Два исхода показываются честно, потому что оба означают «заявка НЕ ушла»:
 *  - обрыв связи — до сервера не доехали;
 *  - 429 — счётчик исчерпан, и заявку никто не принял. Молчать здесь опаснее
 *    всего: лимит на адрес — 20 в час, а в офисе адрес общий, так что упереться
 *    в него может человек, который сам ещё ни разу ничего не отправлял.
 * Существование учётки этим не выдаётся: оба счётчика (email и адрес) сервер
 * крутит ДО похода в базу, и 429 приходит одинаково на любой адрес.
 */

/** Один текст на все исходы, кроме сетевого сбоя и 429. */
export const RESET_REQUEST_SENT = "Если такой сотрудник есть, администратор получит заявку";

export function ForgotPasswordForm({ onBack }: { onBack(): void }) {
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const [networkError, setNetworkError] = useState(false);
  const [limitedFor, setLimitedFor] = useState<number | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting || email.trim().length === 0) return;
    setSubmitting(true);
    setNetworkError(false);
    setLimitedFor(null);
    try {
      await requestPasswordReset(email.trim());
      setSent(true);
    } catch (err) {
      if (err instanceof ApiError && err.code === NETWORK_ERROR) {
        setNetworkError(true);
        return;
      }
      const retry = retryAfterSec(err);
      // 429: заявку не приняли. Показываем срок и НЕ повторяем запрос сами —
      // повтор упрётся в тот же счётчик и только продлит окно.
      if (retry !== null) setLimitedFor(retry);
      // Любой другой ответ сервера (404, 409, 422) неотличим от успеха.
      else setSent(true);
    } finally {
      setSubmitting(false);
    }
  };

  if (sent) {
    return (
      <Stack gap="var(--lc-space-3)">
        {/* Информационная плашка — синяя, а не акцентная: акцент теперь
            зелёный и означает действие. */}
        <Alert color="info" variant="light" role="status">
          {RESET_REQUEST_SENT}
        </Alert>
        <Text fz="sm" c="var(--lc-text-2)">
          Администратор выдаст новую ссылку установки пароля и передаст её вам.
        </Text>
        <Button variant="default" fullWidth onClick={onBack}>
          Вернуться ко входу
        </Button>
      </Stack>
    );
  }

  return (
    <form onSubmit={handleSubmit} noValidate>
      <Stack gap="var(--lc-space-3)">
        <Title order={2} fz="var(--lc-fz-section)" fw={600} c="var(--lc-text-1)">
          Не помню пароль
        </Title>
        <Text fz="sm" c="var(--lc-text-2)">
          Оставьте email — заявка уйдёт администратору, он выдаст новую ссылку установки пароля.
        </Text>
        {networkError && (
          <Alert color="red" variant="light" role="alert">
            Сервер недоступен — заявка не отправлена. Проверьте соединение
          </Alert>
        )}
        {limitedFor !== null && (
          <Alert color="yellow" variant="light" role="alert">
            Слишком много заявок — эта не отправлена. Попробуйте {formatRetryAfter(limitedFor)}
          </Alert>
        )}
        <TextInput
          label="Email"
          type="email"
          autoComplete="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.currentTarget.value)}
          disabled={submitting}
        />
        <Button type="submit" fullWidth loading={submitting} disabled={email.trim().length === 0}>
          Отправить заявку
        </Button>
        <Button variant="subtle" fullWidth onClick={onBack} disabled={submitting}>
          Вернуться ко входу
        </Button>
      </Stack>
    </form>
  );
}
