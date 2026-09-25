# LeadChat — dev-команды спринтов 1–2. Запуск за 10 минут — README.md.

COMPOSE := docker compose -f docker-compose.dev.yml

.PHONY: dev down api worker scheduler migrate seed seed-avito test test-int lint fe-dev fe-build

dev: ## поднять dev-инфраструктуру: postgres + redis + fake-avito
	$(COMPOSE) up -d postgres redis fake-avito
	@echo ""
	@echo "Инфраструктура поднята: postgres :5432, redis :6379, fake-avito :8020"
	@echo "Дальше: make migrate && make seed, затем make api, make worker,"
	@echo "make scheduler и (в другом окне) make fe-dev"

down: ## остановить dev-инфраструктуру
	$(COMPOSE) down

api: ## бэкенд локально с hot-reload
	uv run uvicorn app.main:app --reload --port 8000

worker: ## ARQ-воркер локально: inbound-конвейер + доставка (docs/08 §2)
	uv run arq app.workers.main.WorkerSettings

scheduler: ## scheduler локально: heartbeat, token refresh, reconcile, партиции (docs/08 §6)
	uv run python -m app.scheduler.main

migrate: ## применить миграции Alembic
	uv run alembic upgrade head

seed: ## создать первого админа (CLI из docs/08 §7)
	uv run python -m app.cli create-admin

seed-avito: ## насыпать историю в fake-avito для backfill-теста (docs/07 §2.2)
	curl -sf -X POST http://localhost:8020/_control/seed_history \
		-H 'Content-Type: application/json' \
		-d '{"account_user_id": 111222333, "chats": 8, "unread_chats": 2}' \
		| python3 -m json.tool

test: ## unit-тесты (без внешних сервисов)
	uv run pytest tests/unit -q

test-int: ## integration-тесты (testcontainers: postgres + redis, нужен Docker)
#
# ⚠ БЕЗ DOCKER ЭТА ЦЕЛЬ ОБЯЗАНА КРАСНЕТЬ, А НЕ ПЕЧАТАТЬ «266 skipped».
#
# 23 августа владелец выполнил её и получил «266 skipped» с кодом возврата 0.
# Ноль означает «всё хорошо», и прочитать это иначе нельзя: ни одна проверка не
# выполнилась, а команда отчиталась успехом. Сам набор про эту ловушку знает —
# в шапке `tests/integration/conftest.py` записано, что пропущенный целиком
# прогон неотличим от настоящего, — но запрещает пропуск только на сборке (CI).
#
# Здесь другое: `make test-int` — это ПРЯМАЯ просьба выполнить интеграционные
# тесты. Не выполнить их по просьбе — неудача, а не «нечего делать». Обычный
# `make test` при этом не страдает: он про другой набор и Docker ему не нужен.
	@docker info >/dev/null 2>&1 || { \
		printf '\033[31m✗ Docker не запущен — интеграционные тесты выполнить нечем.\033[0m\n'; \
		printf '  Молчать об этом нельзя: «266 skipped» и «266 passed» возвращают\n'; \
		printf '  один и тот же 0, и тишина читается как успех.\n'; \
		printf '  Запустите Docker Desktop окном (не из скрипта: при ошибке он\n'; \
		printf '  показывает диалог, а без окна показывать его некому) и повторите.\n'; \
		exit 1; }
	uv run pytest tests/integration -q

lint: ## ruff + mypy
	uv run ruff check app tests
	uv run ruff format --check app tests
	uv run mypy app

fe-dev: ## фронтенд: Vite dev-сервер (:5173)
	cd frontend && npm run dev

fe-build: ## фронтенд: production-сборка
	cd frontend && npm run build

# ============================================================================
# Прод-таргеты (docs/05-DEPLOY-OPS.md, пошагово — docs/RUNBOOK-DEPLOY.md).
# Деплой идёт из CI по тегу v*; эти цели — для ручных операций и диагностики.
#
# Нужны переменные окружения (или префиксом к команде):
#   VPS_USER=deploy VPS_HOST=1.2.3.4 make prod-logs
# ============================================================================

VPS_USER    ?= deploy
VPS_HOST    ?=
# Каталог и адрес БОЕВОГО сервера. Оба умолчания были неверны и оба молча:
#   * /opt/leadchat на этом сервере не существует — система стоит в /srv;
#   * chat.partner-lead-centre.ru ведёт на <сторонний сервер>, где работает
#     FreeScout, чужой хелпдеск с действующим сертификатом на это же имя.
# То есть `make smoke` проверял чужую систему и мог сказать «всё хорошо».
PROD_DIR    ?= /srv/leadchat
PROD_DOMAIN ?= 188-225-34-82.sslip.io
TAG         ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
GHCR_NS     ?= ghcr.io/lead-partner
SSH          = ssh $(VPS_USER)@$(VPS_HOST)

.PHONY: prod-build prod-deploy prod-logs prod-backup smoke _need-host

_need-host:
	@test -n "$(VPS_HOST)" || { \
		echo "VPS_HOST не задан: VPS_HOST=1.2.3.4 make <цель>"; exit 2; }

prod-build: ## собрать прод-образы api и web локально (проверка сборки перед тегом)
	docker build -f docker/Dockerfile.api -t $(GHCR_NS)/leadchat-api:$(TAG) .
	docker build -f docker/Dockerfile.web -t $(GHCR_NS)/leadchat-web:$(TAG) \
		--build-arg VITE_APP_VERSION=$(TAG) .
	@echo ""
	@echo "Собрано: $(GHCR_NS)/leadchat-api:$(TAG) и leadchat-web:$(TAG)"
	@echo "В GHCR их пушит CI по тегу v* (.github/workflows/deploy.yml)"

prod-deploy: _need-host ## выкатить тег на VPS вручную (обычно это делает CI): TAG=<sha> make prod-deploy
	$(SSH) '$(PROD_DIR)/deploy/deploy.sh $(TAG)'

prod-logs: _need-host ## хвост логов прода: SVC=worker make prod-logs
	$(SSH) 'cd $(PROD_DIR) && docker compose logs -f --tail=200 $(or $(SVC),api worker scheduler)'

prod-backup: _need-host ## прогнать бэкап на VPS прямо сейчас (05 §6.2)
	$(SSH) '$(PROD_DIR)/deploy/backup.sh'

smoke: ## регрессионный smoke против прода (07 §6); STRICT=1 — SKIP тоже провал
	SMOKE_BASE_URL=https://$(PROD_DOMAIN) \
		./deploy/smoke.sh $(if $(STRICT),--strict,)
