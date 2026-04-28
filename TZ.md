# ТЗ

# ТЗ: Telegram-бот управления подписками 3x-ui

## 1. Стек и архитектура
| Компонент | Выбор | Обоснование |
|-----------|-------|-------------|
| Язык | Python 3.12+ | Современный async/await, строгая типизация |
| Фреймворк | `aiogram 3.x` | Native async, middleware, FSM, фильтры |
| HTTP-клиент | `httpx` | Async, поддержка таймаутов, retry |
| БД | `aiosqlite` | Не блокирует event loop, прямой `await`, минимальный оверхед |
| Планировщик | `APScheduler 3.x` | AsyncJobStore, cron/interval триггеры |
| Контейнеризация | Docker + docker-compose | Изоляция, volume для БД, миграция в 1 команду |
| Связь с панелью | REST API 3x-ui | `http://host.docker.internal:2053/panel/api/...` |

**Принципы доменной логики:**
- Временная модель: только UTC (`datetime.now(timezone.utc)`), хранение в ISO-8601 UTC.
- Источник лимитов/трафика: только настройки клиента в 3x-ui (БД хранит локальные метаданные).
- Подписка (Вариант A): бот не поднимает `/sub/<token>`, а отдает ссылку подписки, которую предоставляет 3x-ui.
- API и веб-панель 3x-ui доступны только админу/боту, не публикуются наружу.
- Оплата: разовый перевод на карту (manual), без эквайринга, без привязки карты и без автосписаний.

## 1.1 Подход к реализации (Local-first, Container-ready)
- Разработка и первичная отладка выполняются локально без Docker.
- Код и конфигурация сразу пишутся в container-ready стиле:
  - все настройки только через `.env`;
  - файловые пути без хардкода ОС, рабочий путь совместим с `/app/...`;
  - хранение времени только в UTC;
  - логирование в stdout + файл;
  - graceful shutdown для бота/планировщика/БД.
- Контейнеризация и серверный деплой выполняются только после прохождения локальных тестов и smoke-проверок.

**Топология:**
```
[Telegram Bot Container]
   ↓ (async httpx + aiosqlite)
[Host: 3x-ui Panel (127.0.0.1:2053)]
   ↓ (REST API)
[Xray Core]
```

---

## 2. Схема БД (`aiosqlite`)
```sql
CREATE TABLE IF NOT EXISTS users (
    tg_id BIGINT PRIMARY KEY,
    username TEXT,
    xui_email TEXT UNIQUE,
    xui_uuid TEXT,
    subscription_url TEXT,
    trial_start TEXT, -- UTC ISO-8601
    trial_end TEXT,   -- UTC ISO-8601
    paid_until TEXT,  -- UTC ISO-8601, NULL если нет paid-периода
    status TEXT NOT NULL DEFAULT 'disabled', -- disabled, trial, paid, payment_pending
    warned_48h INTEGER NOT NULL DEFAULT 0, -- 0/1
    warned_24h INTEGER NOT NULL DEFAULT 0, -- 0/1
    plan_devices INTEGER NOT NULL DEFAULT 1, -- выбранный тариф: число устройств
    limit_ip INTEGER DEFAULT 3,
    traffic_limit_bytes BIGINT DEFAULT 0, -- кэш лимита из 3x-ui, 0 = unlimited
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_trial_end ON users(trial_end);
CREATE INDEX IF NOT EXISTS idx_paid_until ON users(paid_until);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id BIGINT NOT NULL,
    provider TEXT NOT NULL, -- card_transfer
    provider_payment_id TEXT NOT NULL UNIQUE, -- внутренний order_id бота
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    devices INTEGER NOT NULL,
    period_days INTEGER NOT NULL DEFAULT 30,
    status TEXT NOT NULL, -- pending, succeeded, failed, canceled
    confirmation_url TEXT,
    webhook_event_id TEXT UNIQUE, -- не используется в manual-флоу, оставлено для совместимости
    proof_file_id TEXT, -- Telegram file_id скриншота/чека
    transfer_ref TEXT, -- комментарий/референс перевода
    approved_by BIGINT, -- tg_id админа, подтвердившего платеж
    approved_at TEXT, -- UTC ISO-8601
    reject_reason TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    paid_at TEXT, -- UTC ISO-8601
    FOREIGN KEY (tg_id) REFERENCES users(tg_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
```

---

## 3. Этапы реализации

### Этап 0: Фундамент и интеграция с API
- [ ] Включить API в 3x-ui: `Settings → API Settings → Enable` → сгенерировать токен
- [ ] Настроить `.env`: `BOT_TOKEN`, `XUI_API_URL=http://host.docker.internal:2053`, `XUI_API_TOKEN`, `ADMIN_IDS`, `TRIAL_DAYS=3`
- [ ] Реализовать `XUIAPI` класс:
  - `login()` → получить/обновить сессионный токен
  - `get_inbounds()` → список inbound'ов
  - `resolve_inbound_id(tag_or_remark="vless_reality")` → выбрать inbound по `remark/tag` (не по хардкоду)
  - `add_client(inbound_id, email, uuid, limit_ip)`
  - `disable_client(inbound_id, email)`
  - `get_client_traffics(inbound_id, email)`
  - `build_or_get_subscription_url(...)` → получить subscription URL клиента через возможности 3x-ui
