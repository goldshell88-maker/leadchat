import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Paper,
  PasswordInput,
  Progress,
  Skeleton,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { LogoMark } from "@/shared/ui/LogoMark";
import { acceptInvite, getInvite } from "./api";
import "./login.css";

const MIN_PASSWORD_LENGTH = 10; // API limit, 01 §2.4

/** Strength scale is a hint, never a blocker (11 §1.4). */
function passwordStrength(p: string): { value: number; label: string; color: string } {
  let score = Math.min(50, p.length * 5);
  if (/[a-zа-яё]/.test(p) && /[A-ZА-ЯЁ]/.test(p)) score += 15;
  if (/\d/.test(p)) score += 15;
  if (/[^a-zA-Zа-яА-ЯёЁ0-9\s]/.test(p)) score += 20;
  if (score < 50) return { value: score, label: "слабый", color: "red" };
  if (score < 75) return { value: score, label: "средний", color: "yellow" };
  return { value: score, label: "надёжный", color: "lp" };
}

function InviteCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="login-screen">
      <Paper
        className="login-card"
        p="var(--lc-space-6)"
        radius="var(--lc-radius-md)"
        shadow="sm"
        withBorder
      >
        {children}
      </Paper>
    </div>
  );
}

function ExpiredView() {
  return (
    <InviteCard>
      <Stack gap="var(--lc-space-4)" align="center">
        <LogoMark size={40} />
        <Title order={1} fz="var(--lc-fz-page)" fw={600} ta="center" c="var(--lc-text-1)">
          Ссылка устарела
        </Title>
        <Text fz="sm" c="var(--lc-text-2)" ta="center">
          Ссылка недействительна, устарела или уже использована. Запросите новую у администратора
        </Text>
        <Button component={Link} to="/login" variant="outline" fullWidth>
          Перейти ко входу
        </Button>
      </Stack>
    </InviteCard>
  );
}

/** Set-password page for the invite flow — /invite/:token (11 §1.4). */
export function InvitePage() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const applySession = useSessionStore((s) => s.applySession);

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);

  const inviteQuery = useQuery({
    queryKey: qk.invite(token),
    queryFn: () => getInvite(token),
    retry: false,
    staleTime: Infinity,
  });

  const accept = useMutation({
    mutationFn: () => acceptInvite(token, password),
    onSuccess: async (data) => {
      // Token body equals POST /auth/login — the user is logged in right away.
      await applySession(data);
      navigate("/chats", { replace: true });
    },
  });

  const isExpired = (e: unknown) =>
    e instanceof ApiError && (e.code === "invite_expired" || e.status === 404 || e.status === 410);

  if (inviteQuery.isError && isExpired(inviteQuery.error)) return <ExpiredView />;
  if (accept.isError && isExpired(accept.error)) return <ExpiredView />;

  if (inviteQuery.isPending) {
    return (
      <InviteCard>
        <Stack gap="var(--lc-space-3)">
          <Skeleton height={40} width={40} radius="var(--lc-radius-sm)" />
          <Skeleton height={24} width="70%" />
          <Skeleton height={36} />
          <Skeleton height={36} />
          <Skeleton height={36} />
        </Stack>
      </InviteCard>
    );
  }

  if (inviteQuery.isError) {
    return (
      <InviteCard>
        <Stack gap="var(--lc-space-4)" align="center">
          <Alert color="red" variant="light" w="100%">
            Не получилось загрузить приглашение
          </Alert>
          <Button variant="outline" onClick={() => void inviteQuery.refetch()}>
            Повторить
          </Button>
        </Stack>
      </InviteCard>
    );
  }

  const invite = inviteQuery.data;
  const strength = passwordStrength(password);
  const tooShort = password.length > 0 && password.length < MIN_PASSWORD_LENGTH;
  const mismatch = confirm.length > 0 && confirm !== password;
  const canSubmit =
    password.length >= MIN_PASSWORD_LENGTH && confirm === password && !accept.isPending;

  const firstName = invite.full_name.trim().split(/\s+/)[0] || invite.full_name;

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setFieldError(null);
    accept.mutate(undefined, {
      onError: (err) => {
        if (err instanceof ApiError && err.code === "validation_error") {
          setFieldError(err.message || "Пароль не подходит: минимум 10 символов");
        }
      },
    });
  };

  const acceptFailedOther =
    accept.isError &&
    !isExpired(accept.error) &&
    !(accept.error instanceof ApiError && accept.error.code === "validation_error");

  return (
    <InviteCard>
      <Stack gap="var(--lc-space-4)">
        <Stack gap="var(--lc-space-1)" align="center">
          <LogoMark size={40} />
          <Title order={1} fz="var(--lc-fz-page)" fw={600} ta="center" c="var(--lc-text-1)">
            Здравствуйте, {firstName}!
          </Title>
          <Text fz="sm" c="var(--lc-text-2)" ta="center">
            Придумайте пароль для входа в LeadChat
          </Text>
        </Stack>

        {acceptFailedOther && (
          <Alert color="red" variant="light" role="alert">
            {accept.error instanceof ApiError
              ? accept.error.message
              : "Не получилось сохранить пароль. Попробуйте ещё раз"}
          </Alert>
        )}

        <form onSubmit={handleSubmit} noValidate>
          <Stack gap="var(--lc-space-3)">
            <TextInput label="Email" value={invite.email} readOnly disabled />
            <PasswordInput
              label={`Пароль (мин. ${MIN_PASSWORD_LENGTH} символов)`}
              autoComplete="new-password"
              autoFocus
              value={password}
              onChange={(e) => {
                setPassword(e.currentTarget.value);
                setFieldError(null);
              }}
              error={fieldError ?? (tooShort ? `Минимум ${MIN_PASSWORD_LENGTH} символов` : undefined)}
            />
            {password.length > 0 && (
              <div>
                <Progress value={strength.value} color={strength.color} size="xs" />
                <Text fz="xs" c="var(--lc-text-3)" mt={4}>
                  {strength.label}
                </Text>
              </div>
            )}
            <PasswordInput
              label="Повторите пароль"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.currentTarget.value)}
              error={mismatch ? "Пароли не совпадают" : undefined}
            />
            <Button type="submit" fullWidth loading={accept.isPending} disabled={!canSubmit && !accept.isPending}>
              Сохранить и войти
            </Button>
          </Stack>
        </form>
      </Stack>
    </InviteCard>
  );
}
