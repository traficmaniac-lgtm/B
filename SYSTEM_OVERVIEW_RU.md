# BBOT Desktop Terminal — системное описание (RU)

Этот файл описывает архитектуру, расположение модулей, назначение каждого элемента и то, как они взаимодействуют. Он предназначен как единая «карта системы» для быстрого ввода в контекст.

## 1. План работы программы (кратко)

1. **Старт приложения**: запускается `src.app.main`, читается `config.json`, поднимается логирование, загружается `AppState` из `config.user.yaml`.
2. **Инициализация GUI**: создаётся `QApplication`, затем `MainWindow`, подключается GUI‑лог‑хендлер и общий exception hook.
3. **Запуск сервисов**: `MainWindow` поднимает ключевые сервисы цен/рынков (PriceFeed/Markets) и менеджер режимов пары.
4. **Экран обзора**: `OverviewTab` загружает список пар через `MarketsService` и подписывается на цены через `PriceFeedManager`.
5. **Выбор режима**: при выборе пары `PairModeManager` открывает нужное окно (Lite Grid, Trade Ready, AI Operator).
6. **Потоки данных**: цены приходят через WS/HTTP, кэшируются `PriceService` и транслируются в UI через `PriceHub`.
7. **AI‑контуры** (опционально): окна с AI собирают datapack, отправляют в `OpenAIClient`, валидируют и применяют ответ.
8. **Runtime** (опционально): торговый runtime запускает `RuntimeEngine`, используя цены и виртуальные ордера.
9. **Завершение**: при закрытии окна останавливаются активные сервисы и освобождаются подписки.

## 2. Точки входа и общий запуск

- **Запуск GUI**: `python -m src.app.main` (см. README/PROJECT_STRUCTURE). Главный путь: `src/app/main.py`.
- **Назначение**: загрузить конфигурацию и пользовательское состояние, поднять главное окно, связать логирование и GUI.

### Стартовая цепочка

1. `src.app.main.main()`:
   - читает конфиг (`src.core.config.load_config`),
   - поднимает логирование (`src.core.logging.configure_logging`),
   - загружает `AppState` из `config.user.yaml`,
   - создаёт `QApplication` и `MainWindow`,
   - подключает лог‑хендлер GUI и exception hook.
2. `MainWindow` формирует весь интерфейс и создаёт системные сервисы (PriceFeedManager, PairModeManager).

## 3. Общая структура директорий

```
src/
  app/        — точка входа GUI
  core/       — конфигурация, модели, утилиты времени, логирование, стратегии
  binance/    — HTTP/WS клиенты + account client
  services/   — сервисы: рынки, цены, кэш, rate limit, price feed
  gui/        — окна/вкладки интерфейса, диалоги, модели состояния UI
  ai/         — схемы и логика AI/оператора
  runtime/    — локальный runtime‑движок и виртуальные ордера
```

## 4. Core: конфигурация, модели и утилиты

### `src/core/config.py`
- **Dataclasses**:
  - `AppConfig` — окружение, уровень логов.
  - `HttpConfig` — таймауты и ретраи.
  - `RateLimitConfig` — лимиты запросов.
  - `PricesConfig` — ttl для цены, интервал обновления, fallback.
  - `BinanceConfig` — base URL, WS URL, recvWindow, API ключи.
  - `Config` — агрегатор конфигурации + `validate()`.
- **Функции**:
  - `load_config()` читает JSON, поверх применяет env vars.
  - `_get_nested/_get_env*()` — утилиты доступа.

### `src/core/models.py`
- `Pair`, `PriceTick`, `ExchangeInfo` — базовые модели домена.

### `src/core/logging.py`
- `configure_logging()` — базовая конфигурация root‑логов.
- `get_logger()` — логгер‑адаптер с extra.

### `src/core/timeutil.py`
- `utc_ms()/monotonic_ms()` — метки времени.
- `is_expired()` — TTL‑проверка.
- `backoff_delay()` — экспоненциальный backoff с джиттером.

### `src/core/symbols.py`
- `sanitize_symbol()`, `validate_trade_symbol()`, `validate_asset()` — нормализация и валидация тикеров.

