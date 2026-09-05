# VPS deploy — weekly gas monitor (Rucloud private runner)

**Роли**

| Хост | IP | Роль |
|---|---|---|
| Rucloud (Королёв, Ubuntu 22.04) | `45.8.230.214` | Приватный cron-раннер `weekly_gas_carrier_monitor.py`. Здесь лежит `.env` с `PROVIDER_*`. |
| LD8 (London) | `185.39.19.75` / `185.39.19.231` | Публичная витрина. **Секреты платного API сюда не класть.** |

Без Docker/CI/IaC. Идемпотентный повторный деплой безопасен.

Путь на Rucloud: `/opt/oracle1001/weekly_monitor`

---

## Что уже должно быть локально

- SSH root к `45.8.230.214` (BatchMode / ключ)
- Проект `Oracle-1001/7000` с `weekly_gas_carrier_monitor.py` и `output/fleet_database.csv`

---

## Деплой с Windows (рекомендуется)

Из корня проекта:

```powershell
cd C:\Users\MSI\Oracle-1001\7000
.\deploy\ruvds_gas_monitor\Deploy-Rucloud.ps1
```

С allowlist SSH (пример) и реальным ключом провайдера:

```powershell
.\deploy\ruvds_gas_monitor\Deploy-Rucloud.ps1 `
  -SshAllowCidrs "YOUR.PUBLIC.IP.HERE" `
  -ProviderApiKey "REAL_KEY" `
  -ProviderCostPerCallUsd "0.05" `
  -AllowDryRun 0
```

Пока `PROVIDER_API_KEY` не задан, `GAS_MONITOR_ALLOW_DRY_RUN=1` → джоба честно идёт в `--dry-run`
(summary пишется, кредиты не списываются; на 1GB RAM bootstrap ограничен
`GAS_MONITOR_DRY_RUN_LIMIT=30`).

## Факт приёмки (Rucloud `45.8.230.214`, 2026-08-12)

| Проверка | Результат |
|---|---|
| `systemctl is-active gas-monitor.timer` | `active` |
| `systemctl list-timers gas-monitor.timer` | next ≈ Mon 04:00 UTC + jitter |
| `systemctl start gas-monitor.service` | `Result=success`, `ExecMainStatus=0` |
| `summary.json` | создан в `features/gas_carrier_weekly/` |
| `bash -n` на `*.sh` | OK |
| `.env` mode | `600` |
| UFW | deny inbound except SSH/22; outbound allow |
| LD8 | секреты/`gas-monitor` **не** ставились |

Алерт после 2 подряд fail (на этапе отладки CRLF/placeholder) записан в `logs/alerts.log` — механизм подтверждён.

---

## Команды, которые выполняет установщик на сервере

(эквивалент ручного прогона)

```bash
# пакеты + venv + pip (lean: pandas/pydantic/dotenv)
apt-get update -y
apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates curl ufw
python3 -m venv /opt/oracle1001/weekly_monitor/venv
/opt/oracle1001/weekly_monitor/venv/bin/pip install -r requirements-monitor.txt

# код + data + wrapper
install -m 0755 run_gas_monitor.sh /opt/oracle1001/weekly_monitor/bin/
install -m 0644 weekly_gas_carrier_monitor.py /opt/oracle1001/weekly_monitor/
install -m 0644 fleet_database.csv /opt/oracle1001/weekly_monitor/data/
# .env создаётся из env.template только если отсутствует; chmod 600

install -m 0644 gas-monitor.service /etc/systemd/system/
install -m 0644 gas-monitor.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gas-monitor.timer

# ufw: deny inbound except SSH; allow outbound (HTTPS к провайдеру)
bash /opt/oracle1001/weekly_monitor/bin/setup_ufw.sh
```

**systemd**

- `gas-monitor.service` — `Type=oneshot`, `EnvironmentFile=-/opt/oracle1001/weekly_monitor/.env`, `ExecStart=.../bin/run_gas_monitor.sh`, `MemoryMax=700M`
- `gas-monitor.timer` — `OnCalendar=Mon *-*-* 04:00:00 UTC`, `RandomizedDelaySec=900`, `Persistent=true`

**Алерты:** при 2 подряд ненулевых exit-кодах строка пишется в  
`/opt/oracle1001/weekly_monitor/logs/alerts.log`  
(без Telegram/email, пока не подтверждён канал).

---

## Проверка успешности за ~2 минуты (один SSH)

```bash
ssh root@45.8.230.214 'set -e
systemctl is-active gas-monitor.timer
systemctl list-timers gas-monitor.timer --no-pager
systemctl start gas-monitor.service
systemctl show gas-monitor.service -p Result -p ExecMainStatus --value
test -f /opt/oracle1001/weekly_monitor/features/gas_carrier_weekly/summary.json
python3 -c "import json;s=json.load(open(\"/opt/oracle1001/weekly_monitor/features/gas_carrier_weekly/summary.json\"));print(s.get(\"generated_at_utc\"), \"coverage\", s.get(\"coverage_of_top_n_target_pct\"), \"dry_run\", s.get(\"dry_run\"))"
tail -n 20 /opt/oracle1001/weekly_monitor/logs/alerts.log 2>/dev/null || echo "alerts: none"
ufw status | head -n 20
'
```

Ожидаемо:

1. `gas-monitor.timer` → `active`
2. `list-timers` → next Monday ~04:00 UTC (+ jitter ≤900s)
3. `summary.json` существует после `systemctl start gas-monitor.service`
4. UFW: SSH allowed, 80/443 inbound нет

Локально перед выкладкой (на сервере делает `install.sh` / `Deploy-Rucloud.ps1`):

```bash
bash -n deploy/ruvds_gas_monitor/install.sh
bash -n deploy/ruvds_gas_monitor/run_gas_monitor.sh
bash -n deploy/ruvds_gas_monitor/setup_ufw.sh
```

---

## LD8 (витрина) — явно не делать

Не копировать `/opt/oracle1001/weekly_monitor/.env` и не ставить `gas-monitor.*` на `185.39.19.75` / `.231`.  
Публичный Mission Control может позже **забирать только JSON-артефакты** без ключей (scp/rsync results) — отдельным шагом по запросу.

---

## Операции

| Действие | Команда |
|---|---|
| Ручной запуск | `systemctl start gas-monitor.service` |
| Лог юнита | `journalctl -u gas-monitor.service -n 100 --no-pager` |
| Сменить ключ | `nano /opt/oracle1001/weekly_monitor/.env` → `chmod 600` → убрать `GAS_MONITOR_ALLOW_DRY_RUN=1` |
| Обновить флот CSV | перезапустить `Deploy-Rucloud.ps1` или `scp output/fleet_database.csv root@45.8.230.214:/opt/oracle1001/weekly_monitor/data/` |
