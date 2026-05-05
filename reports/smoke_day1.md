# Smoke report: day1

Дата: 2026-04-28
Режим: local deterministic smoke (без Telegram polling, с временной SQLite и fake 3x-ui)

## Сценарий

1. Новый пользователь -> `/start` (эквивалент через `create_user_if_not_exists` + `get_user`)
2. `/trial` -> клиент выдан (эквивалент через текущую trial-логику: inbound resolve/add client/save trial)
3. Повторный `/trial` -> отказ (проверка флага `has_trial_used=1`)
4. `/status` -> карточка статуса
5. `/link` -> повторная выдача ссылки

## Результаты

- PASS `start_new_user`
- PASS `trial_first_request`
- PASS `trial_second_request_denied`
- PASS `status_after_trial`
- PASS `link_returns_url`

## Детали прогона

- inbound_id: `101` (fake)
- subscription_url: `https://fake-xui.local/sub/trial_900000001`
- status: `trial_active`
- has_trial_used: `yes`
- has_subscription_url: `yes`

## Ограничения текущего smoke

- Это локальный deterministic smoke, не e2e через Telegram API.
- Внешний 3x-ui заменен на fake-клиент, чтобы зафиксировать корректность day1-flow независимо от сети и внешнего стенда.
