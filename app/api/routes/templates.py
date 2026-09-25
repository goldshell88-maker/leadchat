"""Templates — быстрые ответы композера (01 §7).

Модель одна (`templates`): `owner_id IS NULL` — общий шаблон, иначе личный.
Права (01 §12): `templates:own` — admin/head/manager (личные + чтение),
`templates:shared` — admin/head (общие). Observer шаблонов не видит вовсе:
`require_permission("templates:own")` отдаёт ему 403 на каждой ручке.

Видимость: свои личные + все общие. Чужой личный шаблон не существует для
запрашивающего — 404, а не 403 (не раскрываем факт наличия, 01 §7.4).

Переменные `{имя}` / `{менеджер}` / `{объявление}` подставляет ФРОНТ при вставке
в поле ввода; бэкенд хранит тело как есть (01 §7) — никакого рендеринга здесь.
"""

import uuid
from typing import Any, cast

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_permission
from app.core.errors import ApiError
from app.core.rbac import ROLE_PERMISSIONS
from app.models import Template, User
from app.services.audit import write_audit

router = APIRouter()
log = structlog.get_logger("app.templates")

own_perm = require_permission("templates:own")

MAX_TITLE = 200
MAX_BODY = 4000  # тот же предел здравого смысла, что у текста сообщения (01 §6.2)
MAX_FOLDER = 100


# --- схемы -------------------------------------------------------------------


class TemplateOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID | None  # null — общий шаблон
    title: str
    body: str
    folder: str | None
    #: Сколько раз применили. Подсказка ставит ходовые выше — просьба
    #: владельца 29.08: «сделай популярность ответов».
    used_count: int = 0


class PageOut(BaseModel):
    limit: int
    offset: int
    total: int


class TemplatesPageOut(BaseModel):
    items: list[TemplateOut]
    page: PageOut


class FoldersOut(BaseModel):
    shared: list[str]
    personal: list[str]