### `src/core/strategies/*`
- `manual_strategies.py` — ручные стратегии и параметры.
- `manual_runtime.py` — runtime‑логика ручных стратегий.
- `registry.py` — регистрация/поиск стратегий.

## 5. Binance клиенты

### `src/binance/http_client.py`
- `BinanceHttpClient` — HTTP обёртка на httpx.
- Методы: `get_exchange_info`, `get_ticker_price(s)`, `get_klines`, `get_orderbook_depth`, `get_recent_trades`, etc.
- `_request_json()` — общий retry/backoff, обработка статус‑кодов.

### `src/binance/ws_client.py`
- `WsManager` — управляет loop в отдельном потоке.
- `BinanceWsClient` — подписка на `!miniTicker@arr`, парсит сообщения, вызывает `on_tick` и `on_status`.
- Обработка reconnect/backoff.

### `src/binance/account_client.py`
- `BinanceAccountClient` — подписанные запросы (HMAC), операции:
  - `get_account_info/status`, `get_open_orders`, `place_limit_order`, `cancel_order`, `get_trade_fees`, `get_my_trades`.
  - синхронизация времени (`sync_time_offset`).
- `AccountStatus` — результат проверки торговых прав.

## 6. Services: рынки, цены, кэш

### `src/services/markets_service.py`
- `MarketsService` — загрузка списка торговых пар:
  - фильтрация по quote asset, статусу, blacklist, корректность символов.
  - кеширование в `data/exchange_info_*.json`.
  - `load_pairs_cached` / `load_pairs_cached_all` возвращают пары + флаг cache‑fresh.

### `src/services/price_feed_manager.py`
- **Центральный агрегатор цен** (WS + HTTP fallback + микроструктура).
- Основные сущности:
  - `PriceUpdate`, `MicrostructureSnapshot`, `SymbolDataRouter`.
  - Логика маршрутизации источника цены (`decide_price_source`).
- **Механизм подписок**:
  - `register_symbol` / `unregister_symbol` — учёт refcount.
  - `subscribe` / `unsubscribe` — подписки на `PriceUpdate`.
  - `subscribe_status` / `unsubscribe_status` — статус WS по символу.
- **Состояния**: `WS_CONNECTED`, `WS_DEGRADED`, `WS_LOST` + health.
- **Дополнительно**: self‑test / transport test, warmup и анти‑флап логика.

### `src/services/price_feed_service.py`
- `PriceFeedService` — low‑level WS для одного символа.
- Выдаёт `PriceTick`, статусы WS, heartbeat‑монитор.

### `src/services/price_service.py`
- `PriceService` — кэш последних WS‑цен, ttl‑валидация.

### `src/services/price_hub.py`
- `PriceHub` — Qt‑сервис для UI (QTimer); рассылает price snapshot через signal.
- Умеет `register_symbol`/`unregister_symbol`, использует `PriceFeedManager`.

### `src/services/data_cache.py`
- `DataCache` — in‑memory cache (symbol + data_type → data + timestamp).

### `src/services/rate_limiter.py`
- `RateLimiter` — блокировка частых вызовов по ключу.

### `src/services/ai_provider.py`
- Простой stub‑AI: генерирует `StrategyPlan`, умеет `chat_adjustment` + `apply_patch`.

## 7. AI модуль

### `src/ai/models.py`
- Модели аналитики: `AiAnalysisResult`, `AiTradeOption`, `AiActionSuggestion`, `AiStrategyPatch`, `AiResponseEnvelope`.
- `parse_ai_response`, `parse_ai_response_with_fallback`, `fallback_do_not_trade`.

### `src/ai/openai_client.py`
- `OpenAIClient` — асинхронный клиент:
  - `self_check()` — проверка ключа.
  - `analyze_pair()` — JSON‑анализ датапака (AiResponseEnvelope).
  - `analyze_operator()` / `chat_operator()` — JSON‑ответы под AI Operator.

### `src/ai/operator_models.py`
- Описание схемы оператора: `OperatorAIResult`, `StrategyPatch`, `OperatorAIRequestData`.
- Парсер JSON и нормализаторы действий/состояний.

