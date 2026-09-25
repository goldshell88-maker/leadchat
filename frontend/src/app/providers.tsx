import type { ReactNode } from "react";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./queryClient";
import { cssVariablesResolver, theme } from "./theme";

/**
 * Тёмная тема — по умолчанию (бриф: «Dark Theme First»). Светлая остаётся
 * доступной и переключается в профиле; выбор сотрудника Mantine хранит сам и
 * при следующем входе умолчание уже не применяет.
 *
 * Тосты — правый ВЕРХНИЙ угол и 4 секунды (бриф). Нижний правый угол в
 * рабочем месте оператора приходится ровно на композер: уведомление
 * закрывало поле ввода в тот момент, когда человек в него печатает.
 */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider
        theme={theme}
        defaultColorScheme="dark"
        cssVariablesResolver={cssVariablesResolver}
      >
        <Notifications position="top-right" autoClose={4000} limit={4} />
        {children}
      </MantineProvider>
    </QueryClientProvider>
  );
}
