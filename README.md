# Oracle-1001 · AIS Continuous Archive / Sentinel

Система непрерывного сбора AIS-позиций для судов из `fleet_database.csv`
через бесплатный поток [AISstream.io](https://aisstream.io), с ежедневными
снапшотами в `история1\` и браузерной витриной Sentinel HUD.

---

## READ THIS FIRST (Prompt 10 — out-of-box contract)

**Binding contract for humans and AI agents:** [`AGENTS.md`](./AGENTS.md)  
Install maintenance hook (Prompt 11): `python scripts/install_githooks.py`  
If this README (especially older “multi-batch collector” sections) conflicts with
`AGENTS.md` / `services/dual_gate.py` — **those win**.

| Question | Answer |
|---|---|
| Is coverage 3–5 a bug? | **No.** Terrestrial AIS ceiling (G3). Do not re-tune subscription to chase ≥100. |
| What blocks `--prod-rebuild`? | Only `pipeline_health_status != NOMINAL` — **not** low coverage. |
| Can I trade on LSSI/DAR/DFS now? | **No**, unless `fleet_sample_status=FULL` and `signal_status != insufficient_sample`. |
| Satellite AIS? | Stub only (`services/satellite_ais_adapter.py`) — no invented keys/endpoints. |
| Production ingest? | **`docker compose up -d`** (`sentinel-core` + `sentinel-web`). Manual `python -m services.aisstream_connector` = diagnostic only. |
| Canonical port / DB | **8765** · `история1/sentinel_ais.db` (Compose volume `data_sqlite`) |

```powershell
.\venv\Scripts\python.exe scripts\assert_out_of_box_contract.py
.\venv\Scripts\python.exe run_release.py --prod-gate --no-open
```

HUD: `http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10`  
Health: `http://127.0.0.1:8765/output/api/v1/health`

Historical reports under `output/*readiness*` / old “coverage gate=100” narratives are
**superseded** by dual-gate semantics (see Deploy Gate below + `CHANGELOG.md`).

---

## Главная точка входа — Mission Control

**Откройте эту страницу — она покажет состояние всей системы и переключит
вас на нужный модуль:**

```text
http://127.0.0.1:8765/output/mission_control.html
```

Сборка портала:

```powershell
.\venv\Scripts\python.exe build_mission_control.py
# или полный релизный прогон (финальный шаг = Mission Control):
.\venv\Scripts\python.exe run_release.py --skip-heavy
.\venv\Scripts\python.exe run_server.py
```

Портал объединяет 4 контура с **честной деградацией** (LIVE / ACCUMULATING /
STANDBY / NO FORECAST / AWAITING) — никогда не имитирует наличие данных:

| Модуль | Источник | Типичный статус при пустом архиве |
|---|---|---|
| 1 Fleet Registry | `output/dashboard.html` | LIVE (5908 судов) |
| 2 AIS Archive | `history_dashboard.html` | STANDBY (0/30 дней) |
| 3 Forecast Ensemble | `forecast_dashboard.html` | NO FORECAST (0 набл.) |
| 4 Causal TTF/Brent | `causal_analysis.py` | AWAITING (нет файла цен) |

### Внешний вид (Apple HIG)

Все четыре экрана + портал используют единый `output/design_system.css`
(системные шрифты SF Pro / -apple-system, светлый фон `#FFFFFF`, карточки
`#F5F5F7`, акцент Apple Blue `#0071E3`). Неон / Orbitron / тёмная «ЦУП»-сетка
убраны.

Как выглядит Mission Control на светлом фоне:
- тонкая sticky-навигация с плашкой **Fleet Intelligence** слева и компактными
  статус-пилюлями 4 модулей справа + тумблер светлая/тёмная тема;
- крупный спокойный aggregate: «1 из 4 модулей активны (LIVE)» и раскрывающийся
  блок «Что нужно для активации остальных»;
- четыре карточки модулей: зелёная точка **LIVE** у Fleet; оранжевые точки
  **STANDBY / NO FORECAST / AWAITING** у остальных (не красный — это ожидание
  данных, не сбой); у AIS/Forecast — прогресс-бары накопления;
- клик по карточке/пилюле переключает детальную панель модуля (iframe полной
  витрины только когда модуль реально LIVE).

Тёмная тема: `prefers-color-scheme` + ручной тумблер (`localStorage` `fi-theme`).

---

## 0. Честное ограничение (важно)

**AIS-поток даёт данные ТОЛЬКО с момента запуска сборщика (28.07.2026 → вперёд).**

Ретроактивной истории «1 месяц назад» на дату запуска **физически не существует**.
Глубина «1 месяц» появится через ~30 дней реальной работы collector,
«3 месяца» — через ~90 дней и т.д.

Пробелы, пока ПК выключен / нет сети / судно вне покрытия береговых AIS-станций —
**нормальны**. Система помечает их как `no_signal_24h` и **не выдумывает** координаты.

---

## Бюджет (≤ $30/мес)

| Компонент | Стоимость |
|---|---|
| AISstream.io (free tier) | **$0** |
| Локальный PC + Task Scheduler (`mode: local_scheduled`) | **$0** |
| Опционально VPS 24/7 (`mode: vps_always_on`, Hetzner/DO) | ~**$5**/мес |
| **Итого типичный** | **$0–5** |

---

## Факт по базе (не подгонять)

После `prepare_targets.py` смотрите реальные цифры. Пример прогона:

- Vessel-строк в `fleet_database` ≈ 5908  
- **Уникальных IMO с MMSI** ≈ **3021** (не 5900)  
- Уникальных IMO **без** MMSI ≈ 2307 → `targets_missing_mmsi.csv`  
- AISstream принимает фильтр **только по MMSI**  
- **Official limits (2026-09):** ≤**200 MMSI / subscription**, ≤**3 connections / account & IP**  
  ([документация](https://aisstream.io/documentation))  
- **Sentinel production:** **one** persistent WS + rotation of ≤200-MMSI chunks  
  (`config.yaml` → `sentinel.subscription_mode: single_persistent`).  
  Do **not** open ceil(N/200) parallel sockets — that hits the 3-connection cap (Prompt 6).

---

## Быстрый старт

### 1. API-ключ AISstream (вручную, бесплатно)

1. Откройте https://aisstream.io и войдите (GitHub и др.)
2. Создайте ключ: https://aisstream.io/apikeys
3. Скопируйте `.env.example` → `.env` и вставьте ключ:

```powershell
cd c:\Users\MSI\Oracle-1001\7000
copy .env.example .env
notepad .env
```

```
AISSTREAM_API_KEY=ваш_ключ_сюда
```

**Никогда не коммитьте `.env`.**

### 2. Зависимости

```powershell
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Список целей

```powershell
python prepare_targets.py
```

Создаёт: `targets.json`, `targets_batches.json`, `targets_missing_mmsi.csv`.

### 4. Production ingest (persistent) — canonical ops path

```powershell
docker compose up -d --build
# planes: sentinel-core (AIS + TTF) · sentinel-web (:8765 HUD)
# restart: unless-stopped · logs: json-file max-size=20m max-file=5
```

Ожидайте terrestrial plateau **единиц** (типично 3–5 simultaneous). Это не регресс.
Deploy Gate при `pipeline_health=NOMINAL` и `fleet_sample=LIMITED|INSUFFICIENT` — **PASS**.

### 4a. Diagnostic / manual mode only (not ops)

```powershell
# Process dies when you stop it — health.json will go stale between sessions.
.\venv\Scripts\python.exe -m services.aisstream_connector
# optional short soak:
.\venv\Scripts\python.exe -m services.aisstream_connector --once --duration 1800
```

### 4b. Legacy `collector.py` (архивный контур — не Sentinel prod)

> **WARNING:** секции ниже описывают исторический multi-batch collector →
> `история1/raw_positions.db`. Для Sentinel HUD / dual-gate / Docker core
> используйте §4 выше. Параллельный multi-WS против актуальных лимитов AISstream
> (3 connections) — известный антипаттерн (Prompt 6 → 429 / socket close).

**Риск-аудит (legacy, 28.07.2026; частично устарел):** ранее в документации не было
явной цифры на число одновременных WS. **Сейчас зафиксировано:** ≤3 connections /
account & IP, ≤200 MMSI/subscription. Legacy `collector.py` по-прежнему:

- запускает батчи с задержкой **2.5 сек** между стартами (`--stagger-seconds`), а не все
  в первую же миллисекунду;
- ограничивает **одновременные попытки handshake** семафором, по умолчанию **20**
  (`--max-concurrent-handshakes`) — сам лимит держится только на фазе connect+subscribe,
  не на количестве уже стабильно открытых соединений;
- если конкретный батч не смог подписаться **3 раза подряд**, помечает его
  «unreachable this session», пишет это в `logs\collector_log.txt` и в
  `история1\collector_unreachable_mmsi.json`, но **не останавливает** остальные батчи
  (graceful degradation).

Шаг A — безопасный первый тест (5 батчей = 250 MMSI):

```powershell
.\venv\Scripts\python.exe collector.py --test-minutes 15 --max-batches 5
```

Проверьте:
- строки `POS imo=… lat=… lon=…` в консоли и `logs\collector_log.txt`
  (доказательство живого потока, не mock);
- `история1\raw_positions.db` реально растёт:
  `sqlite3 история1\raw_positions.db "SELECT COUNT(*) FROM positions;"`
  (или через Python — см. ниже) до и после теста;
- разрыв связи → переподключение с backoff (видно в логе как
  `disconnect: ...; retry in Ns`, задержка растёт 1s → 2s → 4s → … → 120s).

Шаг B — если шаг A прошёл чисто, полный прогон на всех батчах:

```powershell
.\venv\Scripts\python.exe collector.py --test-minutes 15 --max-batches 55
```

Если после какого-то батча N вместо `subscribed OK` пойдут повторяющиеся
`UNREACHABLE this session` — это и есть **реальный эмпирический лимит**
AISstream на ваш ключ (зафиксируйте N в `SYSTEM_ACCEPTANCE_REPORT.md`, не
игнорируйте молча).

Постоянный режим (все батчи, без ограничения по времени):

```powershell
.\venv\Scripts\python.exe collector.py
# или
.\scripts\run_collector.bat
```

### 5. Ручной ежедневный снапшот (не ждать сутки)

```powershell
.\venv\Scripts\python.exe daily_snapshot.py
.\venv\Scripts\python.exe forecast.py
.\venv\Scripts\python.exe build_history_dashboard.py
```

`daily_snapshot.py` **идемпотентен**: повторный запуск за тот же день не
дублирует строки — существующая строка с тем же `snapshot_date` в
`by_vessel\{imo}.csv` заменяется, а не дописывается; `daily\{date}.csv`
перезаписывается целиком. Проверено ручным двойным прогоном.

Структура:

```
история1/
  raw_positions.db          # непрерывный сырой лог
  daily/2026-07-28.csv      # срез дня
  by_vessel/{imo}.csv       # накопительный трек на судно
  forecast/{imo}.json       # ОЦЕНКА dead-reckoning (не факт)
```

### 6. Windows Task Scheduler (режим `local_scheduled`)

**Обязательно PowerShell от администратора** — без этого `schtasks /RL HIGHEST`
молча падает с «Отказано в доступе», а скрипт теперь явно это проверяет и
останавливается (раньше падал тихо и рапортовал ложный успех — починено).

```powershell
cd c:\Users\MSI\Oracle-1001\7000
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\register_daily_task.ps1
```

Скрипт сам печатает верификацию (`schtasks /Query ... /V /FO LIST`) и явно
проверяет, что `Task To Run` указывает на `run_daily_snapshot.bat`. Этот bat
теперь вызывает **`venv\Scripts\python.exe` по полному пути**, а не `python`
через `PATH`/`activate.bat` — частая причина, по которой задача в Task
Scheduler падает с `ModuleNotFoundError` (использует системный Python без
`pandas`/`websockets`), хотя из интерактивной консоли всё работало.

Ручная проверка при необходимости:

```powershell
schtasks /Query /TN Oracle1001_AIS_DailySnapshot /V /FO LIST
schtasks /Run /TN Oracle1001_AIS_DailySnapshot
```

Задача запускает `scripts\run_daily_snapshot.bat` ежедневно ≈ **00:05 UTC**
(скрипт сам конвертирует 00:05 UTC в локальное время машины — не завязан на
хардкод TZ). Задача выполняется в контексте текущего пользователя, режим
«run only when user is logged on» (без сохранённого пароля) — если ПК
выключен/пользователь разлогинен в момент триггера, снапшот этого дня
**пропускается**, это ожидаемый и документированный пробел (см. раздел
«Операционные риски» ниже), а не ошибка.

Collector желательно держать запущенным отдельно (ярлык в автозагрузке /
`run_collector.bat`), иначе точки не пишутся в SQLite, пока процесс не активен.

### 6a. Восстановление после сбоя

| Симптом | Диагностика | Действие |
|---|---|---|
| Task Scheduler не срабатывает | `schtasks /Query /TN Oracle1001_AIS_DailySnapshot /V /FO LIST` → смотреть `Last Result` (не 0) и `Scheduled Task State` | Перерегистрировать: `.\scripts\register_daily_task.ps1` (админ-PowerShell) |
| Задача триггерится, но нет новых файлов в `история1\daily\` | Открыть `logs\snapshot_log.txt` — если пусто/старое, значит bat не достиг python | Проверить `Test-Path venv\Scripts\python.exe`; переустановить venv: `python -m venv venv; venv\Scripts\pip install -r requirements.txt` |
| Коллектор не пишет `POS` строки | `logs\collector_log.txt` — искать `UNREACHABLE`/`disconnect` подряд | Проверить `.env` (ключ не истёк/не отозван на https://aisstream.io/apikeys), проверить интернет/firewall на `wss://` |
| Нужно вручную закрыть «дырку» одного дня | — | `.\venv\Scripts\python.exe daily_snapshot.py --as-of 2026-08-01T00:05:00Z` (идемпотентно — повтор не дублирует строки) |
| Полный переезд/сброс | — | Удалить `история1\raw_positions.db` только если осознанно теряете сырые данные; `daily/` и `by_vessel/` не трогать без причины |

### 7. Витрина архива

`fetch()` к файлам не работает из `file://` — нужен локальный HTTP:

```powershell
python build_history_dashboard.py
python run_server.py
```

Откройте: http://127.0.0.1:8765/output/history_dashboard.html

Баннер в первый день честно покажет **«Дней накоплено: 0»** (или 1 после первого snapshot)
и текст: *«Сбор данных начат: 28.07.2026. Глубина архива растёт естественно.»*

Если выбран период 1М/3М/1Г больше глубины архива — появится явное предупреждение,
данные **не** подделываются.

Прогноз на карте — **оранжевый пунктир**, факт — **бирюза**. Подпись: «ОЦЕНКА (dead-reckoning)».

---

## Режим VPS (`vps_always_on`) — опционально ~$5/мес

1. Арендуйте VPS (Hetzner CX22 / DigitalOcean Basic и т.п.)
2. Скопируйте проект, создайте `.env` с ключом
3. systemd unit `/etc/systemd/system/oracle-ais-collector.service`:

```ini
[Unit]
Description=Oracle-1001 AIS Collector
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/oracle-1001/7000
ExecStart=/opt/oracle-1001/7000/venv/bin/python collector.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/oracle-1001/7000/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now oracle-ais-collector
```

Cron для снапшота (00:05 UTC):

```cron
5 0 * * * cd /opt/oracle-1001/7000 && ./venv/bin/python daily_snapshot.py && ./venv/bin/python forecast.py && ./venv/bin/python build_history_dashboard.py
```

Переключите `mode: vps_always_on` в `config.yaml` для документации/напоминания.

---

## Архитектура файлов

| Файл | Роль |
|---|---|
| `AGENTS.md` | **Binding** out-of-box contract (Prompt 10/11) |
| `githooks/pre-commit` | Contract drift guard (Prompt 11) |
| `services/aisstream_connector.py` | Sentinel production AIS ingest (1 WS + rotation) |
| `services/dual_gate.py` / `release_gate.py` | Pipeline vs fleet-sample Deploy Gate |
| `build_sentinel_dashboard.py` | Sentinel HUD + health.json |
| `prepare_targets.py` | IMO+MMSI из fleet → targets |
| `collector.py` | Legacy WebSocket AISstream → `raw_positions.db` (не Sentinel prod) |
| `daily_snapshot.py` | 24ч срез → daily/ + by_vessel/ |
| `forecast.py` | dead-reckoning JSON |
| `build_history_dashboard.py` | витрина HTML |
| `run_server.py` / `scripts/serve_dashboard.py` | http://127.0.0.1:8765 |
| `config.yaml` | mode / Sentinel limits (`single_persistent`) |
| `история1/sentinel_ais.db` | Sentinel production replica |
| `история1/` | архив (+ legacy `raw_positions.db`) |
| `история1/collector_unreachable_mmsi.json` | батчи, не подключившиеся 3+ раз подряд за сессию (генерируется, если такие есть) |

Mock-траектории (`trajectories.html` / `sample_ais_history_MOCK.csv`) —
**отдельная демо-витрина**, не путать с live-архивом.

---

## Deploy Gate (dual semantics)

`--prod-rebuild` / `--prod-gate` **block publish only** when `pipeline_health_status != NOMINAL`:

- AIS lag &lt; 300s, WAL/DB integrity OK, no HTTP 429 / reconnect storm, dashboard port **8765** only

`fleet_sample_status` (`FULL` ≥100 / `LIMITED` ≥5 / `INSUFFICIENT` &lt;5) is **informational** —
it does **not** fail Deploy Gate. It **must** be checked by any automatic downstream
consumer (alerts, trading signals) before acting on fleet-wide quant fields
(LSSI / DAR / DFS / live ensemble confidence). Observed terrestrial AIS ceiling
(Prompt 7 soak): peak coverage ≈5, cumulative unique ≈7/hour.

Satellite AIS: `services/satellite_ais_adapter.py` is a **stub only** — activate only
after an explicit provider choice and real credentials (never invent endpoints/keys).

---

## Логи и диагностика

| Лог | Содержание |
|---|---|
| `logs/collector_log.txt` | подписки, обрывы, POS-сэмплы |
| `logs/snapshot_log.txt` | итоги daily snapshot |
| `logs/aisstream_ws_attempts.jsonl` | handshake/subscribe attempts |
| `logs/health_coverage_daily.jsonl` | append-only G3 health/coverage (Prompt 11) |

Типичные проблемы:

- Нет `POS` 10+ минут → проверьте ключ, firewall; TOP-500 в океане часто невидимы terrestrial AIS  
- Много `no_signal_24h` → судно в океане вне terrestrial AIS / collector не работал  
- HTTP 429 / reconnect storm → `pipeline_health_status` → CRITICAL; single-WS connector should not multi-batch

---

## Юридическое / этическое

Используется только легитимный бесплатный API AISstream.io.
Официальный лимит: **до 200 MMSI / подписку**, **до 3 соединений / аккаунт**
([документация](https://aisstream.io/documentation)). Sentinel держит **одно** WS и
ротирует FiltersShipMMSI. Ключ не светить в фронтенде (браузерный CORS к AISstream запрещён их политикой).