def _clean(value: str | None) -> str | None:
    """Пустая/пробельная папка эквивалентна её отсутствию."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class TemplateCreate(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    body: str = Field(min_length=1, max_length=MAX_BODY)
    folder: str | None = Field(default=None, max_length=MAX_FOLDER)
    shared: bool = False

    @field_validator("title", "body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Поле не может быть пустым")
        return v.strip()


class TemplateUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY)
    folder: str | None = Field(default=None, max_length=MAX_FOLDER)

    @field_validator("title", "body")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("Поле не может быть пустым")
        return v.strip() if v is not None else None


def _out(tpl: Template) -> TemplateOut:
    return TemplateOut(
        id=tpl.id,
        owner_id=tpl.owner_id,
        title=tpl.title,
        body=tpl.body,
        folder=tpl.folder,
        used_count=tpl.used_count or 0,
    )


# --- доступ ------------------------------------------------------------------


def _can_share(user: User) -> bool:
    return "templates:shared" in ROLE_PERMISSIONS.get(user.role, frozenset())


def _visible(user: User) -> sa.ColumnElement[bool]:
    """Свои личные + все общие (01 §7.1)."""
    return sa.or_(Template.owner_id.is_(None), Template.owner_id == user.id)


async def _get_writable(db: AsyncSession, template_id: uuid.UUID, user: User) -> Template:
    """Шаблон под изменение/удаление с проверкой правил 01 §7.4."""
    tpl = await db.get(Template, template_id)
    if tpl is None:
        raise ApiError("not_found", "Шаблон не найден", status=404)
    if tpl.owner_id is None:  # общий — только templates:shared
        if not _can_share(user):
            raise ApiError(
                "forbidden",
                "Общие шаблоны меняет только администратор или руководитель",
                status=403,
            )
        return tpl
    # личный: владелец или admin; чужой личный — 404, факт наличия не раскрываем
    if tpl.owner_id != user.id and user.role != "admin":
        raise ApiError("not_found", "Шаблон не найден", status=404)
    return tpl


# --- ручки -------------------------------------------------------------------


@router.get("/templates/folders", response_model=FoldersOut)
async def list_template_folders(
    user: User = Depends(own_perm),
    db: AsyncSession = Depends(get_db),
) -> FoldersOut:
    """SELECT DISTINCT folder по обеим областям — для выпадающих списков (01 §7.2)."""

    async def _folders(cond: sa.ColumnElement[bool]) -> list[str]:
        rows = await db.execute(
            select(Template.folder)
            .where(cond, Template.folder.is_not(None))
            .distinct()
            .order_by(Template.folder)
        )
        return [f for f in rows.scalars() if f]

    return FoldersOut(
        shared=await _folders(Template.owner_id.is_(None)),
        personal=await _folders(Template.owner_id == user.id),
    )


@router.get("/templates", response_model=TemplatesPageOut)
async def list_templates(
    scope: str = Query("all", pattern="^(all|shared|personal)$"),
    folder: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(own_perm),
    db: AsyncSession = Depends(get_db),
) -> TemplatesPageOut:
    conds: list[sa.ColumnElement[bool]] = [_visible(user)]
    if scope == "shared":
        conds.append(Template.owner_id.is_(None))
    elif scope == "personal":
        conds.append(Template.owner_id == user.id)
    if folder is not None:
        cleaned = _clean(folder)
        conds.append(Template.folder.is_(None) if cleaned is None else Template.folder == cleaned)
    if q:
        like = f"%{q.strip()}%"
        conds.append(sa.or_(Template.title.ilike(like), Template.body.ilike(like)))

    total = (
        await db.execute(select(sa.func.count()).select_from(Template).where(*conds))
    ).scalar_one()
    rows = (
        await db.execute(
            select(Template)
            .where(*conds)
            .order_by(
                Template.folder.asc().nulls_last(),  # папки первыми, «без папки» — в хвосте
                Template.title.asc(),
                Template.id,  # тай-брейк: стабильная пагинация при одинаковых title
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return TemplatesPageOut(
        items=[_out(t) for t in rows],
        page=PageOut(limit=limit, offset=offset, total=total),
    )


@router.post("/templates/{template_id}/used", status_code=204)
async def mark_template_used(
    template_id: uuid.UUID,
    user: User = Depends(own_perm),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Заготовку применили — она поднимается в подсказке.

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 29.08: «сделай популярность ответов, когда пишешь для
    быстрых ответов». Заготовок 39 общих плюс личные, а подсказка показывает
    пять: какие пять — вопрос не вкуса, и отвечать на него должен живой сигнал.

    ⚠ СЧЁТЧИК ДВИГАЕТСЯ ЗАПРОСОМ В БАЗУ, А НЕ ЧТЕНИЕМ-ЗАПИСЬЮ. Тринадцать
    диспетчеров применяют одни и те же общие заготовки одновременно; прочитай мы
    значение и запиши обратно, часть нажатий потерялась бы молча. `UPDATE ... +1`
    считает сама база.

    ⚠ ПРАВО — ТО ЖЕ, ЧТО У ЧТЕНИЯ СПИСКА (`templates:own`), А НЕ ПРАВО НА
    ОБЩИЕ. Применить общую заготовку может каждый, кто её видит; менять её
    текст — только тот, кому можно (`templates:shared`). Потребуй мы здесь
    второе, счётчик у общих не двигался бы ни у кого, кроме администратора и
    руководителя, — то есть популярность считалась бы по двоим из тринадцати.

    Ответ пустой: интерфейс шлёт это «вдогонку» и результата не ждёт. Отказ
    ничего не ломает — заготовка вставлена, а порядок в подсказке подождёт
    следующего применения.
    """
    updated = await db.execute(
        sa.update(Template)
        .where(Template.id == template_id, _visible(user))
        .values(used_count=Template.used_count + 1)
    )
    # rowcount живёт на CursorResult; типизированный Result его не обещает —
    # тот же приём, что в `cli.py` и `avito_accounts.py`.
    if cast("sa.CursorResult[Any]", updated).rowcount == 0:
        # Чужая личная заготовка или удалённая: молчим тем же 404, что и
        # остальные ручки, — существование чужих заготовок не наше дело.
        raise ApiError("not_found", "Шаблон не найден", status=404)
    await db.commit()


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateCreate,
    user: User = Depends(own_perm),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    if body.shared and not _can_share(user):  # manager не заводит общие (01 §7.3)
        raise ApiError(
            "forbidden",
            "Создавать общие шаблоны может только администратор или руководитель",
            status=403,
        )
    tpl = Template(
        id=uuid.uuid4(),
        owner_id=None if body.shared else user.id,
        title=body.title,
        body=body.body,
        folder=_clean(body.folder),
    )
    db.add(tpl)
    await write_audit(
        db,
        user_id=user.id,
        action="template.created",
        entity="template",
        entity_id=str(tpl.id),
        details={"shared": body.shared, "folder": tpl.folder},
    )
    await db.commit()
    log.info("template.created", template_id=str(tpl.id), shared=body.shared)
    return _out(tpl)


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: uuid.UUID,
    body: TemplateUpdate,
    user: User = Depends(own_perm),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    tpl = await _get_writable(db, template_id, user)
    patch: dict[str, Any] = {}
    fields_set = body.model_fields_set  # None-значение осмысленно только для folder
    if "title" in fields_set and body.title is not None:
        tpl.title = body.title
        patch["title"] = body.title
    if "body" in fields_set and body.body is not None:
        tpl.body = body.body
        patch["body"] = True  # тело в аудит не тащим — может быть длинным
    if "folder" in fields_set:
        tpl.folder = _clean(body.folder)
        patch["folder"] = tpl.folder
    if not patch:
        return _out(tpl)
    # Перевод личного в общий (и обратно) запрещён — создайте новый (01 §7.4):
    # owner_id здесь не меняется никогда.
    await write_audit(
        db,
        user_id=user.id,
        action="template.updated",
        entity="template",
        entity_id=str(tpl.id),
        details={"shared": tpl.owner_id is None, "changed": sorted(patch)},
    )
    await db.commit()
    return _out(tpl)


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    user: User = Depends(own_perm),
    db: AsyncSession = Depends(get_db),
) -> Response:
    tpl = await _get_writable(db, template_id, user)
    shared = tpl.owner_id is None
    await db.delete(tpl)
    await write_audit(
        db,
        user_id=user.id,
        action="template.deleted",
        entity="template",
        entity_id=str(template_id),
        details={"shared": shared},
    )
    await db.commit()
    return Response(status_code=204)
