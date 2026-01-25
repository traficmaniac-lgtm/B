# BBOT Desktop Terminal — системное описание (RU)

Этот файл описывает архитектуру, расположение модулей, назначение каждого элемента и то, как они взаимодействуют. Он служит «картой системы» для быстрого онбординга и навигации по коду.

## 1. Цели и общая логика

BBOT Desktop Terminal — настольный GUI‑терминал для работы с рынками Binance: обзор пар, лайв‑цены, экспериментальные grid‑режимы, режимы AI‑анализа и локальный runtime для симуляций, включая отдельный режим NC MICRO. Центральный поток приложения:

1. Запуск GUI и загрузка конфигурации.
2. Инициализация сервисов цен/рынков.
3. Взаимодействие пользователя с Overview и выбор режима для конкретной пары.
4. Опциональные режимы (Lite Grid, Lite All Strategy, ALGO PILOT v1.6.5, NC MICRO, AI‑режимы, Trade Ready/Runtime).

## 2. Точки входа и запуск

- **GUI**: `python -m src.app.main` (главный путь `src/app/main.py`).
- **Назначение**: прочитать runtime‑конфиг (ожидаемый `config.json`), загрузить пользовательское состояние (`config.user.yaml`), поднять окно `MainWindow`, подключить логирование/GUI‑хендлеры.

### Стартовая цепочка (упрощённо)

1. `src.app.main.main()`:
   - читает конфиг через `src.core.config.load_config`,
   - поднимает логирование (`src.core.logging.configure_logging`),
   - загружает `AppState` из `config.user.yaml`,
   - создаёт `QApplication` и `MainWindow`.
2. `MainWindow` инициализирует `PriceFeedManager`, `PairModeManager`, вкладки и лог‑док.

## 3. Конфигурация и runtime‑артефакты

- `config.json` — runtime‑конфиг (env, лимиты, URL‑адреса, таймауты). Не хранится в репозитории.
- `config.user.yaml` — пользовательское состояние GUI (ключи, дефолты). Создаётся при первом запуске.
- `data/exchange_info_*.json` — кэш Binance `exchangeInfo` (создаётся/обновляется `MarketsService`).

Дополнительно:

- `src/config/fee_overrides.py` хранит таблицы override‑комиссий для guard‑логики.

## 4. Структура директорий (коротко)

```
src/
  app/        — запуск GUI
  core/       — конфигурация, модели, логирование, стратегии, cancel/reconcile, NC MICRO helpers
  binance/    — HTTP/WS/Account клиенты
  services/   — рынки, цены, кэши, rate‑limiter, nc_micro session, net worker
    nc_micro/ — контейнеры состояния NC MICRO (session)
    net/      — фоновый NetWorker для HTTP‑задач
  gui/        — окна/вкладки/диалоги/модели UI (Lite, Lite All Strategy, ALGO PILOT, NC MICRO)
  ai/         — AI‑схемы и OpenAI‑клиент
  runtime/    — локальный runtime и виртуальные ордера
```

## 5. Core: конфигурация, модели, утилиты

### `src/core/config.py`
- Конфиги: `AppConfig`, `HttpConfig`, `RateLimitConfig`, `PricesConfig`, `BinanceConfig`, `Config`.
- `load_config()` — чтение JSON + применение переменных окружения.
- `Config.validate()` — проверка полноты и диапазонов.

### `src/core/models.py`
- Базовые модели домена: `Pair`, `PriceTick`, `ExchangeInfo`.

### `src/core/logging.py`
- `configure_logging()` и `get_logger()`.

### `src/core/timeutil.py`
- Временные утилиты (UTC/monotonic), TTL‑проверки, экспоненциальный backoff.

### `src/core/cancel_reconcile.py`
- `cancel_all_open_orders()` — отмена ордеров с polling.
- `cancel_all_with_reconcile()` — повторные попытки cancel + reconcile‑петля.

### `src/core/micro_edge.py`
- `compute_expected_edge_bps()` — расчёт edge/expected profit (basis points) для thin‑edge защиты.

### `src/core/nc_micro_*`
- `nc_micro_exec_dedup.py` — дедупликация trade_id и учёт cumulative fills.
- `nc_micro_refresh.py` — вычисление допустимости refresh + log‑лимитер.
- `nc_micro_stop.py` — финализация stop‑состояний с единым логированием.

### `src/core/strategies/*`
- `manual_strategies.py`, `manual_runtime.py`, `registry.py` — ручные стратегии и их регистрация.

## 6. Binance клиенты

### `src/binance/http_client.py`
- `BinanceHttpClient` — HTTP‑обёртка на httpx, методы получения `exchangeInfo`, `ticker`, `klines`, `orderbook`, `trades`.
- `_request_json()` — retry/backoff и статус‑контроль.

### `src/binance/ws_client.py`
- `WsManager` и `BinanceWsClient` — WS‑подписки на цены, reconnect и backoff.

