# FINAL REPORT — Oracle-1001 Fleet OSINT Pipeline

**Рабочая область:** `c:\Users\MSI\Oracle-1001\7000`  
**Исходник (read-only):** `C:\Users\MSI\OneDrive\Desktop\7000\Книга 1 (1).xlsx`, лист `7000`  
**Финальная итерация:** 5  
**Дата прогона:** 2026-07-28

---

## 1. Итоговая сводка метрик

| Метрика | Значение |
|---|---|
| Всего строк исходника | **6036** |
| Строк обработано (непустые) | **6034** |
| Vessel в основной базе (`fleet_database`) | **5908** |
| Non-vessel исключено | **1** |
| Уникальных валидных IMO (в vessel-базе) | **5326** |
| Несовпадение IMO (`imo_mismatch`) | **24** |
| Подозрение на подмену идентичности (`identity_conflict_flagged`) | **1** |
| Невалидная контрольная цифра / формат IMO | **3** |
| Требуют ручной проверки (`needs_review`) | **221** |
| Средняя заполненность полей по vessel-записям | **59.96%** |

### Разбивка по `source_confidence` (от 6036 строк исходника)

| source_confidence | Записей | % от 6036 |
|---|---:|---:|
| `parsed` | 5787 | 95.87% |
| `needs_review` | 221 | 3.66% |
| `imo_mismatch` | 24 | 0.40% |
| `identity_conflict_flagged` | 1 | 0.02% |
| `non_vessel` | 1 | 0.02% |
| *(полностью пустые строки, пропущены)* | 2 | 0.03% |
| **Итого** | **6036** | **100%** |

---

## 2. Применённые бизнес-правила (полная история решений)

### Итерация 2 — базовый контракт парсинга (правила 1–8)

1. **Пустой текст:** если столбец A содержит 7 цифр → `imo=A`, `source_confidence=needs_review`, текстовые поля null; иначе `imo=UNKNOWN`, `needs_review`.
2. **Лейблы имени** (начало строки, специфичные фразы первыми):  
   `Наименование судна|Наименование|Название судна|Название|Реальное имя судна`.
3. **Паттерн `IMO / MMSI: X / Y`** проверяется первым; группа 1 → `imo_from_text`, группа 2 → `mmsi`.
4. **`imo_from_text`** — только первое явное совпадение среди `IMO:`, `IMO номер:`, или группы 1 из п.3. Вторичные упоминания (в т.ч. «Истинный IMO») сюда **не** попадают.
5. **Подмена идентичности** — отдельный паттерн  
   `(?:Истинный IMO|Реальный IMO|Подлинный IMO)` → `identity_spoofing_suspected_imo` + note;  
   `source_confidence=identity_conflict_flagged` **перезаписывает** `imo_mismatch` (более специфичная категория).
6. *(исходное правило 6 по vessel_type/AtoN — заменено в итерациях 3–5, см. ниже)*.
7. **Разделение статусов:**  
   - `nav_status` ← только `Навигационный статус:`  
   - `asset_status` ← `Статус актива:`  
   - `compliance_risk_level` ← `Уровень риска` / `Статус комплаенса` **или** фразы НИЗКИЙ/СРЕДНИЙ/ВЫСОКИЙ/КРИТИЧЕСКИ ВЫСОКИЙ РИСК  
   Поля никогда не смешиваются.
8. **Формат столбца A ≠ 7 цифр** (`imo_format_error=True`) — это **не** `imo_mismatch`; `imo_from_text` извлекается как обычно для аудита.

**Приоритет идентификатора:** первичный `imo` = нормализованный столбец A; текст используется для сверки и аудита, но не подменяет A при mismatch.

### Итерация 3 — презумпция судна + алиас типа

- Лейбл **`Тип:`** добавлен как алиас `Тип судна:` / `Тип объекта:`.
- **Презумпция vessel:** если нет явного non-vessel признака, но извлечено хотя бы одно из  
  `vessel_name | mmsi | imo_from_text | loa_m | nav_status` → `vessel_category=vessel`,  
  даже при `vessel_type=null`.
- Отсутствие `vessel_type` само по себе **не** форсирует `needs_review`; порог остаётся `< 6` значимых полей.

### Итерация 4 — word-boundary для маркеров (промежуточная)

