import type { ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { preloadOnHover, type LazyRoutePath } from "@/app/lazyRoutes";
import { usePermissions } from "@/shared/auth/usePermissions";
import "./settings.css";

/**
 * Пункт меню. Все разделы настроек ленивые, поэтому `to` — путь из карты
 * чанков: пункт предзагружает свой раздел по наведению и фокусу
 * (`lazyRoutes.ts`), а класс `pending` на время загрузки вешает сам `NavLink`
 * (nav-pending.css). Один компонент на девять пунктов — чтобы правило «пункт
 * греет свой чанк» нельзя было забыть у десятого.
 */
function Item({ to, end, children }: { to: LazyRoutePath; end?: boolean; children: ReactNode }) {
  return (
    <NavLink to={to} className="settings-nav__item" end={end} {...preloadOnHover(to)}>
      {children}
    </NavLink>
  );
}

/**
 * Каркас `/settings/*` (11 §4): вторая NAV-колонка, наполнение — по правам.
 * Прямой заход без права ловит guard роутера (03 §6) — здесь только пункты.
 */
export function SettingsLayout() {
  const { can, canAny } = usePermissions();

  return (
    /*
      ⚠ ЛИШНИЙ УЗЕЛ, И ОН НУЖЕН РОВНО ДЛЯ ОДНОГО: ЕМУ МЕРЯЮТ МЕСТО.

      Порог, на котором колонка разделов ложится строкой, считается по ширине,
      достающейся РАЗДЕЛУ, а не по ширине окна: рельса приложения забирает
      218 px и развёрнута по умолчанию. Меряет её контейнерный запрос
      (settings.css, `@container settings-frame`), а `@container` правит
      потомков контейнера — значит контейнером обязан быть узел СНАРУЖИ
      `.settings-page`, которой и меняют направление. Раскладка от этого не
      меняется: обёртка — тот же флекс-столбец в одну строку.
    */
    <div className="settings-frame">
      <div className="settings-page">
        <nav className="settings-nav" aria-label="Разделы настроек">
          {can("accounts:read") && (
            <Item to="/settings/accounts" end>
              Аккаунты Авито
            </Item>
          )}
          {/* Отдельным пунктом, а не вкладкой внутри аккаунтов: это ответ на
              другой вопрос — «куда подключён человек», тогда как список каналов
              отвечает «кто на этом канале». Условие то же, что у маршрута. */}
          {can("accounts:read") && (
            <Item to="/settings/accounts/operators">
              Люди и каналы
            </Item>
          )}
          {/* ⚠ ЛЮБОЕ ИЗ ДВУХ ПРАВ, И ТО ЖЕ САМОЕ У МАРШРУТА (разбор 03.09).
              Пункт закрывался только `templates:shared`, и диспетчер не видел
              раздела с названием того, чем пользуется каждый день. Условие
              обязано совпадать с `RequirePermission` в router.tsx: разойдись
              они — пункт вёл бы на отказ или маршрут остался бы без входа. */}
          {(can("templates:own") || can("templates:shared")) && (
            <Item to="/settings/templates">
              Быстрые ответы
            </Item>
          )}
          {can("bots:manage") && (
            <>
              <Item to="/settings/bots">
                Боты
              </Item>
              {/* Лид-бот — ОТДЕЛЬНАЯ система, а не один из ботов (решение
                  владельца от 12 августа). Стоит рядом с «Ботами», потому что оба
                  про автоматику, но своей строкой: у него свой сервер, свой
                  регламент, свои цены и своя цена вызова. Право то же —
                  `bots:manage`: здесь включается автоответ живым клиентам. */}
              <Item to="/settings/leadbot">
                Лид-бот
              </Item>
            </>
          )}
          {can("settings:manage") && (
            <>
              {/* Автозаявки — про то, что происходит ПОСЛЕ разговора: диалог с
                  итогом «Выезд» уходит заявкой в лид-центр. Право то же, что у
                  маршрута (`settings:manage`): пункт меню, ведущий в отказ, —
                  это обещание, которого экран не выполнит. */}
              <Item to="/settings/leads">
                Автозаявки
              </Item>
              <Item to="/settings/distribution">
                Распределение
              </Item>
              {/* Монитор внешних сервисов: кто подключён, потолки, расход,
                  проверка доступности; свои записи владельца. */}
              <Item to="/settings/apis">
                Внешние сервисы
              </Item>
            </>
          )}
          {canAny("users:manage", "audit:read") && (
            <Item to="/settings/team">
              Команда
            </Item>
          )}
          <Item to="/settings/profile">
            Профиль
          </Item>
        </nav>
        <div className="settings-content">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