### `src/ai/operator_datapack.py`
- `build_ai_datapack()` — собирает датапак для AI Operator:
  - exchange rules, fees, balances, market data (orderbook, trades, klines), data quality, риск‑ограничения.
  - `estimate_grid_edge()` для оценки edge.

### `src/ai/operator_math.py`
- `estimate_grid_edge()` — расчет net edge и break-even.

### `src/ai/operator_profiles.py`
- `ProfilePreset` и `PROFILE_PRESETS` — профили (CONSERVATIVE/BALANCED/AGGRESSIVE).

### `src/ai/operator_validation.py`
- `validate_strategy_patch()` — проверка патча стратегии на профили/ограничения.

### `src/ai/operator_request_loop.py`
- `run_request_data_loop()` — итерационный запрос доп.данных по просьбе AI.

### `src/ai/operator_runtime.py`
- `cancel_all_bot_orders()` + helper‑обвязки `pause_state` / `stop_state`.

### `src/ai/operator_state_machine.py`
- Логика состояний AI оператора (переходы/решения).

## 8. Runtime (локальная симуляция)

### `src/runtime/virtual_orders.py`
- `VirtualOrder`, `VirtualOrderBook` — создание/отмена/фиксация виртуальных ордеров.

### `src/runtime/strategy_executor.py`
- `StrategyExecutor` — строит grid/range планы, rebuild после fills.
- `StrategyConfig` — минимальная конфигурация стратегии.

### `src/runtime/engine.py`
- `RuntimeEngine` — цикл исполнения:
  - подписки на price feed,
  - управление состояниями RUNNING/PAUSED/STOPPED,
  - расчет PnL, volatility, microstructure, рекомендации для AI observer.

### `src/runtime/runtime_state.py`
- `RuntimeState` — IDLE/RUNNING/PAUSED/STOPPED.

## 9. GUI: окна, диалоги, вкладки

### Главный контейнер

#### `src/gui/main_window.py`
- `MainWindow` — главное окно:
  - создает `PriceFeedManager`, `PairModeManager`, `OverviewTab`, `LogDock`.
  - меню/toolbar Settings, status bar, управление лог‑доком.
  - на закрытие: останавливает OverviewTab, окна, price feed.

### Overview (каталог пар)

#### `src/gui/overview_tab.py`
- `OverviewTab` — главный экран:
  - таблица пар + фильтры (quote, search), статусные бейджи.
  - работает через `MarketsService` (HTTP Binance) и `PriceFeedManager`.
  - обновляет цену только для выбранной строки.
  - поддерживает обновление балансов, статуса аккаунта.
  - double‑click по строке → вызывает `on_open_pair` (PairModeManager).

### Менеджер режимов пары

#### `src/gui/pair_mode_manager.py`
- `PairModeManager` — шлюз открытия окон:
  - `open_pair_dialog()` → `PairActionDialog`.
  - `open_pair_mode()` → окно Lite Grid / Trade Ready / AI Operator Grid.

#### `src/gui/pair_action_dialog.py`
- Диалог выбора режима работы с парой (Lite Mode / AI Operator Grid).

### Lite Grid Terminal

#### `src/gui/lite_grid_window.py`
- Главное окно Lite Grid (базовый grid‑бот):
  - `GridEngine` формирует план ордеров.
  - UI: параметры сетки, статус, таблицы ордеров, logs.
  - Binance API: `BinanceAccountClient` + `BinanceHttpClient`.
  - `PriceFeedManager` для живых цен.
  - `FillAccumulator`, `DataCache`, `TradeGate` (гейт на live).

#### `src/gui/lite_grid_math.py`
- Математика лота и расчетов для grid (используется в LiteGridWindow).

### AI Operator Grid

#### `src/gui/ai_operator_grid_window.py`
- Расширенный режим AI Operator поверх Lite Grid:
  - строит MarketSnapshot, собирает datapack,
  - общается с OpenAI (`OpenAIClient`), валидирует patch,
  - поддерживает apply patch / request data / start/stop/pause,
  - хранит историю AI ответов и использует `RateLimiter`/`DataCache`.