- Устранение FP `Baton Rouge` ⊂ `aton` через границы слова и case-sensitive `AtoN`.
- Выявила следующий класс FP: описание SPM-«буя» в тексте рейса обычного танкера.

### Итерация 5 — финальная архитектура non-vessel *(текущая)*

- Non-vessel маркеры ищутся **только в извлечённом `vessel_type`** (содержимое после `Тип судна:` / `Тип:` / `Тип объекта:`).
- Поиск по остальному `raw_text` (порты, SPM, описания) **не ведётся**.
- Маркеры в `vessel_type`:  
  `Aid to Navigation`, `AtoN` (точный регистр + границы),  
  `буй`/`буи`/`буйки` (**без** `буя`/`буем`),  
  `навигационный знак`, `маяк`.
- Иначе — презумпция vessel из итерации 3.

**Приоритет `source_confidence`:**  
`identity_conflict_flagged` > `non_vessel` > `imo_mismatch` > `needs_review` > `parsed`.

---

## 3. Известные ограничения базы

### 3.1. `imo_mismatch` — 24 записи (экспертная проверка обязательна)

Столбец A и первый явный IMO в тексте расходятся. Автоматически выбрать «верный» нельзя: нужна сверка с реестром (Equasis / IMO GISIS / класс).

| # | IMO (столбец A) | IMO из текста | vessel_name |
|---:|---|---|---|
| 1 | 9737204 | 9736808 | SENWA MARU |
| 2 | 9367736 | 9975507 | MRAIKH |
| 3 | 9360829 | 9224764 | FSO AFRICA |
| 4 | 9892822 | 9692822 | SEARAMBLER |
| 5 | 9357640 | 9357676 | CECILE |
| 6 | 9357676 | 9357640 | SIENE |
| 7 | 1087639 | 9825439 | MAERSK DETROIT. |
| 8 | 9786231 | 9899727 | ELKA ATHINA. |
| 9 | 9290361 | 9378876 | ELKA ATHINA. |
| 10 | 9380520 | 9455703 | IKARIOTIKOS |
| 11 | 9868120 | 9379777 | CHANG HANG HONG TU |
| 12 | 9330173 | 9517941 | KOKUHO MARU |
| 13 | 9247194 | 9669940 | STI OPERA |
| 14 | 9162163 | 9304667 | SKIPPER (исторические названия: TOYO, MAERA, ADISA) |
| 15 | 9280885 | 9288088 | *(нет имени)* |
| 16 | 9360843 | 9367437 | AL THUMAMA |
| 17 | 9723679 | 9184392 | MARITIME JEWEL |
| 18 | 9406350 | 9184392 | MARITIME JEWEL |
| 19 | 9610767 | 9717761 | SEAWAYS WARWICK |
| 20 | 9952983 | 1044900 | BELLA TORTUM ST (ранее HYUNDAI MIPO 8417) |
| 21 | 9898515 | 9184392 | MARITIME JEWEL |
| 22 | 9210098 | 9387437 | FALCON SILK |
| 23 | 9204336 | 9204348 | *(нет имени)* |
| 24 | 9431379 | 9200081 | *(нет имени)* |

Файл: `output\imo_mismatch.csv`.

### 3.2. `identity_conflict_flagged` — 1 запись (приоритет №1)

| Поле | Значение |
|---|---|
| IMO (A) | 9474333 |
| vessel_name | ALAM RUKUN |
| identity_spoofing_suspected_imo | 9333670 |
| note | фрагмент с «Истинный IMO: 9333670» (контекст про «Михаил Ульянов») |

Явный сигнал возможной подмены идентичности → приоритет для sanctions / dark-fleet проверки.  
Файл: `output\identity_conflicts.csv`.

### 3.3. `needs_review` — 221 запись

В основном пустые или малоинформативные исходные тексты (`filled < 6` значимых полей) либо отсутствие обоих источников IMO. Дальнейшее автоматическое улучшение **без нового источника данных** нецелесообразно.  
Файл: `output\needs_review.csv`.

### 3.4. Заполненность vessel ~60%

~40% полей схемы в среднем не найдены в исходном тексте — это ограничение **исходных OSINT-блоков**, не бага парсера.

**Поля с наименьшим % заполненности** (по `fleet_database`, n=5908):