### `src/binance/account_client.py`
- `BinanceAccountClient` — HMAC‑запросы, аккаунт, ордера, комиссии.
- `AccountStatus` — статус доступа/торговых прав.

## 7. Services: рынки, цены, кэш

### `src/services/markets_service.py`
- Загружает список пар, фильтрует по quote/статусу и поддерживает кэш `exchange_info_*.json`.

### `src/services/price_feed_manager.py`
- Агрегатор цен (WS + HTTP fallback), статус WS (`WS_CONNECTED/WS_DEGRADED/WS_LOST`).
- Управляет подписками (`register_symbol`, `subscribe`, `subscribe_status`) и отдаёт `PriceUpdate`.

### `src/services/price_feed_service.py`
- Пер‑символьный сервис feed‑запросов, обслуживающий конкретную пару.
- Используется `PriceFeedManager` для распределения подписок и статусов.

### `src/services/price_hub.py`
- Qt‑hub для UI: QTimer + signals, рассылка снапшотов цен.

### `src/services/price_service.py`
- Кэш последних цен + TTL‑проверки.

### `src/services/data_cache.py`
- In‑memory кэш (symbol/data_type → data + timestamp).

### `src/services/rate_limiter.py`
- Rate‑limit по ключам.

### `src/services/nc_micro/session.py`
- Контейнеры состояния NC MICRO: сеточные настройки, cache market‑данных, runtime‑метки, dedup‑состояние.

### `src/services/net/net_worker.py`
- Фоновый HTTP‑worker: очередь задач, приоритеты, dedup по ключу, rate‑limit и блокировки.
- Варианты вызовов: `submit()` (async) и `call()` (sync с таймаутом).

## 8. GUI: основные окна и режимы

### MainWindow и Overview

- `src/gui/main_window.py` — главный контейнер GUI: создаёт `PriceFeedManager`, `PairModeManager`, вкладки, лог‑док.
- `src/gui/overview_tab.py` — каталог пар: загрузка через `MarketsService`, фильтры, выбор пар.

### PairModeManager + PairActionDialog

- `src/gui/pair_action_dialog.py` — карточки режимов для выбранной пары (Lite Grid, Lite All Strategy, ALGO PILOT, NC MICRO).
- `src/gui/pair_mode_manager.py` — открытие окон для выбранного режима.

### Lite Grid (legacy)

- `src/gui/lite_grid_window.py` — классический grid‑режим, live‑цены и торговые операции через Binance clients.
- `src/gui/lite_grid_math.py` — математика лотов, сетки, утилиты расчётов.

### Lite All Strategy Terminal (v1.0)

- `src/gui/lite_all_strategy_terminal_window.py` — расширенный Lite‑режим для тестирования сеточных стратегий и торговых гейтов.

### Lite All Strategy — ALGO PILOT (v1.6.5)

- `src/gui/lite_all_strategy_algo_pilot_window.py` — отдельное окно с панелью ALGO PILOT.
- Объединяет:
  - **Market KPI panel** с ценой/спредом/волатильностью/комиссией и источником данных.
  - **Grid settings panel** (budget, direction, grid count, шаг сетки, диапазоны, TP/SL, max orders).
  - **Runtime panel** (балансы, PnL, список ордеров, отмена/refresh).
  - **ALGO PILOT panel** с метриками (якорь, отклонение, PnL, устаревшие ордера) и кнопками действий.

### Lite All Strategy — NC MICRO

- `src/gui/lite_all_strategy_nc_micro_window.py` — окно NC MICRO с HTTP‑priority источниками, защитными проверками и детальными refresh‑ограничителями.
- Crash‑catcher пишет в `logs/crash/NC_MICRO_*`, ловит unhandled исключения в потоках.
- Trade‑gate, profit/break‑even/edge‑guards блокируют опасные действия.
- Stale‑refresh основан на hash‑контроле, hard‑TTL и подавлении спама логов.
- Дедупликация fills/trade_id защищает от повторной обработки.
- Сессионное состояние хранится в `NcMicroSession` (grid‑настройки, кэш market‑данных, runtime‑счётчики и dedup‑контейнеры).
- HTTP‑задачи отправляются через `NetWorker` (очередь и дедуп вызовов).

### AI modes

- `src/gui/modes/ai_full_strateg_v2` — отдельный UI‑пакет для AI full strategy (controller + window).
- `src/gui/ai_operator_grid_window.py` и workspace окна — экспериментальные AI‑панели.

## 9. AI модуль

### `src/ai/models.py`
- Модели AI‑ответов и парсинг JSON.

### `src/ai/openai_client.py`
- Асинхронный клиент OpenAI, self‑check и анализ датапаков.

### `src/ai/operator_*`
- Схемы AI‑оператора, сбор datapack, проверки и state‑machine.

## 10. Runtime (локальная симуляция)