- [ ] Инициализация `aiosqlite` с авто-созданием схемы
- [ ] Базовая структура проекта: `bot/`, `core/`, `db/`, `utils/`, `docker/`

**✅ Тесты:**
- `curl -H "X-UI-Token: <token>" <XUI_API_URL>/panel/api/inbounds/list` → `200 OK` + JSON
- `pytest tests/test_xui_api.py` → моковый сервер возвращает ожидаемые структуры
- Контейнер стартует, подключается к БД, лог пишет `DB initialized`

### Этап 1: Пользовательский флоу
- [ ] `/start` → проверка `users.tg_id`. Если нет → приветствие + кнопка `🚀 Получить триал`. Если есть → меню `📊 Статус`, `🔗 Ссылка`, `❓ Помощь`
- [ ] `/trial` →
  - Проверка: один триал на `tg_id` за всё время. Если триал уже был или активен `paid` → отказ.
  - Генерация: `uuid4()` и `xui_email=trial_<tg_id>`
  - Выбор inbound: поиск по `remark/tag = vless_reality`; при 0 или >1 совпадении — ошибка + лог + уведомление админу
  - Запрос к API: создание клиента в выбранном inbound'е
  - Запись в БД: `trial_start=now_utc`, `trial_end=now_utc + TRIAL_DAYS`, `status='trial'`, `warned_48h=0`, `warned_24h=0`
  - Ответ: URL подписки клиента, предоставленный 3x-ui (`subscription_url`), без доступа к админке
- [ ] `/status` → запрос к API → расчёт остатка трафика/дней → Markdown-карточка
- [ ] `/link` → повторная отправка `subscription_url` из БД
- [ ] `/buy` → выбор тарифа по количеству устройств (`1/2/3/5`), период всегда `30 дней`
- [ ] После выбора тарифа:
  - Бот создает внутренний `order_id`, сохраняет запись в `payments` со статусом `pending`
  - Бот отправляет пользователю реквизиты перевода (карта/СБП), сумму и `order_id` (обязателен в комментарии к переводу)
  - `users.status='payment_pending'` до факта оплаты

**✅ Тесты:**
- Новый юзер жмёт `/trial` → получает ссылку, в 3x-ui появляется клиент `trial_<tg_id>`, в БД запись
- `/status` возвращает `🟢 Триал | 12.4 ГБ / 100 ГБ | 2 дн. 14 ч.`
- Повторный `/trial` до истечения → ` Триал уже активен`
- Повторный `/trial` после истечения → `❌ Триал уже использован`
- Ссылка открывается в браузере/клиенте → валидная подписка, клиент не получает доступ к 3x-ui admin panel
- `/buy` создает заявку на оплату и возвращает реквизиты + `order_id`

### Этап 2: Планировщик и авто-управление
- [ ] `APScheduler` async job `check_trials()` → interval 1h
- [ ] Логика:
  - `now < trial_end <= now + 48h` и `warned_48h=0` → warning 48h + `warned_48h=1`
  - `now < trial_end <= now + 24h` и `warned_24h=0` → warning 24h + `warned_24h=1`
  - `trial_end <= now` и `status='trial'` → `disable_client()` + `status='disabled'` + notify
  - Все проверки/сравнения выполняются в UTC
- [ ] Graceful shutdown: `scheduler.shutdown()` при SIGTERM

**✅ Тесты:**
- Моковый юзер с `trial_end=now+47h` и `warned_48h=0` → получает warning и флаг становится `1`
- Повторный прогон job в том же окне не отправляет повторный warning
- Моковый юзер с `trial_end=now-1h` и `status='trial'` → API вызов `disable`, статус `disabled`, уведомление отправлено
- Логи содержат `Trial expired: <tg_id>`, нет ошибок таймаутов

### Этап 3: Платежи (перевод на карту и модерация)
- [ ] Реализовать `PaymentProvider` (первый адаптер: `manual_card_transfer`)
- [ ] Пользовательский flow подтверждения оплаты:
  - Кнопка `✅ Я оплатил` после `/buy`
  - Пользователь отправляет скрин/чек + `order_id`/комментарий
  - Бот сохраняет `proof_file_id`, `transfer_ref`, статус `pending`
- [ ] Админский flow модерации:
  - В админ-чат приходит карточка платежа с кнопками `✅ Подтвердить` / `❌ Отклонить`
  - При подтверждении: `approved_by`, `approved_at`, `payments.status='succeeded'`, `paid_at=now_utc`
  - Идемпотентность: повторное подтверждение того же `payment.id/provider_payment_id` не должно дублировать продление
  - `paid_until = max(now_utc, paid_until) + 30 days`
  - `status='paid'`, `plan_devices=<devices из платежа>`, `limit_ip=<devices>`
  - Обновить клиента в 3x-ui через API (продлить доступ/обновить лимит устройств)
  - Отправить пользователю уведомление об успешной оплате + `subscription_url`
