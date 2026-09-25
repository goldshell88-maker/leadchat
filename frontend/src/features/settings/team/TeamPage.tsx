import { useState } from "react";
import { usePermissions, type Permission } from "@/shared/auth/usePermissions";
import { AuditLogTab } from "./AuditLogTab";
import { TeamMembersTab } from "./TeamMembersTab";
import "./team.css";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";

/**
 * `/settings/team` (11 §4.2). Guard роута — `anyOf ["users:manage","audit:read"]`,
 * то есть admin и head; M/O сюда не попадают.
 *
 * Вкладки строятся по правам: head видит ТОЛЬКО «Журнал аудита» — управление
 * людьми (`users:manage`) остаётся за админом (DESIGN §5.1: «Сотрудники и роли»
 * есть только у админа).
 */
interface TeamTab {
  id: string;
  label: string;
  permission: Permission;
  render(): React.ReactNode;
}

const TABS: TeamTab[] = [
  {
    id: "members",
    label: "Сотрудники",
    permission: "users:manage",
    render: () => <TeamMembersTab />,
  },
  {
    id: "audit",
    label: "Журнал аудита",
    permission: "audit:read",
    render: () => <AuditLogTab />,
  },
];

export function TeamPage() {
  const { can } = usePermissions();
  const visible = TABS.filter((t) => can(t.permission));
  const [activeId, setActiveId] = useState<string>(visible[0]?.id ?? "");
  const active = visible.find((t) => t.id === activeId) ?? visible[0];

  return (
    <section className="settings-section">
      <PageHeader title="Команда" description="Сотрудники, их роли и журнал действий" />

      {visible.length === 0 ? (
        /* Строку не меняем: на неё ссылается тест прав (AuditLog.test.tsx). */
        <EmptyState illustration="archive" title="Для вашей роли здесь пока нет разделов" />
      ) : (
        <>
          <div className="lc-tabs" role="tablist" aria-label="Разделы команды">
            {visible.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                className="lc-tabs__tab"
                aria-selected={active?.id === t.id}
                data-active={active?.id === t.id || undefined}
                onClick={() => setActiveId(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
          {active?.render()}
        </>
      )}
    </section>
  );
}
