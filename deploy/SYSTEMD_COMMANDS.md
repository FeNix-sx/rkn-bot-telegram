# Команды systemd / логи — `rkn-bot`

Краткий справочник (служба: **`rkn-bot`**). Выполнять от **root** или с **`sudo`**.

| Команда | Зачем |
|--------|--------|
| `systemctl start rkn-bot` | Запустить бота (если остановлен). |
| `systemctl stop rkn-bot` | Остановить бота. |
| `systemctl restart rkn-bot` | Перезапуск (после обновления кода / `.env`). |
| `systemctl status rkn-bot` | Статус: работает ли, PID, последние строки лога. Выход из просмотра: **`q`**. |
| `systemctl enable rkn-bot` | Включить автозапуск при загрузке ОС (делается один раз). |
| `systemctl disable rkn-bot` | Убрать автозапуск (службу можно потом вручную `start`). |
| `systemctl is-active rkn-bot` | Коротко: `active` или `inactive`. |
| `systemctl is-enabled rkn-bot` | Коротко: `enabled` / `disabled` — стоит ли автозапуск. |
| `systemctl daemon-reload` | Перечитать юниты после правки файла в `/etc/systemd/system/` (потом обычно `restart`). |

## Логи

| Команда | Зачем |
|--------|--------|
| `journalctl -u rkn-bot -e` | Логи службы, в конец списка (`-e`). Выход: **`q`**. |
| `journalctl -u rkn-bot -f` | «Хвост» в реальном времени (как `tail -f`). Остановка: **`Ctrl+C`**. |
| `journalctl -u rkn-bot -n 50 --no-pager` | Последние 50 строк без пейджера (удобно копировать). |

## После правки `rkn-bot.service`

```bash
cp /opt/rkn-bot/deploy/rkn-bot.service /etc/systemd/system/rkn-bot.service
systemctl daemon-reload
systemctl restart rkn-bot
```