### Trade Ready Mode + Runtime

#### `src/gui/trade_ready_mode_window.py`
- Trade Ready Mode:
  - показывает market context, AI отчёт, варианты сделок.
  - создаёт `TradingRuntimeWindow` по выбранному варианту.
  - слушает `PriceFeedManager` для live‑цены.

#### `src/gui/trading_runtime_window.py`
- Runtime UI:
  - интерфейс к `RuntimeEngine`, подписка на цены и events.
  - панели: ордера, fills, PnL, рекомендации, чаты/логи.
  - кнопки Start/Pause/Stop/Emergency.

### Pair Workspace (старый режим)

#### `src/gui/pair_workspace_window.py`
- Простая версия Pair Workspace:
  - `AIProvider` stub, имитация prepare/analyze/apply.
  - Отдельный chat dock и набор вкладок.

#### `src/gui/pair_workspace_tab.py`
- Расширенная версия Pair Workspace для вкладки:
  - Управляет состоянием `PairState` и `PairWorkspaceState`.
  - Готовит datapack, запускает AI через `OpenAIClient`.
  - Ведёт статус и логи, открывает `TradingWorkspaceWindow`.

#### `src/gui/trading_workspace_window.py`
- Trading Workspace:
  - мониторинг активной стратегии,
  - AI observer циклы, кнопки approve для действий.

### Settings

#### `src/gui/settings_dialog.py`
- Модальный диалог настроек: ключи Binance/OpenAI, периоды, TTL, quote и т.д.

#### `src/gui/i18n.py`
- Помощники локализации и текстовых ресурсов.

#### `src/gui/models/*`
- `AppState`, `PairState`, `PairWorkspaceState`, `MarketState`, `PairMode` — контейнеры UI‑состояний.

### GUI Widgets и legacy‑вкладки

#### `src/gui/widgets/*`
- `LogDock` — GUI‑лог‑док с Qt‑хендлером.
- `PairTopBar` — верхняя панель действий в Pair Workspace.
- `PairLogsPanel` — простая панель логов.
- `DashboardTab`, `MarketsTab`, `BotTab`, `SettingsTab` — исторические вкладки (плейсхолдеры/демо), сейчас используются как reference.

## 10. Взаимодействия и потоки данных

### Основные потоки

1. **Конфиг и состояние пользователя**:
   - `load_config()` → `AppState.load()` → настройки в GUI.
2. **Список пар**:
   - `OverviewTab` → `MarketsService` → Binance HTTP → кэш → таблица.
3. **Цены**:
   - все окна подписываются на `PriceFeedManager` (WS + HTTP fallback).
   - `OverviewTab` и `TradeReadyMode` слушают обновления для UI.
4. **Запуск режима**:
   - `OverviewTab` (двойной клик) → `PairModeManager` → окно режима (Lite Grid / AI Operator / Trade Ready).
5. **AI анализ**:
   - Pair Workspace / AI Operator Grid собирает datapack → `OpenAIClient` → парсинг JSON → UI.
6. **Runtime**:
   - `TradingRuntimeWindow` управляет `RuntimeEngine`.
   - RuntimeEngine использует `PriceFeedManager` и `VirtualOrderBook`.

### Логирование

- Все компоненты используют `get_logger()`.
- `MainWindow` подключает `LogDock.handler` к root‑логгеру.

## 11. Тесты

- `tests/test_app_state.py` — проверка сохранения/загрузки AppState.
- `tests/test_imports.py` — smoke‑imports GUI.
- `tests/test_markets_service.py` — тесты MarketsService.

## 12. Примечания

- В проекте есть несколько UI‑веток (Lite Grid, Pair Workspace, Trade Ready, AI Operator). Это связано со стадиями развития и экспериментами.
- Реальные сделки не запускаются автоматически: многие режимы содержат confirm/approve шаги и работают в dry‑run.

---

Если нужно сделать ещё более подробную карту (например, расписать каждый метод), сообщите, какие модули важнее всего, и я расширю этот файл.
