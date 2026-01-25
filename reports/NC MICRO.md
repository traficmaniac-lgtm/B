# NC MICRO — системное описание (RU)

Этот файл описывает режим **NC MICRO**: где он расположен в проекте, как устроен, какие подсистемы использует и как проходит основной runtime‑поток.

## 1. Назначение и место в приложении

**NC MICRO** — отдельное окно семейства Lite All Strategy, ориентированное на устойчивую работу при нестабильных источниках данных и с усиленными защитными механизмами (stale‑политики, trade‑gate, crash‑catcher, дедупликация событий, ограничители refresh). Режим открывается из диалога выбора режима пары и запускается как самостоятельное окно.

**Точка входа:**

- `PairActionDialog` показывает режим **NC MICRO** среди вариантов.
- `PairModeManager` создаёт `LiteAllStrategyNcMicroWindow` при выборе режима.
- Основной класс окна: `src/gui/lite_all_strategy_nc_micro_window.py`.

## 2. Основные зависимости и сервисы

NC MICRO использует общий стек Lite‑режимов, но дополняется специализированными защитными модулями и состояниями:

- `Config` и `AppState` — доступ к конфигурации, ключам, пользовательскому состоянию.
- `PriceFeedManager` — подписки на цены и статус WS (Connected/Degraded/Lost).
- `BinanceHttpClient` — book‑ticker, exchangeInfo, klines.
- `BinanceAccountClient` — балансы, ордера, торговые проверки (если ключи доступны).
- `DataCache` — кэш HTTP‑ответов с TTL.
- `NetWorker` — фоновые HTTP‑задачи с очередью, приоритетами и дедупликацией запросов.
- `FillAccumulator` — накопитель fills для расчёта PnL/комиссий.
- `cancel_reconcile` — управление отменой ордеров с reconcile/poll‑циклами.
- `micro_edge` — вычисление edge‑метрик (edge_raw/expected_profit) для thin‑edge защиты.
- `nc_micro_exec_dedup` — дедупликация trade_id и контроль суммарных fill‑объёмов.
- `nc_micro_refresh` — правила разрешения stale‑refresh и лог‑лимитер.
- `nc_micro_stop` — финализация stop‑состояний с единым логированием.

## 3. Сессионное состояние NC MICRO

Состояние режима хранится в `NcMicroSession` и разделено на несколько компактных блоков данных:

- **GridSettingsState** — базовые параметры сетки (`budget`, `direction`, `grid_count`, `grid_step_pct`, `grid_step_mode`, `range_mode`, `range_low_pct`, `range_high_pct`, `take_profit_pct`, `stop_loss_enabled`, `stop_loss_pct`, `max_active_orders`, `order_size_mode`).
- **MarketDataCache** — локальный снимок bid/ask/last, возраст данных (`age_ms`) и источник (`source`) для тонких защит (thin‑edge/stale).
- **NcMicroRuntimeState** — runtime‑метки (состояние engine, stop‑флаги, времена последнего polling/UI‑обновления, минутные отчёты).
- **OrderTrackingState** — реестр ордеров, map‑представления, множества ключей, а также `TradeIdDeduper`/`ExecKeyDeduper`/`CumulativeFillTracker` для дедупликации событий.
- **NcMicroMinuteCounters** — минутные счётчики (`exec_dup`, `stale_poll_skips`, `kpi_updates`, `fills`) и их сброс.

Сессия используется как централизованный контейнер для данных, чтобы связывать UI, сетевые обновления и guard‑логику в единый runtime‑поток.

## 4. Crash‑catcher и устойчивость

При запуске окна устанавливается crash‑catcher:

- Создаёт лог‑файл `logs/crash/NC_MICRO_<SYMBOL>_<TIMESTAMP>.log`.
- Включает `faulthandler` и периодические дампы stacktrace.
- Ловит unhandled исключения из главного потока и worker‑потоков.

Это позволяет диагностировать ошибки при WS‑flapping, повреждённых payload и race‑сценариях в refresh/stop‑циклах.

## 5. Состояния и модели управления

NC MICRO определяет набор доменных моделей и перечислений для управления режимом:

### GridSettingsState
Хранит текущие настройки сетки:

- `budget`, `direction`, `grid_count`, `grid_step_pct`
- `grid_step_mode`, `range_mode`, `range_low_pct`, `range_high_pct`
- `take_profit_pct`, `stop_loss_enabled`, `stop_loss_pct`
- `max_active_orders`, `order_size_mode`

### TradeGate / TradeGateState
Управляют блокировкой live‑действий:

- отсутствие ключей или read‑only → запрет торговли;
- API‑ошибки или `canTrade=false` → запрет;
- нет подтверждения live‑торговли → запрет.

### PilotState / PilotAction / StalePolicy
Состояния и действия пилота NC MICRO:

- Состояния: `OFF`, `NORMAL`, `HOLD`, `ACCUMULATE_BASE`.
- Действия: `RECENTER`, `RECOVERY`, `FLATTEN_BE`, `FLAG_STALE`, `CANCEL_REPLACE_STALE`.
- Stale‑политики: `NONE`, `RECENTER`, `CANCEL_REPLACE_STALE`.

### ProfitGuardMode и LegacyPolicy
- Profit guard: `BLOCK` или `WARN_ONLY` (минимальная прибыльность/edge‑guard).
- Legacy policy: реакция на «старые» ордера (`CANCEL` или `IGNORE`).

## 6. MarketHealth и thin‑edge контроль

NC MICRO строит `MarketHealth` из цен и KPI:

