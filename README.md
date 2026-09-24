# LeadChat

Исходный код LeadChat: онлайн-чата с операторами и связанного с ним лид-бота.

## Структура

| Каталог     | Что внутри                                   | Сервер        |
|-------------|----------------------------------------------|---------------|
| `leadchat/` | LeadChat: панель операторов, виджет, API     | `LeadChat`    |
| `lead-bot/` | Бот, создающий заявки из диалогов LeadChat   | `lead-bot-nl` |
| `scripts/`  | Служебные скрипты для серверов               | —             |

Каталоги `leadchat/` и `lead-bot/` появляются после импорта кода с серверов.

## Импорт кода с сервера

`scripts/import-from-server.sh` выгружает рабочий каталог сервиса в ветку
`import/<компонент>`. На сервере ничего не меняется. Ветка проходит ревью и
только потом попадает в `main`.

В репозиторий не попадают:

- зависимости и результаты сборки (`node_modules`, `venv`, `dist`);
- логи, загруженные пользователями файлы, базы данных, дампы, архивы;
- ключи и сессии (`*.pem`, `*.key`, `*.session`);
- файлы `.env`. Вместо них создаётся `.env.example` только с именами переменных.

Секреты, записанные прямо в коде (токены Telegram, пароли в строках подключения,
API-ключи), заменяются на `<REDACTED>`. Список всего, что не попало в
репозиторий, записывается в описание коммита.

Каждый сервер получает свой deploy key. Он даёт доступ только к этому репозиторию:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/leadchat_deploy -C "leadchat-deploy@$(hostname)"
cat ~/.ssh/leadchat_deploy.pub
```

Выведенный ключ добавьте в *Settings → Deploy keys → Add deploy key* и отметьте
*Allow write access*. Затем:

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/leadchat_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:goldshell88-maker/leadchat.git ~/leadchat-tools
bash ~/leadchat-tools/scripts/import-from-server.sh                            # где работают сервисы
bash ~/leadchat-tools/scripts/import-from-server.sh /var/www/leadchat leadchat
```

После импорта снимите с ключа *Allow write access* или удалите его.

## Отчёт о сервере

`scripts/server-report.sh` только читает данные и выводит: что занимает диск,
какие логи, дампы и кэши растут, размеры баз и таблиц, запущенные сервисы
и число их перезапусков, задачи cron, часовой пояс, открытые порты, базовые
настройки защиты (SSH, firewall, fail2ban, обновления).

```bash
sudo bash ~/leadchat-tools/scripts/server-report.sh > report-$(hostname).txt
```

## Правила

- Секреты хранятся только в `.env` на сервере, в репозиторий они не попадают.
- Изменения попадают в `main` только через pull request с ревью.
