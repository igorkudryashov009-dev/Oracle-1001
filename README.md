# Oracle-1001 · AIS Continuous Archive

Система непрерывного сбора AIS-позиций для судов из `fleet_database.csv`
через бесплатный поток [AISstream.io](https://aisstream.io), с ежедневными
снапшотами в `история1\` и браузерной витриной.

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
- AISstream принимает фильтр **только по MMSI**, лимит **50 MMSI на одно WebSocket**  
  ([документация](https://aisstream.io/documentation))  
- Нужно ≈ **ceil(unique_mmsi / 50)** параллельных подключений (легитимный батчинг)

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

### 4. Тест коллектора (поэтапно — не все 55 батчей сразу)

**Риск-аудит (28.07.2026):** в [документации AISstream](https://aisstream.io/documentation)
**нет** опубликованной цифры лимита на количество *одновременных* WebSocket-соединений
с одним API-ключом. Есть только общая фраза про throttling «at the api key and user
level» без числа, плюс жёсткий факт: **max 1 subscribe-сообщение/сек на соединение**
(не проблема — мы отправляем subscribe один раз при коннекте) и рекомендация не отдавать
ключ на клиентские сокеты (не наш случай — сборщик работает как backend-процесс).
Поэтому `collector.py` **не считает 55 соединений безопасными по умолчанию** и:

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
| `prepare_targets.py` | IMO+MMSI из fleet → targets |
| `collector.py` | WebSocket AISstream → SQLite |
| `daily_snapshot.py` | 24ч срез → daily/ + by_vessel/ |
| `forecast.py` | dead-reckoning JSON |
| `build_history_dashboard.py` | витрина HTML |
| `run_server.py` | http://127.0.0.1:8765 |
| `config.yaml` | mode / лимиты |
| `история1/` | архив |
| `история1/collector_unreachable_mmsi.json` | батчи, не подключившиеся 3+ раз подряд за сессию (генерируется, если такие есть) |

Mock-траектории (`trajectories.html` / `sample_ais_history_MOCK.csv`) —
**отдельная демо-витрина**, не путать с live-архивом.

---

## Логи и диагностика

| Лог | Содержание |
|---|---|
| `logs/collector_log.txt` | подписки, обрывы, POS-сэмплы |
| `logs/snapshot_log.txt` | итоги daily snapshot |

Типичные проблемы:

- Нет `POS` 10+ минут → проверьте ключ, firewall, что `--max-batches` не 0  
- Много `no_signal_24h` → судно в океане вне terrestrial AIS / collector не работал  
- Disconnect loop → смотрите throttle AISstream; уменьшите число параллельных батчей временно через `--max-batches`

---

## Юридическое / этическое

Используется только легитимный бесплатный API AISstream.io.
Лимит 50 MMSI/сокет обходится **разделением на параллельные подписки**, не хаками.
Ключ не светить в фронтенде (браузерный CORS к AISstream запрещён их политикой).
