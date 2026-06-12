# Дизайн: провайдер Барановичи Экспресс + Барановичи в Атласе

**Дата:** 2026-06-12
**Автор:** brainstorming session
**Порядок:** под-проект 2 из 6

## Цель

Бот умеет следить за местами Минск ↔ Барановичи через два источника:
новый провайдер `tickets.baranovichi-express.by` и существующий Атласбус.

## Решения, принятые на brainstorming

| Решение | Значение |
|---|---|
| Направления | Только `mnsk_baran` и `baran_mnsk` (сайт возит только Минск↔Барановичи; в Атласе Барановичи↔Могилёв/Бобруйск пустые — проверено) |
| Парсинг | Regex по HTML, без новых зависимостей (сайт серверный Laravel, JSON API нет) |
| trip_id | Композитный `{date}_{HH:MM}` — стабильного ID в HTML для анонимов нет |
| Atlas ID Барановичей | `c630429` (проверено живым запросом: рейсы возвращаются) |

## Разведка API (проверено 2026-06-12)

- Поиск: GET `https://tickets.baranovichi-express.by/tickets/search`
  с параметрами `pickup` / `destination` (значения: `1`=Барановичи, `2`=Минск),
  `seats_limit=1`, `date_of_journey=ДД.ММ.ГГГГ`. Куки/CSRF не нужны.
- Карточка рейса (`<article class="tickets-item">`): первое `tickets-way__point-time` —
  время отправления, `tickets-item__way-mini` — маршрут, `Свободно мест: N` (включая 0),
  цена в футере `<b>20.00 руб.</b>`.
- Время в карточках **без ведущего нуля** («7:00»).
- Прошедшая дата → страница-заглушка без карточек; дата дальше +1 мес →
  «Ничего не найдено». Оба случая безопасно дают пустой список.
- Сервер уважает дату запроса (проверено сравнением разных дат), дата эхом
  присутствует на странице.

## Изменения

### 1. `providers/base.py`

```python
DIRECTION_MNSK_BARAN = "mnsk_baran"
DIRECTION_BARAN_MNSK = "baran_mnsk"
# в DIRECTION_LABELS:
DIRECTION_MNSK_BARAN: "Минск → Барановичи",
DIRECTION_BARAN_MNSK: "Барановичи → Минск",
```

Хендлеры, LLM-инструменты и планировщик подхватывают новые направления
автоматически (всё выводится из `DIRECTION_LABELS`).

### 2. Новый файл `providers/baranovichi_express.py`

Класс `BaranovichiExpressProvider`:

- `name = "baranovichi_express"`, `display_name = "Барановичи Экспресс"`
- `directions = {mnsk_baran: ("2", "1"), baran_mnsk: ("1", "2")}` — (pickup, destination)
- `get_trips(client, date, direction)`:
  - дату `YYYY-MM-DD` конвертирует в `ДД.ММ.ГГГГ`
  - GET с параметрами выше, `resp.raise_for_status()`
  - режет HTML по `<article class="tickets-item"`, из каждой карточки regex'ами:
    время отправления (первый `point-time`), маршрут (`way-mini`),
    места (`Свободно мест:\s*(\d+)`), цена (`<b>([\d.]+)\s*руб`)
  - **нормализует время в `HH:MM` с ведущим нулём** («7:00» → «07:00») —
    критично: фильтр окна в планировщике сравнивает строки, неотбитое «7:00»
    лексикографически больше «23:00»
  - `Trip(trip_id=f"{date}_{HH:MM}", provider=name, route=..., date=запрошенная,
    departure_time=..., free_seats=..., price=..., currency="руб.")`
  - нет карточек → пустой список

### 3. `providers/atlasbus.py`

В `directions` добавить:
```python
DIRECTION_MNSK_BARAN: ("c625144", "c630429"),
DIRECTION_BARAN_MNSK: ("c630429", "c625144"),
```

### 4. `providers/__init__.py`

Зарегистрировать `"baranovichi_express": BaranovichiExpressProvider()`.

### 5. `llm/prompt.py`

- Первая строка промпта: «…между Могилёвом, Минском, Бобруйском и Барановичами».
- Поднять `LLM_SESSION_VERSION` (паттерн из коммита 4e8ce94) — активные сессии
  сбросятся и модель увидит новые направления.

## Тесты (`tests/test_providers.py`, по образцу существующих)

- Константы направлений: `mnsk_baran`/`baran_mnsk` + «Барановичи» в метках.
- HTML-фикстура с тремя карточками: обычная, «Свободно мест: 0», время без
  ведущего нуля.
- Парсинг фикстуры: количество, trip_id, нормализованное время, места, цена.
- Параметры запроса: формат даты `ДД.ММ.ГГГГ`, правильные pickup/destination
  для обоих направлений.
- Конфиг провайдера: name, display_name, directions.
- Пустая страница («Ничего не найдено») → пустой список.
- Обновить: `test_atlasbus_config` (новые направления), `test_registry_has_all_providers`
  (+ `baranovichi_express`, исключение в проверке mg_mnsk), аналог
  `test_bobruisk_direction_support` для Барановичей.

## Вне скоупа

- БД, конфиг, Docker — без изменений (миграций нет, авторизации для поиска нет).
- Автобронирование — отдельная спека `2026-06-12-baranovichi-autobook-design.md`.
