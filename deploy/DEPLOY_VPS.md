# Деплой RKN_bot на VPS — чеклист

Использование: выполняй шаги по порядку. Следующий шаг — только если критерий «Готово» выполнен.  
Прогресс можно отмечать здесь (`[x]`) или кидать скрины/вывод в чат — отметим вместе.

**Пути по умолчанию** (как в `rkn-bot.service`): `/opt/rkn-bot`, запуск `.venv/bin/python -m bot`, переменные из `/opt/rkn-bot/.env`.

---

## Шаг 0. Подготовка VPS

- [x] VPS: Ubuntu 22.04/24.04 или Debian 12
- [x] Есть IP, доступ по SSH
- [ ] (Опционально) `sudo apt update && sudo apt upgrade -y`

**Профиль инстанса (fenix-and-bro):** Ubuntu 22.04.5 LTS, 2 vCPU / ~3.8 GiB RAM, 30 GB SSD, публичный IP **89.125.85.47**, Docker уже есть + overlay (`amn0`). SSH: `root@89.125.85.47`. Для клиентов vless в `.env` при необходимости: `XUI_VLESS_HOST=89.125.85.47`.

**Не делать:** не светить токен бота и пароли; не запускать сам бот постоянно под `root` (отдельный пользователь — шаг 4).

**Готово, если:** заходишь по SSH, ОС выбрана/обновлена по желанию.

---

## Шаг 1. Код на сервере

Репозиторий: https://github.com/FeNix-sx/rkn-bot-telegram

```bash
cd /opt/rkn-bot
git clone https://github.com/FeNix-sx/rkn-bot-telegram.git .
# если «destination path already exists and is not empty» — сделай ls -a; удали лишнее или клонируй во временную папку и перенеси файлы
```

- [x] Каталог `/opt/rkn-bot` (или свой путь — тогда правь юнит и этот чеклист)
- [x] Репозиторий склонирован / скопирован: есть `bot/`, `requirements.txt`, `deploy/rkn-bot.service` (юнит на VPS создан вручную — имеет смысл закоммитить `deploy/` в репо)

**Не делать:** не путать путь на диске с тем, что в `WorkingDirectory` в systemd; не коммитить `.env` в публичный git.

**Готово, если:** структура проекта на сервере совпадает с репо.

---

## Шаг 2. Python и venv

```bash
cd /opt/rkn-bot
sudo apt install -y python3 python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
deactivate
```

- [x] venv создан: `/opt/rkn-bot/.venv`
- [x] Зависимости установлены

Проверка:

```bash
/opt/rkn-bot/.venv/bin/python -c "import aiogram; print(aiogram.__version__)"
```

**Не делать:** не полагаться на глобальный `pip` без venv — юнит использует `.venv/bin/python`.

**Готово, если:** импорт aiogram без ошибки.

---

## Шаг 3. `.env` на сервере

- [x] Файл `/opt/rkn-bot/.env` создан по образцу `.env.example` (в т.ч. залит с ПК)
- [x] Заполнены: `BOT_TOKEN`, `XUI_*`, `ADMIN_IDS`, при необходимости `DB_PATH`, `XUI_VLESS_HOST`
- [x] Права: `chmod 600 /opt/rkn-bot/.env`

**Не делать:** в строках для `EnvironmentFile` systemd не оборачивай значения в кавычки — они могут уехать в значение переменной.

**Готово, если:** все нужные переменные заданы, файл не world-readable.

---

## Шаг 4. Пользователь сервиса

```bash
sudo useradd --system --home /opt/rkn-bot --shell /usr/sbin/nologin rknbot
sudo chown -R rknbot:rknbot /opt/rkn-bot
```

В `/etc/systemd/system/rkn-bot.service` раскомментировать:

```ini
User=rknbot
Group=rknbot
```

- [x] Пользователь `rknbot` создан и `chown -R rknbot:rknbot /opt/rkn-bot` выполнен
- [x] Владелец `/opt/rkn-bot` — `rknbot:rknbot` (проверка: `ls -ld`, `stat` на `.env`)
- [x] В юните в `deploy/rkn-bot.service` раскомментированы `User=rknbot` и `Group=rknbot`

