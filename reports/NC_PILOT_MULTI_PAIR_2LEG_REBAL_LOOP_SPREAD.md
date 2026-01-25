# NC Pilot Multi‑Pair — 2‑leg rebalancing loop spread (RU)

## 1. Суть схемы

**2‑leg rebalancing loop spread** — это цикл выравнивания дисбаланса между двумя связками (legs) в кластере. Цель — держать нейтральную или контролируемую экспозицию, используя спред между legs как сигнал и как механизм входа/выхода.

- **Leg A (primary)** — ведущая пара, задаёт направление.
- **Leg B (secondary)** — парная позиция, балансирует риск.

## 2. Основные понятия

- **Spread** = `price_A_adj - price_B_adj` (с корректировкой на комиссии и шаги).
- **Loop** — итеративный цикл проверки spread и запуска ребаланса.
- **Rebalance window** — интервал, в который обе legs должны быть подтверждены.

## 3. Входные параметры

- `spread_entry_bps` — порог входа для ребаланса.
- `spread_exit_bps` — порог выхода.
- `hedge_ratio_target` — целевое соотношение объёмов.
- `max_rebalance_steps` — лимит попыток.
- `min_liquidity_score` — минимальная ликвидность для входа.

## 4. Расчёт спреда

1. Определяем mid‑price для каждой пары.
2. Применяем поправку на комиссию и шаг цены.
3. Нормализуем по quote или base (единая валюта измерения).

Пример:

```
spread_bps = (mid_A - k * mid_B) / mid_A * 10_000
k = hedge_ratio_target
```

## 5. Запуск ребаланса (алгоритм)

1. **Проверка доступности данных** (TTL, stale).
2. **Проверка spread**:
   - если `abs(spread_bps) >= spread_entry_bps` → готов к ребалансу.
3. **Проверка ликвидности** обеих legs.
4. **Построение плана**:
   - объём Leg A = `base_amount`.
   - объём Leg B = `base_amount * hedge_ratio_target`.
5. **Выполнение**:
   - сначала Leg A (primary),
   - затем Leg B (secondary).
6. **Проверка исполнения**:
   - если обе legs подтверждены → записать `rebalance_ok`.
   - если только одна leg подтверждена → rollback.

## 6. Условия выхода

- Spread вернулся ниже `spread_exit_bps`.
- Истёк `rebalance_window`.
- Kill‑switch/guard активирован.

## 7. Rollback‑механика

Если частично выполнен только один leg:

- отменить оставшиеся ордера,
- увеличить cooldown,
- задокументировать `partial_fill`.

## 8. Пример сценария

**Исходные условия:**

- `spread_entry_bps = 6`
- `spread_exit_bps = 2`
- `hedge_ratio_target = 0.8`

**Сценарий:**

1. Спред вырос до 7 bps → trigger.
2. Ордер на Leg A исполнился, Leg B задержался.
3. Через `rebalance_window` Leg B не исполнился → rollback.
4. Пара переводится в `COOLDOWN`.

## 9. Guard‑условия, блокирующие ребаланс

- `stale_price` хотя бы на одной leg.
- `spread_bps` сверх лимита (аномальный рынок).
- `liquidity_drop`.
- `drawdown` или `cluster risk`.

## 10. Метрики качества

- `rebalance_success_rate`.
- `avg_spread_capture_bps`.
- `partial_fill_rate`.
- `rollback_count`.

## 11. Чек‑лист перед запуском

- Данные по обеим legs свежие?
- spread >= entry threshold?
- Есть ли доступная ликвидность?
- Не активированы ли guard‑флаги?
