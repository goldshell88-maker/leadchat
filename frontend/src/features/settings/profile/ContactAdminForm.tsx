import { useState, type FormEvent } from "react";
import { Alert, Button, Group, Text, Textarea, TextInput, Title } from "@mantine/core";
import { ApiError, NETWORK_ERROR } from "@/shared/api/http";
import { formatRetryAfter, retryAfterSec } from "@/shared/api/rateLimit";
import { plural } from "@/shared/lib/plural";
import { sendAdminMessage } from "./api";

/**
 * «Написать администратору» (14 §2.2, второй сценарий связи с администратором).
 *
 * До сих пор профиль умел только советовать «напишите ему» — куда именно,
 * человек догадывался сам. Форма кладёт сообщение в центр уведомлений
 * администратора: тот же путь, что у заявки «не помню пароль», только изнутри
 * приложения и от известного отправителя.
 *
 * Прав не спрашиваем: сервер пускает сюда ЛЮБУЮ авторизованную роль, включая
 * наблюдателя — право писать администратору не выдаётся, оно есть у всех.
 */

/** Границы сервера (AdminMessageIn) — повторены здесь, чтобы 422 не ловить формой. */
export const SUBJECT_MIN = 3;
export const SUBJECT_MAX = 200;
export const TEXT_MAX = 2000;

export const MESSAGE_SENT = "Сообщение отправлено администратору";

export function ContactAdminForm() {
  const [subject, setSubject] = useState("");
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const [networkError, setNetworkError] = useState(false);
  const [limitedFor, setLimitedFor] = useState<number | null>(null);

  const trimmedSubject = subject.trim();
  const trimmedText = text.trim();
  const ready = trimmedSubject.length >= SUBJECT_MIN && trimmedText.length > 0;

  /*
   * ПОДТВЕРЖДЕНИЕ ГАСНЕТ, КАК ТОЛЬКО ЧЕЛОВЕК НАЧАЛ ПИСАТЬ СНОВА (PROF-05).
   *
   * `sent` сбрасывался только в начале СЛЕДУЮЩЕЙ отправки. То есть человек
   * отправлял письмо, набирал второе — и всё это время над формой висело
   * зелёное «Сообщение отправлено администратору». Отправив второе, он не
   * получал НИКАКОГО нового признака: плашка та же самая, она и не гасла. Два
   * письма подряд — обычное дело («забыл добавить»), и подтверждение, которое
   * нельзя отличить от прошлого, не подтверждает ничего.
   */
  const startEditing = () => {
    if (sent) setSent(false);
  };

  /*
   * ПОЧЕМУ ТРЕБОВАНИЕ К ТЕМЕ НАПИСАНО, А НЕ СПРЯТАНО В КНОПКЕ (PROF-04).
   *
   * Кнопка гасла, пока тема короче трёх знаков, и об этом не говорила ни
   * подпись поля, ни подсказка, ни ошибка. Тема «ЗП» — ровно то, что человек
   * напишет про зарплату; он набирает её, пишет сообщение, жмёт «Отправить», а
   * кнопка мёртвая, и почему — узнать неоткуда. Требование серверное
   * (`AdminMessageIn`), обойти его нельзя, значит надо назвать заранее.
   *
   * Пока поле пустое, показываем спокойную подсказку под полем; как только в
   * нём что-то есть, но мало, — она же становится ошибкой рядом с полем.
   */
  /*
   * ⚠ «ЧТО-ТО ЕСТЬ» СЧИТАЕТСЯ ПО ВВЕДЁННОМУ, А ДЛИНА — ПО ОБРЕЗАННОМУ.
   * Найдено проверкой боя 14 августа: пять пробелов в «Теме» гасили кнопку и
   * НЕ показывали ошибку. Оба слагаемых брались от обрезанной строки, а у неё
   * длина ноль — то есть поле считалось пустым, хотя человек в него печатал.
   * Он видит набранное, видит мёртвую кнопку и не видит причины.
   */
  const subjectTooShort = subject.length > 0 && trimmedSubject.length < SUBJECT_MIN;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting || !ready) return;
    setSubmitting(true);
    setNetworkError(false);
    setLimitedFor(null);
    setSent(false);
    try {
      await sendAdminMessage(trimmedSubject, trimmedText);
      setSent(true);
      setSubject("");
      setText("");
    } catch (err) {
      if (err instanceof ApiError && err.code === NETWORK_ERROR) {
        setNetworkError(true);
        return;
      }
      // 429 (5 сообщений в час): показываем срок и НЕ повторяем сами. Текст
      // сотрудника при этом остаётся в полях — переписывать его заново незачем.
      const retry = retryAfterSec(err);
      if (retry !== null) {
        setLimitedFor(retry);
        return;
      }
      setNetworkError(true);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="lc-card prof-card" onSubmit={handleSubmit} noValidate>
      <div className="prof-card__head">
        <Title order={2} fz="var(--lc-fz-section)" c="var(--lc-text-1)">
          Написать администратору
        </Title>
        {/* Куда уходит письмо — сказано прямо в шапке карточки. Раньше это
            знание жило только в комментарии к коду, и человек отправлял в
            пустоту: «ушло — а куда?» */}
        <Text component="p" fz="sm" c="var(--lc-text-3)">
          Письмо попадёт в центр уведомлений администратора — от вас, с именем
        </Text>
      </div>
      {/* Информационная плашка — синяя, а не акцентная: акцент теперь зелёный
          и означает действие. */}
      {sent && (
        <Alert color="info" variant="light" role="status">
          {MESSAGE_SENT}
        </Alert>
      )}
      {networkError && (
        <Alert color="red" variant="light" role="alert">
          Сообщение не отправлено. Проверьте соединение и попробуйте ещё раз
        </Alert>
      )}
      {limitedFor !== null && (
        <Alert color="yellow" variant="light" role="alert">
          Слишком много сообщений — это не отправлено. Попробуйте {formatRetryAfter(limitedFor)}
        </Alert>
      )}
      <TextInput
        label="Тема"
        placeholder="Коротко: о чём вопрос"
        description={`Не короче ${SUBJECT_MIN} знаков — по теме администратор поймёт, срочное это или нет`}
        error={
          subjectTooShort
            ? `Слишком коротко: нужно хотя бы ${SUBJECT_MIN} ${plural(SUBJECT_MIN, "знак", "знака", "знаков")}`
            : undefined
        }
        maxLength={SUBJECT_MAX}
        value={subject}
        onChange={(e) => {
          startEditing();
          setSubject(e.currentTarget.value);
        }}
        disabled={submitting}
      />
      <Textarea
        label="Сообщение"
        placeholder="Что случилось и что нужно"
        autosize
        minRows={3}
        maxRows={10}
        maxLength={TEXT_MAX}
        value={text}
        onChange={(e) => {
          startEditing();
          setText(e.currentTarget.value);
        }}
        disabled={submitting}
      />
      <Group justify="flex-end">
        <Button type="submit" loading={submitting} disabled={!ready}>
          Отправить
        </Button>
      </Group>
    </form>
  );
}