- `spread_bps`, `vol_bps`, `age_ms`, `src`.
- `edge_raw_bps` и `expected_profit_bps` на базе `micro_edge`.

Если `expected_profit_bps` ниже порога, режим активирует thin‑edge защиту: пилот уходит в `HOLD`, блокирует `RECENTER`/`CANCEL_REPLACE_STALE` и может смещать якорь для консервативной перестройки.

## 7. GridEngine и построение планов

`GridEngine` отвечает за построение сетки и планирование ордеров:

- Строит список `GridPlannedOrder` с уровнями, ценами и объёмами.
- Проверяет `minNotional`, `minQty`/`maxQty` и шаги округления.
- Формирует статистику плана (`GridPlanStats`) для аналитики и логирования.

## 8. Источники данных и обработка цены

NC MICRO использует **HTTP‑приоритет** и берёт рыночные данные через:

- `PriceFeedManager` для live‑цен и статуса WS.
- HTTP book‑ticker/klines для KPI и подтверждения bid/ask.
- `DataCache` для сглаживания нагрузки и контроля TTL.

Контроль stale‑состояний включает:

- таймауты на устаревание bid/ask и цены;
- детект дрейфа сетки относительно market price;
- защитные кулдауны перед повторным авто‑действием.

## 9. Stale‑refresh и обновление ордеров

NC MICRO жёстко лимитирует refresh‑цикл ордеров:

- `compute_refresh_allowed` оценивает частоту обновлений, hard‑TTL, dirty‑состояние registry, изменение хэша и число «стабильных» циклов.
- `StaleRefreshLogLimiter` подавляет шум в логах и пишет сообщения только при изменении состояния или истечении интервала.
- Дополнительная защита: refresh может быть подавлен, если не критичен и идёт другой inflight‑процесс.

## 10. Дедупликация fills и повторов торговых событий

Для защиты от повторной обработки:

- `TradeIdDeduper` отслеживает trade_id с TTL и максимальным размером.
- `CumulativeFillTracker` корректирует cumulative fills, возвращая только дельту.
- `should_block_new_orders` блокирует отправку новых ордеров при состояниях STOPPING/STOPPED.

Дополнительно используется `ExecKeyDeduper` для защиты от повторов по ключам исполнения, когда trade_id отсутствует или нестабилен.

## 11. Торговая безопасность и режимы выполнения

Ключевые защиты режима:

- **HTTP‑only trade source** — стабильное поведение при WS‑flapping.
- **Dry‑run** — режим по умолчанию для предотвращения случайной торговли.
- **TradeGate** — блокировка действий при отсутствующих ключах, read‑only или ошибках API.
- **Profit guard** — повышение TP/шагов/диапазонов до безопасного минимума с учётом комиссий/волатильности.
- **Break‑even guard** — контроль перестроений, чтобы сетка не пересекала цену безубытка.
- **Stop‑flow** — унифицированная финализация через `finalize_stop_state` + контроль refresh.

## 12. Управление ордерами, cancel/reconcile и legacy‑механики

NC MICRO ведёт реестр ордеров и учитывает «legacy» ордера:

- Хранит `bot_order_ids`, `bot_client_ids`, `bot_order_keys`.
- Сопоставляет ордера с уровнями сетки и защищает от дублей.
- Ведёт активные registry‑ключи и синхронизирует их со снимком open_orders.
- Поддерживает bootstrap‑режим: сначала собирает снимок ордеров, затем принимает решения.
- Отмена ордеров использует `cancel_reconcile`: повторные попытки, reconcile‑петли и лог‑детализацию.

## 13. Таймеры, обновления и UI

Внутренние циклы обновления:

- обновление цен и статуса WS;
- refresh балансов, ордеров, fills;
- обновление KPI, волатильности и thin‑edge метрик;
- GC реестров ключей и TTL‑снимков.

UI строится вокруг панелей Lite All Strategy (grid‑настройки, статус торговли, логи и управление), но с отдельным брендингом **NC MICRO**. Дополнительно используется диалог `PilotSettingsDialog` для конфигурации пилота/guard‑настроек.

## 14. Сетевой слой (NetWorker)

NC MICRO использует фоновый worker для сетевых задач:

- Очередь с приоритетами (HIGH/MED/LOW/LOWEST) для разного типа запросов.
- Dedup‑механизм на уровне ключей, чтобы не дублировать book‑ticker/статусные вызовы.
- Rate‑limit при отправке частых задач и защита от повторной подачи при закрытии окна.
- Синхронный `call()` поддерживает вызов сетевой функции и возврат результата в UI‑потоке с таймаутом.

## 15. Рекомендуемые точки диагностики

- Логи окна (log panel) для трейсинга решений и refresh‑причин.
- Crash‑логи в `logs/crash/NC_MICRO_*` при неожиданных исключениях.
- Ручной чеклист в `tests/manual/nc_micro_router_test.md` для сценариев с нестабильным WS.

## 16. Краткий flow выполнения

1. Пользователь выбирает режим NC MICRO в диалоге пары.
2. `PairModeManager` создаёт окно `LiteAllStrategyNcMicroWindow`.
3. Окно устанавливает crash‑catcher, подключает сигналы и initial‑state.
4. Данные: `PriceFeedManager` + HTTP book‑ticker → KPI/MarketHealth.
5. Пользователь включает режим (dry‑run или live) → `GridEngine` строит сетку.
6. Ордеры проходят trade‑gate + profit/break‑even/thin‑edge guards.
7. Фоны: stale‑детект, refresh‑лимиты, дедуп fills, stop‑циклы, логирование.
