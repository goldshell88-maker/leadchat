import { http } from "@/shared/api/http";
import type { TemplateScope } from "@/shared/api/queryKeys";
import type { TemplateDto, TemplateFolders, TemplateInput, TemplatesPage } from "@/shared/api/types";

/**
 * GET /templates (01 §7.1): scope=all — личные текущего пользователя + общие.
 *
 * `limit=200` — потолок, а не «все». Сервер возвращает `page.total`, и экран
 * обязан сравнить его с длиной списка: если записей больше, показанное — не вся
 * библиотека, а поиск по ней неполон (TPL-05).
 */
export const TEMPLATES_LIMIT = 200;

export function fetchTemplates(scope: TemplateScope): Promise<TemplatesPage> {
  return http.get<TemplatesPage>(`/templates?scope=${scope}&limit=${TEMPLATES_LIMIT}`);
}

/** GET /templates/folders (01 §7.2) — для селектов папок. */
export function fetchTemplateFolders(): Promise<TemplateFolders> {
  return http.get<TemplateFolders>("/templates/folders");
}

/** POST /templates (01 §7.3); shared:true требует templates:shared. */
export function createTemplate(input: TemplateInput): Promise<TemplateDto> {
  return http.post<TemplateDto>("/templates", input);
}

/** PATCH /templates/{id} (01 §7.4) — title/body/folder; область не меняется. */
export function updateTemplate(id: string, input: Omit<TemplateInput, "shared">): Promise<TemplateDto> {
  return http.patch<TemplateDto>(`/templates/${encodeURIComponent(id)}`, input);
}

/** DELETE /templates/{id} (01 §7.4). */
export function deleteTemplate(id: string): Promise<void> {
  return http.del<void>(`/templates/${encodeURIComponent(id)}`);
}
