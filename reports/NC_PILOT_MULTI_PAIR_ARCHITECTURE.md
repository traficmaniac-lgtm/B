# NC Pilot Multi‑Pair — архитектура (RU)

## 1. Архитектурная карта

Мультипарный контур описывается как набор сервисов/узлов, интегрированных поверх существующего рантайма:

```
[Market Data] -> [Pair Scanner] -> [Cluster Builder] -> [Signal Engine]
                                         |                |
                                         v                v
                                   [Risk Engine] <- [State Store]
                                         |
                                         v
                                 [Execution Orchestrator]
                                         |
                                         v
                                  [Order Router/NetWorker]
```

## 2. Основные компоненты

### 2.1 Market Data Layer

**Задача:** обеспечить единый слой доступа к данным (ticks, orderbook, spreads).

- Использует `PriceFeedManager` и `PriceFeedService`.
- Для HTTP‑fallback опирается на `NetWorker` + `DataCache`.
- Накапливает window‑метрики: волатильность, средний спред, скорость обновления.

### 2.2 Pair Scanner

**Задача:** отобрать кандидаты в мультипарный режим.

- Фильтры ликвидности (min volume / min depth).
- Проверка свежести данных (price TTL).
- Условие допущения по корреляции и штрафы за аномальные спреды.

### 2.3 Cluster Builder

**Задача:** сформировать кластеры и связки для хеджа.

- Подбирает пары к ведущей по корреляции/спреду.
- Ограничивает размер кластера.
- Формирует `cluster_id`, `primary_pair`, `secondary_pairs`.

### 2.4 Signal Engine

**Задача:** решить, активировать ли пары и какие действия выполнить.

- Сигналы входят из KPI и контроля качества данных.
- Выдаёт `intent` на каждую пару: `build_grid`, `rebalance`, `pause`, `cancel`.
- Вычисляет целевой hedge ratio и запрашивает Risk Engine.

### 2.5 Risk Engine

**Задача:** блокировать рискованные действия.

- Проверяет лимиты по экспозиции, drawdown, волатильности.
- Использует guard‑политику из NC MICRO (thin edge / stale / break‑even).
- Возвращает `guard_status` и список причин блокировки.

### 2.6 State Store

**Задача:** хранение состояния на уровне пары и кластера.

- Пер‑pair: сетка, активные ордера, last fill, last refresh, local PnL.
- Пер‑cluster: агрегированная экспозиция, hedge ratio, system flags.
- Должен быть сериализуемым для восстановления.

### 2.7 Execution Orchestrator

**Задача:** синхронизировать действия по парам.

- Учитывает последовательность: Primary → Secondary.
- Применяет soft‑locks, чтобы избежать гонок.
- Планирует выполнение через очереди `NetWorker`.

### 2.8 Order Router

**Задача:** отправка ордеров в внешнюю систему.

- Делегирует торговые операции клиентам Binance.
- Следит за дедупом запросов, обновляет состояние.

## 3. Взаимодействие с существующими модулями

- **NC MICRO**: политика `refresh gating`, `dedup`, `profit guards`.
- **ALGO PILOT**: KPI‑панели и расчёт сетки.
- **Core utilities**: `micro_edge`, `timeutil`, `symbols`.

## 4. Потоки данных (sequence)

1. **Scan**: Pair Scanner собирает кандидатов из `MarketsService`.
2. **Cluster**: Cluster Builder формирует связки и роли.
3. **Signal**: Signal Engine подготавливает intent.
4. **Risk**: Risk Engine фильтрует intent.
5. **Execute**: Orchestrator распределяет задачи по парам.
6. **Update**: State Store фиксирует результат и KPI.

## 5. Состояния пары (state machine)

- `IDLE` → `CANDIDATE` → `ACTIVE` → `REBALANCE` → `COOLDOWN` → `PAUSED`.
- Переходы разрешены только при `guard_status = OK`.
- При `ALERT` или `ERROR` — переход в `PAUSED` и отмена активных ордеров.

## 6. Синхронизация в кластере

- Ведущая пара формирует основу экспозиции.
- Ведомая пара может выполнять ребаланс только после подтверждения заявки ведущей.
- Система блокирует несовместимые шаги, если одна из legs не подтверждена.

## 7. Отказоустойчивость

- Все действия логируются (intent + guard reasons).
- Прерывание network‑канала вызывает `degraded` состояние и приоритетную отмену.
- State Store хранит checkpoints после каждого цикла исполнения.

## 8. Расширение

- Возможны доп. роли: `liquidity booster`, `neutralizer`.
- Поддержка альтернативных источников данных (index price, funding).