- [ ] При отклонении:
  - `payments.status='failed'`, заполнить `reject_reason`
  - Вернуть пользователя в сценарий повторной оплаты (без изменения активного paid-периода)

**✅ Тесты:**
- Подтвержденный админом `pending` платеж продлевает подписку ровно на 30 дней
- Дубликат подтверждения того же платежа не меняет `paid_until` второй раз
- После подтверждения `limit_ip` в 3x-ui совпадает с выбранным тарифом устройств
- Пользователь получает сообщение `✅ Оплата получена, подписка активна до ...`
- Отклоненный платеж не меняет активный paid-период и корректно переводит в повторную оплату

### Этап 4: Админ-панель
- [ ] Middleware `admin_only` по `tg_id` из `ADMIN_IDS`
- [ ] `/admin` → inline-кнопки: `📊 Статистика`, `🔍 Поиск`, `💳 Платежи`, `⚙️ Настройки`
- [ ] `📊 Статистика` → запрос ко всем inbound'ам → парсинг → таблица:
  ```
  | @user | ↑ 1.2 ГБ ↓ 4.5 ГБ | 94.3 ГБ | 2 дн. | 🟢 Триал |
  | @admin | ∞ | ∞ | 0 дн. | 🔵 Admin |
  ```
- [ ] `🔍 Поиск` → ввод `@username`/`tg_id` → карточка + кнопки `🔌 Вкл/Выкл`, `🔄 Сброс трафика`
- [ ] `💳 Платежи` → журнал платежей (`pending/succeeded/failed`), поиск по `tg_id/provider_payment_id`, модерация pending-платежей

**✅ Тесты:**
- Некорректный `tg_id` → `🚫 Доступ запрещён`
- Таблица рендерится за <2с, данные совпадают с веб-панелью
- Кнопка `Выкл` → статус в БД `disabled`, клиент в 3x-ui отключён
- Экран платежей показывает актуальные статусы по ручной модерации

### Этап 5: Контейнеризация (после локальной стабилизации)
- [ ] `Dockerfile` (multi-stage, slim image, non-root user)
- [ ] `docker-compose.yml`:
  ```yaml
  services:
    bot:
      build: .
      env_file: .env
      volumes:
        - bot_data:/app/data
      restart: unless-stopped
      extra_hosts:
        - "host.docker.internal:host-gateway"
      healthcheck:
        test: ["CMD", "python", "-m", "bot.healthcheck"]
        interval: 30s
  volumes:
    bot_data:
  ```
- [ ] Логирование: `logging.config.dictConfig` → файл + stdout, ротация 10MB×3
- [ ] Graceful restart: сохранение состояния, закрытие БД, отмена задач

**✅ Тесты:**
- `docker compose up -d` → контейнер healthy, бот отвечает на `/start`
- `docker compose down` → БД сохраняется в volume, при `up` данные на месте
- В контейнере переменные окружения корректно читаются из `.env`, без хардкода путей/секретов

### Этап 6: Серверный деплой и эксплуатация
- [ ] Перенести `.env` на сервер (без коммита секретов), проверить `ADMIN_IDS`, `BOT_TOKEN`, `XUI_API_*`
- [ ] Настроить сетевую доступность контейнера бота к 3x-ui API (учесть особенности Linux и альтернативу `host.docker.internal`)
- [ ] Выполнить smoke после деплоя: `/start`, `/trial`, `/buy` → `pending` → `approve`, `/status`
- [ ] Проверить авто-логин к 3x-ui при истечении API-токена, бот не падает

**✅ Тесты:**
- Бот на сервере отвечает на `/start`, локальная и серверная логика совпадают
- End-to-end сценарий оплаты и активации проходит без ручных правок БД
- `check_trials()` запускается по расписанию после рестарта контейнера/хоста

---

## 4. Критерии готовности (DoD)
- [ ] Юзер получает триал за <5с, ссылка работает в Hiddify/Nekobox
- [ ] Автоотключение срабатывает точно в `trial_end`, уведомление доставлено
- [ ] Юзер делает перевод по реквизитам, админ подтверждает оплату в боте, доступ активируется автоматически
- [ ] Привязки карты и автосписаний нет; каждое продление — отдельная разовая оплата
- [ ] Админ видит актуальную статистику, управление работает без веба
- [ ] При рестарте контейнера/хоста состояние не теряется, периодический `check_trials()` запускается автоматически
- [ ] Покрытие тестами: unit >70%, integration с моком API проходит
- [ ] Docker-образ <150MB, memory footprint <50MB в простое

---

## 5. Дальнейшие шаги (зарезервировано)
- [ ] Платёжный шлюз (CryptoBot/ЮKassa) → интерфейс `PaymentProvider`, добавление webhook-адаптера без правок ядра
- [ ] Мониторинг сервера → вынос бота на отдельный VPS, health-check API хоста, alert в админ-чат
- [ ] Rate-limiting и anti-spam → `aiogram-throttling`, бан за флуд
- [ ] Multi-inbound поддержка → выбор локации/протокола при выдаче

**Формат поставки:** Git-репозиторий с ветками `dev`, `staging`, `main`. CI: `pytest`, `ruff`, `docker build`.
