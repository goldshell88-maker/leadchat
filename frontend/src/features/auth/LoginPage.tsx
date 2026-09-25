import { useEffect, useRef, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import {
  Alert,
  Anchor,
  Button,
  Checkbox,
  Paper,
  PasswordInput,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { ApiError, NETWORK_ERROR, TIMEOUT_ERROR } from "@/shared/api/http";
import { ExternalLink } from "@/shared/ui/ExternalLink";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { FullscreenLoader } from "@/shared/ui/FullscreenLoader";
import { LogoMark } from "@/shared/ui/LogoMark";
import { ForgotPasswordForm } from "./ForgotPasswordForm";
import { useDesktopRelease } from "./useDesktopRelease";
import { wasKickedOut } from "./sessionEnd";
import "./login.css";

type LoginErrorState =
  | { kind: "invalid" }
  | { kind: "locked"; until: number }
  | { kind: "forbidden"; message: string }
  | { kind: "network" }
  /** Сервер принял соединение и промолчал до потолка ожидания (ACC-06). */
  | { kind: "timeout" }
  /** `detail` — машинная часть (код ответа): её показываем мелко и отдельно. */
  | { kind: "other"; message: string; detail?: string };

function formatCountdown(totalSec: number): string {
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Экран входа (11 §1). Шесть состояний отказа, и у каждого есть ответ на
 * вопрос «что мне теперь делать»:
 *
 *   invalid   — проверить раскладку и Caps Lock, иначе просить новый пароль;
 *   locked    — подождать столько-то (счётчик тикает) или просить новый пароль;
 *   forbidden — учётную запись включает администратор, сам человек не может;
 *   network   — проверить интернет и повторить;
 *   timeout   — интернет ни при чём, сервер молчит: повторить и звать админа;
 *   other     — повторить, а если повторяется — показать администратору код.
 *
 * Отдельно — плашка «почему я здесь» (SHELL-03): человека могло снять с
 * рабочего экрана без единого слова.
 */
export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const user = useSessionStore((s) => s.user);
  const bootstrapped = useSessionStore((s) => s.bootstrapped);
  const login = useSessionStore((s) => s.login);

  const естьДесктоп = useDesktopRelease();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<LoginErrorState | null>(null);
  const [shaking, setShaking] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  // «Не помню пароль» (14 §2.2) — та же карточка, отдельный режим: заводить
  // ради одной формы маршрут и чанк незачем.
  const [forgot, setForgot] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);

  const fromState = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
  const from = fromState ?? "/chats";

  /*
   * ПОЧЕМУ ЧЕЛОВЕК ЗДЕСЬ (SHELL-03).
   *
   * Считается один раз при монтировании и намеренно НЕ пересчитывается: как
   * только человек введёт пароль, `hadSessionInThisTab()` останется true, а
   * плашка должна исчезнуть вместе с формой, а не мигать по дороге.
   */
  const [kicked] = useState(() => wasKickedOut(Boolean(fromState)));

  const lockedRemaining =
    error?.kind === "locked" ? Math.max(0, Math.ceil((error.until - now) / 1000)) : 0;
  const locked = error?.kind === "locked" && lockedRemaining > 0;

  // Countdown tick for the account_locked plate; the form unlocks itself at zero.
  useEffect(() => {
    if (error?.kind !== "locked") return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [error]);

  useEffect(() => {
    if (error?.kind === "locked" && lockedRemaining <= 0) setError(null);
  }, [error, lockedRemaining]);

  if (!bootstrapped) return <FullscreenLoader />;
  // Cookie survived the silent refresh — the user never sees the form (11 §1.2).
  if (user) return <Navigate to={from} replace />;

  const canSubmit = email.trim().length > 0 && password.length > 0 && !locked && !submitting;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password, remember);
      navigate(from, { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        switch (err.code) {
          case "invalid_credentials": {
            // Single generic text for both unknown email and wrong password (11 §1.3).
            setError({ kind: "invalid" });
            setPassword("");
            setShaking(true);
            window.setTimeout(() => setShaking(false), 400);
            passwordRef.current?.focus();
            break;
          }
          case "account_locked": {
            const retry = err.details?.["retry_after_sec"];
            const sec = typeof retry === "number" && retry > 0 ? retry : 15 * 60;
            setNow(Date.now());
            setError({ kind: "locked", until: Date.now() + sec * 1000 });
            break;
          }
          case "forbidden":
            setError({
              kind: "forbidden",
              message:
                err.message ||
                "Учётная запись отключена. Включить её может только администратор — напишите ему",
            });
            break;
          case NETWORK_ERROR:
            setError({ kind: "network" });
            break;
          case TIMEOUT_ERROR:
            /*
             * Молчащий сервер — не то же, что оборванный интернет, и говорить
             * надо разное. До появления потолка ожидания (ACC-06) кнопка
             * «Войти» в этом случае крутилась бесконечно: диспетчер в начале
             * смены смотрел на неё и не знал, ждать ему или звать
             * администратора. Теперь ждём двадцать секунд и говорим.
             */
            setError({ kind: "timeout" });
            break;
          default:
            /*
             * МАШИННЫЙ ТЕКСТ НА ЭКРАН НЕ ПУСКАЕМ (TEXT-21).
             *
             * `http.ts` собирает для неизвестного отказа строку «Запрос
             * завершился с кодом 502», и она доезжала сюда дословно — человек
             * на входе читал про код ответа. Код при этом выбрасывать нельзя:
             * администратору он нужен, чтобы понять, чинить сервер или сеть.
             * Поэтому человеку — что делать, а код — мелкой строкой под ним.
             */
            setError({
              kind: "other",
              message: "Войти не получилось. Попробуйте ещё раз через минуту",
              detail: err.status ? `Ответ сервера: ${err.status}` : err.code,
            });
        }
      } else {
        setError({ kind: "other", message: "Не получилось войти. Попробуйте ещё раз" });
      }
    } finally {
      setSubmitting(false);
    }
  };

  const fieldsDisabled = locked || submitting;
  const invalid = error?.kind === "invalid";

  return (
    <div className="login-screen">
      <Paper
        className="login-card"
        data-shake={shaking || undefined}
        p="var(--lc-space-6)"
        radius="var(--lc-radius-md)"
        shadow="sm"
        withBorder
      >
        <Stack gap="var(--lc-space-4)">
          <Stack gap="var(--lc-space-1)" align="center">
            <LogoMark size={40} />
            <Title order={1} fz="var(--lc-fz-page)" fw={600} ta="center" c="var(--lc-text-1)">
              LeadChat by Lead Partner
            </Title>
            <Text fz="sm" c="var(--lc-text-3)" ta="center">
              «Мы ремонтируем — Вы зарабатываете»
            </Text>
          </Stack>

          {forgot && <ForgotPasswordForm onBack={() => setForgot(false)} />}

          {/*
            «ПОЧЕМУ Я ЗДЕСЬ» — до формы и до ошибок: это про предыдущий экран,
            а не про то, что человек сейчас нажал (SHELL-03).

            Не `Alert`: он жёстко ставит `role="alert"` поверх любого другого,
            а это сообщение — не тревога, а пояснение. `alert` перебивает
            скринридер и, что важнее, спорит с настоящими ошибками формы,
            которые появятся ниже.
          */}
          {!forgot && kicked && !error && (
            <div className="login-note" role="status">
              <Text fz="sm" c="var(--lc-info-text)">
                Вход закончился — так бывает после долгого перерыва или когда администратор
                отключил учётную запись. Войдите ещё раз, вы вернётесь на ту же страницу.
              </Text>
            </div>
          )}

          {!forgot && (
            <>
              {error?.kind === "locked" && (
                <Alert color="yellow" variant="light" role="alert">
                  Слишком много попыток входа. Повторите через {formatCountdown(lockedRemaining)}.
                  Если пароль не вспоминается — «Не помню пароль» внизу, администратор выдаст новый
                </Alert>
              )}
              {error?.kind === "forbidden" && (
                <Alert color="red" variant="light" role="alert">
                  {error.message}
                </Alert>
              )}
              {error?.kind === "network" && (
                <Alert color="red" variant="light" role="alert">
                  Не получилось связаться с сервером. Проверьте интернет и нажмите «Войти» ещё раз
                </Alert>
              )}
              {/* Интернет тут ни при чём — про него нарочно ни слова: сервер
                  на связи, но не отвечает. Отправить человека дёргать роутер
                  значило бы отнять у него минуту и ничего не починить. */}
              {error?.kind === "timeout" && (
                <Alert color="red" variant="light" role="alert">
                  Сервер не ответил. Нажмите «Войти» ещё раз, а если повторится — покажите это
                  администратору
                </Alert>
              )}
              {error?.kind === "other" && (
                <Alert color="red" variant="light" role="alert">
                  {error.message}
                  {error.detail && (
                    <Text fz="xs" c="var(--lc-text-3)" mt={4}>
                      Если повторяется — покажите администратору: {error.detail}
                    </Text>
                  )}
                </Alert>
              )}

              <form onSubmit={handleSubmit} noValidate>
                <Stack gap="var(--lc-space-3)">
                  <TextInput
                    label="Email"
                    type="email"
                    autoComplete="email"
                    autoFocus
                    value={email}
                    onChange={(e) => setEmail(e.currentTarget.value)}
                    disabled={fieldsDisabled}
                    error={invalid || undefined}
                  />
                  <PasswordInput
                    label="Пароль"
                    autoComplete="current-password"
                    ref={passwordRef}
                    value={password}
                    onChange={(e) => setPassword(e.currentTarget.value)}
                    disabled={fieldsDisabled}
                    error={invalid || undefined}
                  />
                  <Checkbox
                    label="Запомнить меня"
                    checked={remember}
                    onChange={(e) => setRemember(e.currentTarget.checked)}
                    disabled={fieldsDisabled}
                  />
                  <Button
                    type="submit"
                    fullWidth
                    loading={submitting}
                    disabled={!canSubmit && !submitting}
                  >
                    Войти
                  </Button>
                  {/* Один текст на «неизвестный email» и «неверный пароль»
                      (11 §1.3) — подсказывать, что почта существует, нельзя.
                      Но что делать дальше, сказать можно и нужно. */}
                  {invalid && (
                    <div role="alert">
                      <Text fz="sm" c="var(--lc-danger)" ta="center">
                        Неверный email или пароль
                      </Text>
                      <Text fz="xs" c="var(--lc-text-3)" ta="center">
                        Проверьте раскладку и Caps Lock. Пароль не вспоминается — «Не помню пароль»
                      </Text>
                    </div>
                  )}
                  {/* Восстановление пароля идёт через администратора (14 §2.2):
                      ссылка заводит заявку, а не отправляет письмо. */}
                  <Anchor component="button" type="button" fz="sm" ta="center" onClick={() => setForgot(true)}>
                    Не помню пароль
                  </Anchor>
                </Stack>
              </form>
            </>
          )}
        </Stack>
      </Paper>

      <div className="login-footer">
        {/* Ссылка показывается, ТОЛЬКО когда инсталлятор действительно выложен
            (разбор — в useDesktopRelease). До первого релиза `/download` даёт
            404, и обещать скачивание перед входом нельзя. */}
        {естьДесктоп && (
          <>
            <span aria-hidden="true">🖥</span>
            <a href="/download">Скачать приложение для Windows</a>
            <span aria-hidden="true">·</span>
          </>
        )}
        <ExternalLink url="https://partner-lead-centre.ru">partner-lead-centre.ru</ExternalLink>
      </div>
    </div>
  );
}
