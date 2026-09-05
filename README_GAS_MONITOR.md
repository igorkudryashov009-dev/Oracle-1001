# Weekly gas-carrier monitor (paid AIS)

Еженедельный опрос LNG/LPG-газовозов из `output/fleet_database.csv` через
credit-based API (**VesselFinder** по умолчанию) в рамках бюджета **$50/мес**.

При нехватке бюджета список **обрезается** (приоритет: `risk_tier`, затем DWT ↓);
`coverage_of_top_n_target_pct < 100` пишется в `summary.json`. Фиктивные позиции не создаются.

> Скелет `weekly_gas_carrier_monitor.py` в репозитории отсутствовал — модуль собран
> заново по контракту Mission Control. Если вы уже оплатили **другой** сервис
> (Datalastic / MarineTraffic и т.п.), задайте `PROVIDER_BASE_URL` / `PROVIDER_NAME`
> под его HTTPS IMO-lookup или сообщите URL — адаптер можно сузить точечно.

## Переменные окружения

Скопируйте `.env.example` → `.env` и заполните:

| Переменная | Обязательно | Смысл |
|---|---|---|
| `PROVIDER_API_KEY` | да | userkey / API key провайдера |
| `PROVIDER_COST_PER_CALL_USD` | да | стоимость одного вызова в USD |
| `PROVIDER_MONTHLY_BUDGET_USD` | нет (50) | месячный потолок |
| `PROVIDER_NAME` | нет | метка в отчётах (`vesselfinder`) |
| `PROVIDER_BASE_URL` | нет | `https://api.vesselfinder.com/vessels` |

Недельный лимит вызовов:

`floor(monthly_budget_usd / 4.345 / cost_per_call_usd)`

## Windows (PowerShell / cmd)

```powershell
cd C:\Users\MSI\Oracle-1001\7000
copy .env.example .env
notepad .env

# venv проекта
.\venv\Scripts\python.exe -m pytest tests\test_weekly_gas_carrier_monitor.py -v

# Ручной прогон на 12 газовозах без сети (артефакты + budget gate)
.\venv\Scripts\python.exe weekly_gas_carrier_monitor.py --dry-run --limit 12 --monthly-budget-usd 5 -v

# Боевой прогон (списывает кредиты провайдера!)
.\venv\Scripts\python.exe weekly_gas_carrier_monitor.py

# Обновить Mission Control (модуль 5)
.\venv\Scripts\python.exe build_mission_control.py
.\venv\Scripts\python.exe run_server.py
```

Откройте: `http://127.0.0.1:8765/output/mission_control.html`

Anaconda (если предпочитаете `C:\Users\MSI\anaconda3`):

```powershell
C:\Users\MSI\anaconda3\python.exe -m pip install pydantic python-dotenv pandas pytest
C:\Users\MSI\anaconda3\python.exe weekly_gas_carrier_monitor.py --dry-run --limit 12
```

Планировщик заданий Windows: еженедельно запускайте
`weekly_gas_carrier_monitor.py` (после настройки `.env`).

## Linux (systemd)

```bash
cd /opt/oracle-1001/7000   # или ваш путь
cp .env.example .env
nano .env
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# unit
sudo tee /etc/systemd/system/oracle-gas-weekly.service >/dev/null <<'EOF'
[Unit]
Description=Oracle-1001 weekly gas carrier monitor
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/oracle-1001/7000
EnvironmentFile=/opt/oracle-1001/7000/.env
ExecStart=/opt/oracle-1001/7000/venv/bin/python weekly_gas_carrier_monitor.py
User=oracle
EOF

sudo tee /etc/systemd/system/oracle-gas-weekly.timer >/dev/null <<'EOF'
[Unit]
Description=Weekly timer for Oracle-1001 gas monitor

[Timer]
OnCalendar=Mon *-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now oracle-gas-weekly.timer
sudo systemctl list-timers | grep oracle-gas
```

## Артефакты

Каталог `features/gas_carrier_weekly/`:

- `summary.json` — бюджет, coverage, counts
- `results.json` — успешные ответы провайдера
- `errors.json` — ошибки по судам
- `mission_control_module.json` — контракт модуля Mission Control

## Тесты

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_weekly_gas_carrier_monitor.py -v
```

Покрытие: нормальный прогон (mock HTTP), урезание бюджета, сетевая ошибка, отсутствие API-ключа, retry/backoff, auth без retry.
