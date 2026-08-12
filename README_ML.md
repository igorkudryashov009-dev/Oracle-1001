# README_ML — Oracle-1001 Forecast Ensemble

Методология прогноза баланса DWT-флота (`laden_dwt_sum` и родственные индексы)
на основе сигналов модулей Промтов 1–4.

Рабочая область: `c:\Users\MSI\Oracle-1001\7000`  
Главный вход: `forecast_ensemble.py` → `output/forecast_dashboard.html`

---

## 1. Что прогнозируем

- **Целевой ряд:** дневной DWT-flow индекс (`features/dwt_flow_timeseries.parquet`, колонка `laden_dwt_sum`, сумма по cargo_class).
- **Горизонт по умолчанию:** T+7 дней.
- **Не прогнозируем «магическую цену» TTF/Brent.** При наличии causal-флагов (Промт 4) в отчёт пишется только *pressure proxy disclaimer* — без числового ценового path.

---

## 2. Признаки (feature stack)

| Источник | Признак | Роль |
|---|---|---|
| Промт 1 | лаги DWT-flow (1,2,3,7,14), roll mean/std | автокорреляция / уровень |
| Промт 2 | `markov_laden_p` (из MC T+7, если forecast.valid) | режим вероятности laden |
| Промт 3 | sin/cos Фурье по *reliable* периодам | сезонность |
| Промт 3 optional | `elliott_flag` | **только** discretionary колонка для важности; `method_type=discretionary_heuristic_not_statistically_validated` |
| Промт 4 | `causal_ttf_any` / `causal_brent_any` | бинарные флаги adjusted-значимого Granger |

Elliott **не** входит в «основной» смысловой драйвер и на дашборде помечен отдельно от Markov/Fourier/Causal.

---

## 3. Модели ансамбля и пороги MIN_N

Пороги задокументированы в коде (`forecast_ensemble.py`) — не «тихие» magic numbers:

| Модель | MIN_N | Зачем такой порог |
|---|---:|---|
| `naive` (baseline) | `horizon + 1` | нужен хотя бы последний факт + горизонт |
| `arima` / SARIMA | **40** | ниже — нестабильные AR/MA и сезонность |
| `gbm` (LightGBM или sklearn HGBR) | **60** | бустинг на коротких панелях легко переобучается |
| `lstm` (optional, torch) | **300** | глубокая сеть на десятках точек почти всегда overfit → **не запускается** |

Если `n` ниже порога метода — модель **отказывается** с явной причиной в `model_notes`, а не «тихо деградирует».

**Baseline обязателен.** Если ML не бьёт naive по MAE на walk-forward, это **явно** показывается (`beats_baseline=false`) и на дашборде подсвечивается.

---

## 4. Валидация

- **Walk-forward с расширяющимся окном** (последние ≤12 origins).
- **Запрещён** случайный K-fold для этого временного ряда (утечка будущего → ложная точность).
- Метрики: **MAE**, **MAPE**, сравнение с naive (`mae_vs_baseline`).

---

## 5. Доверительные интервалы

- Residual **bootstrap** (B=500), центральный интервал 80% (q10–q90).
- **Без** гауссова допущения на приращения (см. Shapiro/JB в `spectral_report.json`).

---

## 6. Ансамбль

Веса ∝ 1/MAE по walk-forward. Модели, проигравшие baseline, получают пониженный вес (×0.5), но naive **не выкидывается** полностью.

---

## 7. Витрина

`output/forecast_dashboard.html` (Apple HIG / `design_system.css`, единый стиль с порталом):

- факт + прогноз + CI-область;
- важность признаков / источники сигналов;
- walk-forward vs baseline;
- **баннер:** «Модель обучена на N наблюдениях, покрытие флота X%» — либо «ПРОГНОЗ НЕ ВЫДАН» при отказе.

Открывать через локальный сервер (`run_server.py`), не `file://`.

---

## 8. Когда не доверять этому прогнозу

1. **Короткая история архива** (≪ 60–90 дней): Фурье/Марков нестабильны; месячные циклы физически не оценимы.
2. **Низкое покрытие MMSI / много `no_signal`:** индекс смещён к судам в зоне terrestrial AIS.
3. **Структурные сдвиги** (санкции, проливы, OPEC+, война премий) — вне признакового пространства.
4. **Elliott-flag** — эвристика, не статистически валидирована.
5. **Causal flags** — «statistically predicts», не физическая причинность.
6. **ML не бьёт naive** на walk-forward — сложная модель не доказала ценность.
7. Прогноз **без** баннера N/coverage на дашборде считать неготовым к использованию.

---

## 9. Запуск

```powershell
cd c:\Users\MSI\Oracle-1001\7000
.\venv\Scripts\python.exe feature_engineering.py
.\venv\Scripts\python.exe markov_model.py
.\venv\Scripts\python.exe spectral_analysis.py
# optional: causal_analysis.py after prices file exists
.\venv\Scripts\python.exe forecast_ensemble.py --horizon 7
.\venv\Scripts\python.exe run_server.py
# http://127.0.0.1:8765/output/forecast_dashboard.html
```

Артефакты: `features/forecast_ensemble_report.json`, `features/forecast_ensemble_series.parquet`, `output/forecast_dashboard.html`.
