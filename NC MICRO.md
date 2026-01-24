# NC MICRO — системное описание (RU)

Этот файл описывает режим **NC MICRO**: где он расположен в проекте, как устроен, какие подсистемы использует и как проходит основной runtime‑поток.

## 1. Назначение и место в приложении

**NC MICRO** — это отдельное окно семейства Lite All Strategy, ориентированное на устойчивую работу при нестабильных источниках данных и с усиленными защитными механизмами (stale‑политики, trade‑gate, crash‑catcher). Режим открывается из диалога выбора режима пары и запускается как самостоятельное окно.

**Точка входа:**

- `PairActionDialog` показывает режим **NC MICRO** среди вариантов.
- `PairModeManager` создаёт `LiteAllStrategyNcMicroWindow` при выборе режима.
- Основной класс окна: `src/gui/lite_all_strategy_nc_micro_window.py`.

## 2. Основные зависимости и сервисы

NC MICRO строится вокруг тех же сервисов, что и остальные режимы Lite, но с отдельным набором защит:

- `Config` и `AppState` — доступ к конфигурации, API‑ключам, пользовательскому состоянию.
- `PriceFeedManager` — подписки на цены и статус WS.
- `BinanceHttpClient` — получение HTTP‑данных (book‑ticker, exchangeInfo, klines).
- `BinanceAccountClient` — аккаунт, балансы, ордера (если доступны ключи).
- `DataCache` — кэш HTTP‑ответов с TTL.
- `FillAccumulator` — накопитель fills для расчёта PnL/комиссий.

## 3. Crash‑catcher и устойчивость

При запуске окна устанавливается crash‑catcher:

- Создаёт лог‑файл `logs/crash/NC_MICRO_<SYMBOL>_<TIMESTAMP>.log`.
- Включает `faulthandler` и периодические дампы stacktrace.
- Записывает unhandled исключения и аварийные падения потоков.

Это позволяет диагностировать нестабильные сценарии, связанные с WS‑flapping или повреждёнными payload.

## 4. Состояния и модели управления

NC MICRO определяет набор доменных моделей и перечислений для управления режимом:

### GridSettingsState
Хранит текущие настройки сетки:

- `budget`, `direction`, `grid_count`, `grid_step_pct`
- `grid_step_mode`, `range_mode`, `range_low_pct`, `range_high_pct`
- `take_profit_pct`, `stop_loss_enabled`, `stop_loss_pct`
- `max_active_orders`, `order_size_mode`

### TradeGate и TradeGateState
Управляют блокировкой live‑действий:

- Отсутствие ключей или read‑only → запрет торговли.
- API‑ошибки или `canTrade=false` → запрет.
- Нет явного подтверждения на live‑торговлю → запрет.

### PilotState / PilotAction / StalePolicy
Контролируют автоматику NC MICRO:

- Состояния: `OFF`, `NORMAL`, `RECENTERING`, `RECOVERY`, `PAUSED_BY_RISK`.
- Действия: `RECENTER`, `RECOVERY`, `FLATTEN_BE`, `FLAG_STALE`, `CANCEL_REPLACE_STALE`.
- Stale‑политики: `NONE`, `RECENTER`, `CANCEL_REPLACE_STALE`.

### ProfitGuardMode и LegacyPolicy
- Profit guard: `BLOCK` или `WARN_ONLY` (минимальная прибыльность).
- Legacy policy: реакция на «старые» ордера (`CANCEL` или `IGNORE`).

## 5. GridEngine и построение планов

`GridEngine` отвечает за построение сетки и планирование ордеров:

- Строит список `GridPlannedOrder` с уровнями, ценами и объёмами.
- Проверяет `minNotional`, `minQty`/`maxQty` и шаги округления.
- Формирует статистику плана (`GridPlanStats`) для аналитики и логирования.

## 6. Источники данных и обработка цены

NC MICRO использует **HTTP‑приоритет** и берёт рыночные данные через:

- `PriceFeedManager` для live‑цен и статуса WS (Connected/Degraded/Lost).
- HTTP book‑ticker/klines для KPI и подтверждения bid/ask.
- `DataCache` для сглаживания нагрузки и контроля TTL.

Контроль stale‑состояний включает:

- таймауты на устаревание bid/ask и цены;
- детект дрейфа сетки относительно market price;
- защитные кулдауны перед повторным авто‑действием.

## 7. Автоматические действия и stale‑политики

NC MICRO поддерживает автоматическую реакцию на устаревание ордеров:

- `FLAG_STALE` — анализ ордеров и логирование кандидатов;
- `RECENTER` — отмена/перестройка сетки вокруг нового якоря;
- `CANCEL_REPLACE_STALE` — отмена устаревших ордеров и размещение обновлённых.

Для безопасности предусмотрены:

- кулдауны на авто‑действия;
- отдельные таймеры обновления снапшотов ордеров;
- блокировка действий при невалидных KPI (spread/volatility/price).

## 8. Торговая безопасность и режимы выполнения

Ключевые защиты режима:

- **HTTP‑only trade source** — стабильное источниковое поведение при WS‑flapping.
- **Dry‑run** — режим по умолчанию для предотвращения случайной торговли.
- **TradeGate** — блокировка действий при отсутствующих ключах, read‑only или ошибках API.
- **Profit guard** — повышение TP/шагов/диапазонов до безопасного минимума.
- **Break‑even guard** — контроль перестроений, чтобы сетка не пересекала цену безубытка.

## 9. Управление ордерами и legacy‑механики

NC MICRO ведёт реестр ордеров и учитывает «legacy» ордера:

- Хранит `bot_order_ids`, `bot_client_ids`, `bot_order_keys`.
- Сопоставляет ордера с уровнями сетки и защищает от дублей.
- Поддерживает bootstrap‑режим: сначала собирает снимок ордеров, затем принимает решения.
- Определяет «owned» ордера и может отменять legacy‑сетки по политике.

## 10. Таймеры, обновления и UI

Внутренние циклы обновления:

- обновление цен и статуса WS;
- refresh балансов, ордеров, fills;
- обновление KPI и вычисление волатильности;
- GC реестров ключей и TTL‑снимков.

UI строится вокруг панелей Lite All Strategy (grid‑настройки, статус торговли, логи и управление), но с отдельным брендингом **NC MICRO** и более жёсткими guards.

## 11. Рекомендуемые точки диагностики

- Логи окна (кнопки/log panel) для трейсинга решений.
- Crash‑логи в `logs/crash/NC_MICRO_*` при неожиданных исключениях.
- Ручной чеклист в `tests/manual/nc_micro_router_test.md` для сценариев с нестабильным WS.

## 12. Краткий flow выполнения

1. Пользователь выбирает режим NC MICRO в диалоге пары.
2. `PairModeManager` создаёт окно `LiteAllStrategyNcMicroWindow`.
3. Окно устанавливает crash‑catcher, подключает сигналы и initial‑state.
4. Данные: `PriceFeedManager` + HTTP book‑ticker → KPI и состояния рынка.
5. Пользователь включает режим (dry‑run или live) → `GridEngine` строит сетку.
6. Ордеры проходят trade‑gate + profit/break‑even guards.
7. Фоны: stale‑детект, авто‑действия, обновление балансов/ордеров, логирование.

