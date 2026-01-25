# NC Pilot Multi‑Pair — runtime и петли исполнения (RU)

## 1. Общая модель исполнения

Runtime мультипарного режима строится вокруг **циклов (loops)** и **событий**:

- **Fast loop (tick loop)** — обработка ценовых тиков и сигналов риска.
- **Slow loop (maintenance)** — рефреш сеток, перерасчёт KPI, ребаланс.
- **Event loop** — реакция на fill, cancel, error, stop.

## 2. Слои циклов

### 2.1 Tick loop (1–5s)

**Назначение:** защитный мониторинг.

- Проверка stale‑данных.
- Обновление price snapshot.
- Мгновенные guard‑решения (thin‑edge, spread spike).
- Формирование `fast_flags` в State Store.

### 2.2 Maintenance loop (10–60s)

**Назначение:** корректировка стратегий.

- Перерасчёт сеток.
- Проверка распределения бюджета.
- Планирование ребаланса.
- Сброс `cooldown` по парам.

### 2.3 Cluster sync loop (15–90s)

**Назначение:** синхронизация между парами.

- Сопоставление current exposure.
- Проверка hedge ratio.
- Запуск 2‑leg rebalancing loop.

## 3. Последовательность цикла исполнения (pseudo)

```
for each cluster:
  refresh_cluster_snapshot()
  update_cluster_kpi()

  if cluster_guard_blocked():
     cancel_cluster_orders()
     set_cluster_state(PAUSED)
     continue

  for each pair in cluster (primary -> secondary):
     update_pair_snapshot()
     intent = build_pair_intent()
     if guard_reject(intent):
        mark_pair_blocked()
        continue
     execute_intent(intent)
     persist_state()
```

## 4. Очереди и приоритеты

- **Priority 0**: cancel, kill‑switch, emergency close.
- **Priority 1**: rebalance legs, position reduce.
- **Priority 2**: grid build, refresh.
- **Priority 3**: metrics, logging, analytics.

`NetWorker` используется как механизм приоритизации и дедупликации.

## 5. Управление состоянием

### 5.1 Пара (PairState)

- `status`: IDLE / ACTIVE / REBALANCE / PAUSED.
- `last_tick_ts`, `last_refresh_ts`.
- `open_orders`, `pending_actions`.
- `pnl_snapshot`, `exposure`.

### 5.2 Кластер (ClusterState)

- `active_pairs`.
- `net_exposure`.
- `hedge_ratio`.
- `cluster_guard_flags`.

## 6. Ребаланс и rollback

- Ребаланс — это **двухфазная** операция:
  1) закрепление основной leg,
  2) доводка вторичной leg.
- Если завершена только одна leg, активируется rollback:
  - отмена частично выполненных действий,
  - перевод пары в `COOLDOWN`.

## 7. Обработка ошибок

- Любая ошибка в ордер‑операциях маркируется как `execution_error`.
- При повторяющихся ошибках включается `degraded` режим:
  - снижение числа активных пар,
  - пауза ребаланса,
  - усиление логирования.

## 8. Восстановление после перезапуска

- State Store должен сохранять snapshot после каждого цикла.
- При старте:
  - импорт предыдущих ордеров,
  - сверка с биржей,
  - восстановление `open_orders`.
- Если данные не совпадают → переход к `reconcile`.

## 9. Контроль задержек

- Каждый цикл фиксирует `loop_latency_ms`.
- При превышении порога:
  - снижать частоту maintenance loop,
  - сокращать количество активных пар,
  - отключать второстепенные метрики.

## 10. Мини‑чеклист оператора

- Есть ли активные `cluster_guard_flags`?
- Не превышен ли `max_pairs_active`?
- Все ли пары имеют `last_tick_ts` в пределах TTL?
- Есть ли «зависшие» pending actions?

## 11. Метрики производительности

- `loop_latency_ms` (p50/p95) по каждому циклу.
- `queue_depth` и `queue_wait_ms` в NetWorker.
- `orders_per_minute` и `cancel_rate`.

## 12. Ограничения SLA

- Max latency для tick loop: 500–800 ms.
- Max latency для maintenance loop: 3–5 s.
- Max задержка между legs при ребалансе: 2–4 s.

## 13. Тестовые сценарии

1. 2 пары, стабильный рынок → проверка ребаланса без rollback.
2. Всплеск волатильности → guard блокирует новые intents.
3. Потеря WS → fallback в HTTP, затем восстановление.
4. Частичное исполнение leg → rollback и cooldown.

## 14. План восстановления (recovery)

- При рестарте загружать `State Store` и reconcile с биржей.
- Если невозможно подтвердить open orders → `SAFE_PAUSE` и отмена.
- Для повторного запуска требуется чистый `guard_status=OK`.