**Не делать:** не оставлять сервис от `rknbot`, если дерево файлов всё ещё `root` — не будет записи в `data/`.

**Готово, если:** владелец и юнит согласованы.

---

## Шаг 5. БД и каталог `data/`

По умолчанию в примере: `DB_PATH=./data/bot.sqlite3` (путь от `WorkingDirectory`).

```bash
sudo -u rknbot mkdir -p /opt/rkn-bot/data
# при миграции с другой машины: положить bot.sqlite3 в data/
sudo chown -R rknbot:rknbot /opt/rkn-bot/data
```

- [ ] Каталог `data/` есть
- [ ] Имя файла БД совпадает с `DB_PATH` в `.env`
- [ ] Права на запись у пользователя сервиса

**Готово, если:** путь к БД корректен; после первого запуска нет ошибок доступа к файлу.

---

## Шаг 6. Ручной прогон (до systemd)

```bash
sudo -u rknbot -H bash -lc 'cd /opt/rkn-bot && set -a && source .env && set +a && .venv/bin/python -m bot'
```

- [ ] Бот стартует, в Telegram отвечает
- [ ] В консоли нет фатальных ошибок
- [ ] Остановлен (Ctrl+C) перед переходом к systemd

**Не делать:** не включать systemd, пока ручной запуск не стабилен.

**Готово, если:** ручной запуск ок.

---

## Шаг 7. systemd

```bash
sudo cp /opt/rkn-bot/deploy/rkn-bot.service /etc/systemd/system/rkn-bot.service
sudo nano /etc/systemd/system/rkn-bot.service   # WorkingDirectory, ExecStart, User, EnvironmentFile
sudo systemctl daemon-reload
sudo systemctl enable rkn-bot
sudo systemctl start rkn-bot
sudo systemctl status rkn-bot
journalctl -u rkn-bot -f
```

- [ ] Юнит скопирован в `/etc/systemd/system/`
- [ ] Пути и пользователь проверены
- [ ] `daemon-reload`, `enable`, `start`
- [ ] `status` = active (running)
- [ ] Логи без бесконечного crash loop

**Не делать:** не править только `deploy/rkn-bot.service` в репо — systemd читает `/etc/systemd/system/`; после правок юнита не забывать `daemon-reload`.

**Готово, если:** сервис работает, логи чистые.

---

## Шаг 8. Сеть и 3x-ui

- [ ] С сервера доступен `XUI_API_URL` (часто `http://127.0.0.1:2053` для локальной панели)
- [ ] Firewall: исходящий к Telegram; для long polling входящий к боту не нужен
- [ ] (Если webhook — отдельно HTTPS и порт)

**Не делать:** не оставлять панель с дефолтами в открытом интернете без мер защиты.

**Готово, если:** в логах нет постоянных ошибок подключения к XUI.

---

## Шаг 9. Обновления кода (на будущее)

```bash
cd /opt/rkn-bot
sudo -u rknbot git pull
sudo -u rknbot /opt/rkn-bot/.venv/bin/pip install -r requirements.txt
sudo systemctl restart rkn-bot
```

- [ ] Запомнил процесс: pull → pip → restart

**Не делать:** не рестартовать без `pip install` после изменения `requirements.txt`.

---

## Быстрая шпаргалка «что не делать»

| Не делать | Почему |
|-----------|--------|
| Токены в git / публично | компрометация |
| Пути в юните ≠ реальные | сервис не стартует |
| Несовпадение `User` и `chown` | нет записи в `data/` |
| Кавычки в `.env` для systemd | битые значения |
| Пропуск ручного запуска | долгий дебаг через restart loop |

---

## Лог для отладки (заполняй при проблемах)

Дата / шаг / симптом / вывод команд:

```
(пример)
2026-__-__  шаг 7  status: ...
journalctl: ...
```
