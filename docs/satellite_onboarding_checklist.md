# Satellite AIS — что запросить у вендора

Канал остаётся `parked`, пока нет пары `SAT_PROVIDER` + `SATELLITE_API_KEY` и базового URL.
Код не содержит адреса вендора и не делает сетевых вызовов в этом состоянии.
`source=satellite_ais` пишется только по точке с широтой, долготой и provenance
(`provider` + `observed_at`). Пропуск без такой точки не создаёт позицию.

Допустимые провайдеры: `spire`, `unseenlabs`, `iceye`.

## Спросить до подписания

1. **Endpoint.** Базовый URL и путь позиций. Его потом кладут в `SAT_BASE_URL`. Адрес в репозиторий не вписывается заранее.
2. **Формат.** Один из уже разобранных конвертов: Spire `data[]` (`latitude` / `longitude` / `imo` / `timestamp`), Unseenlabs `vessels[]` (`lat` / `lon` / `imo` / `observed_at`), ICEYE GeoJSON `features[]` (`properties.imo`, `geometry.coordinates`, `properties.observed_at`). Время наблюдения обязательно: без него точка не становится `satellite_ais`.
3. **Лимиты.** Суточная квота и месячный потолок. Суточный потолок задаётся `SAT_DAILY_CAP` (пока в контракте кода стоит 50). Аллокатор берёт сначала gap-суда из ТОП-500, затем остальные gap, и останавливается на квоте.
4. **Регион вызовов.** Узел Korolev (`45.8.230.214`) гео-блокируется частью API (для Anthropic это HTTP 403 `request_not_allowed`). До оплаты нужно письменно подтвердить, что API отвечает с этого адреса, либо назвать регион, откуда вызов разрешён. Если Korolev закрыт, канал не включают «вслепую»: сначала согласованный регион исходящих вызовов.

## Две команды после подписания

Ключ лежит в `secrets/satellite.key` (файл не коммитится). `SAT_PROVIDER` — имя из контракта.

```bash
SAT_PROVIDER=spire bash scripts/install_key.sh SATELLITE --from-file secrets/satellite.key
bash scripts/install_key.sh SATELLITE --probe-only
```

Первая команда пишет ключ и `SAT_PROVIDER` в локальный `.env`, на Korolev, в `runtime_env` и ставит сигнал watcher. Вторая печатает `active` или `parked` и маску ключа. `SAT_BASE_URL` и `SAT_DAILY_CAP` задаются в `.env` значениями из договора; probe читает свежий `runtime_env`, а не окружение образа.