- `src/runtime/engine.py` — `RuntimeEngine` для симуляций и рекомендаций.
- `src/runtime/virtual_orders.py` — локальные ордера, фиксация fills.
- `src/runtime/strategy_executor.py` — генерация планов сеток и перестроение после fills.

## 11. Потоки данных

1. **Конфиг**: `load_config()` → `AppState.load()` → настройки GUI.
2. **Список пар**: `OverviewTab` → `MarketsService` → Binance HTTP → кэш → таблица.
3. **Цены**: `PriceFeedManager` (WS/HTTP) → `PriceFeedService` → сигналы → UI.
4. **Режимы пары**: `PairModeManager` → окно режима (Lite/Lite All Strategy/ALGO PILOT/NC MICRO).
5. **ALGO PILOT**:
   - обновляет KPI (спред, волатильность, источник),
   - строит сетку через GridEngine и применяет profit/break‑even guards,
   - управляет ордерами, stale‑детектором и безопасностью через TradeGate.
6. **NC MICRO**:
   - берёт цены из `PriceFeedManager`, HTTP book‑ticker и кэширует их через `DataCache`,
   - контролирует thin‑edge/volatility, блокирует действия при рисках,
   - управляет stale‑refresh через `nc_micro_refresh` и dedup для fills.
   - асинхронные HTTP‑задачи идут через `NetWorker` с приоритетами и dedup‑ключами.
7. **AI режимы**: Pair Workspace/AI Operator собирает datapack → `OpenAIClient` → JSON → UI.

## 12. Тесты

- `tests/test_app_state.py` — сохранение/загрузка `AppState`.
- `tests/test_imports.py` — smoke‑imports GUI.
- `tests/test_markets_service.py` — проверки `MarketsService`.

## 13. Ограничения и примечания

- `services/ai_provider.py` — stub AI, не вызывает внешние API.
- Реальная торговля требует ключей Binance и подтверждений; большинство действий остаются в dry‑run, пока не подтверждены.
- Режимы (Lite Grid, Lite All Strategy, ALGO PILOT, Pair Workspace) — разные ветки UI одного проекта, часть из них экспериментальна.

## 14. Документация (reports/)

Все отчёты и расширенные описания собраны в папке `reports/`. В неё вынесены:

- обзорные материалы по ALGO PILOT и NC MICRO,
- общий системный обзор,
- пакет «NC Pilot Multi‑Pair» — набор детальных документов о мультипарной логике и контуре исполнения.

Для удобства навигации по мультипарному сценарию NC Pilot добавлены отдельные справочники:

- **NC_PILOT_MULTI_PAIR_OVERVIEW.md** — стратегия, позиционирование режима и связи с существующими модулями.
- **NC_PILOT_MULTI_PAIR_ARCHITECTURE.md** — компоненты, зоны ответственности и обмен данными.
- **NC_PILOT_MULTI_PAIR_RUNTIME.md** — планировщик, петли исполнения, шаблоны восстановления.
- **NC_PILOT_MULTI_PAIR_RISK_CONTROLS.md** — гейт‑логика, лимиты, аварийные выходы.
- **NC_PILOT_MULTI_PAIR_2LEG_REBAL_LOOP_SPREAD.md** — подробная схема 2‑leg rebalancing loop spread.

## 6. Сервисы рынков и цен

### `src/services/markets_service.py`

- Загружает `exchangeInfo` и хранит кэш символов.
- Формирует список торговых пар для Overview.
- Управляет частотой обновления и логирует время ответа.

### `src/services/price_feed_manager.py`

- Подписки на цены и статусы WS.
- Управляет fallback HTTP при деградации WS.
- Хранит возраст данных и источник (`WS/HTTP`).

## 7. GUI‑модули и ключевые окна

- `MainWindow` — основной контейнер UI.
- `PairActionDialog` — выбор режима для пары.
- `LiteAllStrategy*` окна — Lite Grid / ALGO PILOT / NC MICRO.
- `TradeReadyWindow` / `TradingRuntimeWindow` — режимы AI/Runtime.

## 8. Логи и артефакты

- `logs/app.log` — общий лог приложения.
- `logs/crash/*` — crash‑дампы окон.
- `data/exchange_info_*.json` — кэш биржевой информации.

## 9. Поток данных (концептуально)

```
UI -> PairModeManager -> (Window) -> PriceFeedManager -> NetWorker -> Binance API
```

- UI инициирует действия через окно режима.
- NetWorker обеспечивает последовательность и dedup запросов.
- Ответы обновляют локальные модели и UI‑панели.

## 10. Практические подсказки

- При нестабильном WS включать fallback‑поллинг (HTTP) и следить за `age_ms`.
- При отсутствии bid/ask необходимо проверять корректность символа и доступность book‑ticker.
- Любые изменения в guard‑логике фиксировать в `reports/REPORT.md`.