| Поле | Заполненность |
|---|---:|
| `asset_status` | 0.15% |
| `departure_port` | 0.15% |
| `departure_datetime_utc` | 0.15% |
| `arrival_datetime` | 2.15% |
| `compliance_risk_level` | 23.58% |

Для сравнения: `flag` ≈ 99%, `vessel_name` ≈ 89%, `imo_from_text` ≈ 91%, `loa_m` ≈ 83%.

### 3.5. Прочее

- **Дубликаты IMO** в vessel-базе — один и тот же IMO встречается в нескольких строках исходника (разные редакции блока); список в `logs\run_log.txt`.
- **`invalid_imo_checksum.csv` (3 строки):** включает format error (не 7 цифр в A) и checksum fail — см. файл.
- Non-vessel: ровно **NEW HORAMSHAHR ATON** (`Тип объекта: … Aid to Navigation …`).

---

## 4. Структура выходных файлов

| Файл | Содержимое | Строк данных | Размер (байт) |
|---|---|---:|---:|
| `fleet_database.csv` | `vessel_category == vessel` | 5908 | 19 664 577 |
| `fleet_database.xlsx` | то же, Excel | 5908 | 2 982 409 |
| `fleet_database.json` | то же, JSON для дашборда | 5908 | 24 551 731 |
| `dashboard.html` | self-contained дашборд (JSON встроен) | — | 23 578 255 |
| `non_vessel_entities.csv` | AtoN / non-vessel | 1 | 3 774 |
| `needs_review.csv` | `source_confidence == needs_review` | 221 | 786 410 |
| `imo_mismatch.csv` | A ≠ первый IMO в тексте | 24 | 80 600 |
| `identity_conflicts.csv` | «Истинный/Реальный/Подлинный IMO» | 1 | 4 390 |
| `invalid_imo_checksum.csv` | `imo_valid == False` | 3 | 10 334 |
| `FINAL_REPORT.md` | этот отчёт | — | — |

Лог прогона: `logs\run_log.txt`.

---

## 5. Инструкция по повторному запуску

### Требования

- Python 3.10+ (в проекте есть `venv`)
- Зависимости: `pandas`, `openpyxl`, `pydantic` (`requirements.txt`)
- Исходный файл доступен по пути из `run_all.py` → константа `SOURCE`  
  (сейчас: `C:\Users\MSI\OneDrive\Desktop\7000\Книга 1 (1).xlsx`)

### Перед запуском проверить

1. Лист называется **`7000`**.
2. Структура: столбец **A** = IMO (или идентификатор), столбец **B** = текстовый OSINT-блок.
3. Файл **только на чтение** — пайплайн его не изменяет.
4. OneDrive-синхронизация не блокирует чтение файла.

### Команда

```powershell
cd c:\Users\MSI\Oracle-1001\7000
.\venv\Scripts\python.exe run_all.py
```

Полный прогон перезапишет все файлы в `output\` (включая `dashboard.html` со встроенным JSON) и `logs\run_log.txt`.

### После запуска

1. Сверить `=== SUMMARY ===` в консоли / `logs\run_log.txt`.
2. Открыть `output\dashboard.html` (двойной клик; CDN для DataTables/Chart.js нужен при первом открытии).
3. Проверить вкладку **Identity Conflicts** и карточки метрик.

### Docker (опционально)

В репозитории docker-обёртки нет; при необходимости:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# смонтировать исходный xlsx и поправить SOURCE в run_all.py / env
CMD ["python", "run_all.py"]
```

---

## Контрольные подтверждения финального прогона (итерация 5)

| Проверка | Результат |
|---|---|
| `non_vessel_entities.csv` ровно 1 запись: NEW HORAMSHAHR ATON | ✅ |
| FEADSHIP (IMO 9322279) в `fleet_database` с `vessel_category=vessel` | ✅ |
| UNIQUE INFINITY (IMO 9540833) в `fleet_database` с `vessel_category=vessel` | ✅ |
| `identity_conflicts.csv` = 1 (ALAM RUKUN) | ✅ |
| `imo_mismatch.csv` = 24 | ✅ |
| `invalid_imo_checksum.csv` = 3 | ✅ |
| `needs_review.csv` = 221 (без существенных изменений) | ✅ |
| Новых false positive/negative после итерации 5 | ✅ не обнаружено |
