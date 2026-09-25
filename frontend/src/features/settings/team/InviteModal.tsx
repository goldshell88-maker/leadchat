import { useEffect, useState, type FormEvent } from "react";
import { Alert, Button, Group, Modal, Radio, Stack, Text, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import type { Role } from "@/shared/auth/usePermissions";
import { qk } from "@/shared/api/queryKeys";
import type { InviteIssued } from "@/shared/api/types";
import { inviteUser } from "./api";
import { ROLE_HINTS, ROLE_LABELS, ROLE_ORDER } from "./roles";

/**
 * Приглашение сотрудника (11 §4.2, `POST /users` 01 §3.2): имя, email и роль
 * с описанием прав в одну строку. Почтового сервиса в MVP нет — результатом
 * работы модалки становится одноразовая ссылка, её показывает вызывающий экран.
 */
export function InviteModal({
  opened,
  onClose,
  onIssued,
}: {
  opened: boolean;
  onClose(): void;
  onIssued(result: InviteIssued): void;
}) {
  const qc = useQueryClient();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("manager");
  const [emailError, setEmailError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!opened) {
      setFullName("");
      setEmail("");
      setRole("manager");
      setEmailError(null);
      setFormError(null);
    }
  }, [opened]);

  const invite = useMutation({
    mutationFn: () => inviteUser({ email: email.trim(), full_name: fullName.trim(), role }),
    onSuccess: (result) => {
      void qc.invalidateQueries({ queryKey: qk.users.root });
      onIssued(result);
      onClose();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setEmailError("Сотрудник с таким email уже существует");
        return;
      }
      setFormError(
        err instanceof ApiError && err.message ? err.message : "Не получилось пригласить. Попробуйте ещё раз",
      );
    },
  });

  const canSubmit = fullName.trim().length > 0 && email.trim().length > 0 && !invite.isPending;

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setEmailError(null);
    setFormError(null);
    invite.mutate();
  };

  return (
    <Modal opened={opened} onClose={onClose} title="Пригласить сотрудника" centered>
      <form onSubmit={handleSubmit} noValidate>
        <Stack gap="var(--lc-space-3)">
          {formError && (
            <Alert color="red" variant="light" role="alert">
              {formError}
            </Alert>
          )}
          <TextInput
            label="Имя"
            value={fullName}
            autoFocus
            onChange={(e) => setFullName(e.currentTarget.value)}
            disabled={invite.isPending}
          />
          <TextInput
            label="Email"
            type="email"
            autoComplete="off"
            value={email}
            onChange={(e) => {
              setEmail(e.currentTarget.value);
              setEmailError(null);
            }}
            error={emailError}
            disabled={invite.isPending}
          />
          <Radio.Group label="Роль" value={role} onChange={(v) => setRole(v as Role)}>
            <Stack gap="var(--lc-space-2)" mt="var(--lc-space-2)">
              {ROLE_ORDER.map((r) => (
                <Radio
                  key={r}
                  value={r}
                  disabled={invite.isPending}
                  label={
                    <span>
                      {ROLE_LABELS[r]}
                      <Text component="span" fz="xs" c="var(--lc-text-3)" display="block">
                        {ROLE_HINTS[r]}
                      </Text>
                    </span>
                  }
                />
              ))}
            </Stack>
          </Radio.Group>
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={invite.isPending}>
              Отмена
            </Button>
            {/*
              «Создать приглашение», а не «Пригласить»: кнопка на панели, от
              которой открывается это окно, называется «Пригласить», и с уходом
              «плюса» из её подписи на экране оказались ДВЕ кнопки с одним
              именем — одна открывает окно, вторая отправляет форму. Поймал это
              тест: «найдено несколько элементов с ролью button и именем
              Пригласить». Для того, кто ходит по кнопкам голосом или таб-ом,
              они были неразличимы и до этой правки — просто никто не смотрел.

              Слово выбрано по тому, что кнопка ДЕЛАЕТ: приглашение здесь не
              уходит письмом, а создаётся одноразовой ссылкой, которую
              администратор копирует и передаёт сам.
            */}
            <Button type="submit" loading={invite.isPending} disabled={!canSubmit && !invite.isPending}>
              Создать приглашение
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
