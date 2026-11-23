# Разработка приложения поверх сервера расписания

> Файл-памятка для новой сессии разработки.  
> Актуально для `server.py` версии `2.4.0`.  
> Если что-то в API поменялось — сначала сверься с `/docs` и `server.py`.

---

## TL;DR

- Сервер: FastAPI, база — отдельный SQLite-файл на каждый день.
- Локальный адрес: `http://127.0.0.1:8000`
- Swagger: `http://127.0.0.1:8000/docs`
- Даты — только ISO: `YYYY-MM-DD`.
- Чтение расписания публичное, без авторизации.
- Push-методы защищены API-ключом.
- Главный сценарий приложения:
  1. `GET /getGroups`
  2. пользователь выбирает группу
  3. `GET /getLessonByGroup/{group}?date=YYYY-MM-DD`
- Не хардкодь дату в клиенте. Бери её из `/getDates` или `/health`.
- Если даты нет — API вернёт `404`, это нормально. Обрабатывай статус, а не считай это падением сервера.

---

## 1. Что уже есть в проекте

| Файл | Назначение |
| --- | --- |
| `server.py` | HTTP API, безопасность, push, запуск приложения |
| `db.py` | Работа с базами расписания, индексы преподавателей и кабинетов |
| `pdfparser.py` | Разбор PDF, очистка OCR-мусора |
| `main.py` | Фоновый парсер Яндекс.Диска |
| `downloader.py` | Скачивание PDF |
| `config.py` | Пути и настройки, секреты из env |
| `init_db.py` | Создание `tokens.db` |
| `telenotifications.py` | Telegram-уведомления (сейчас отправка отключена) |
| `databases/*.db` | Базы расписания по дням |
| `tokens.db` | FCM-токены устройств |
| `tests/test_api.py` | API-, DB- и parser-тесты |
| `index.html` | Старая web-версия, в API-документации скрыта |

Схема записи расписания:

```sql
schedule_entries(
    id,
    date,
    `group`,
    lessonNumber,
    subject,
    teacher,
    room,
    DO
)
```

Уникальность: `(date, group, lessonNumber)`.

---

## 2. Запуск сервера

### Первый запуск

```bash
cd /home/ninewer/Downloads/hosting

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# таблица для push-токенов
.venv/bin/python init_db.py
```

### Режим разработки без фонового парсера

```bash
START_SCHEDULE_PARSER=0 .venv/bin/uvicorn server:app --reload
```

### С парсером

```bash
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Парсер запускается дочерним процессом и сам обновляет базы.

### Проверка, что сервер жив

```bash
curl "http://127.0.0.1:8000/health"
```

Пример ответа:

```json
{
  "status": "ok",
  "version": "2.4.0",
  "latest_date": "2026-11-24",
  "dates": 181,
  "docs": "/docs"
}
```

`latest_date` — не константа, а самая свежая реально существующая база.

### Тесты

```bash
START_SCHEDULE_PARSER=0 .venv/bin/python -m unittest discover -v
```

Сейчас в наборе 50 тестов. API-тесты используют группу `9ПР-2.25`.

---

## 3. Переменные окружения

| Переменная | Зачем |
| --- | --- |
| `START_SCHEDULE_PARSER` | `0` — не запускать фоновый парсер |
| `ADMIN_API_KEY` | Ключ для `POST /send_all`. Без него метод отключён (`503`) |
| `REGISTER_API_KEY` | Если задан — `POST /register` тоже требует ключ |
| `CORS_ORIGINS` | Origin'ы через запятую. По умолчанию CORS выключен |
| `MAX_REQUEST_BODY_BYTES` | Максимальный размер тела запроса |
| `MAX_DEVICE_TOKENS` | Максимум push-токенов |
| `REGISTER_RATE_LIMIT` | Лимит регистраций с IP |
| `REGISTER_RATE_WINDOW` | Окно лимита регистраций, секунды |
| `SEND_RATE_LIMIT` | Лимит отправок push с IP в минуту |
| `TOKENS_DB` | Путь к SQLite с push-токенами |
| `FIREBASE_CREDENTIALS` | Путь к Firebase service account JSON |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram |
| `CLOUDCONVERT_API_KEY` | Ключ CloudConvert |
| `YANDEX_TOKEN` | Токен Яндекс.Диска |
| `YANDEX_SECRET` | Секрет Яндекс.Диска |

Секреты не коммитить. `serviceAccountKey.json`, `tokens.db`, `.env` уже в `.gitignore`.

---

## 4. Общие правила API

### Формат данных

- Все ответы — JSON UTF-8.
- Даты — `YYYY-MM-DD`.
- `content-type: application/json`.
- Тело успешного ответа всегда объект, не голый массив.

### URL-кодирование

Кириллицу, пробелы и слэши в path-параметрах кодируй.

```js
encodeURIComponent("9ПР-2.25"); // 9%D0%9F%D0%A0-2.25
encodeURIComponent("318/418");  // 318%2F418
```

Для кабинетов с `/` работают оба варианта:

```text
/getLessonByRoom/318/418
/getLessonByRoom/318%2F418
```

### Дата: как выбирается

| Endpoint | Без даты |
| --- | --- |
| `/getGroups` | последняя доступная база |
| `/getTeachers` | последняя доступная база |
| `/getRooms` | последняя доступная база |
| `/getLessonByGroup/{group}` | последняя доступная база |
| `/getLessonByTeacher/{teacher}` | последняя дата, когда у преподавателя есть пары |
| `/getLessonByRoom/{room}` | последняя дата, когда кабинет используется |

Всегда смотри на поле `date` в ответе — оно показывает, за какой день реально отданы данные.

### Ошибки

| Код | Когда |
| --- | --- |
| `400` | Некорректная дата, пустой/слишком длинный параметр, плохой номер кабинета |
| `401` | Нет/неверный `X-API-Key` |
| `404` | Нет базы за дату, нет группы/преподавателя/кабинета |
| `413` | Слишком большое тело запроса |
| `422` | Ошибка валидации Pydantic, например короткий push-токен |
| `429` | Сработал rate limit |
| `503` | Push отключён, потому что не задан `ADMIN_API_KEY` |

Пример 404:

```json
{
  "detail": "Расписание на 2026-11-23 не найдено. Последняя доступная дата: 2026-11-24"
}
```

Клиент должен проверять `response.ok` / HTTP-статус, а не считать `404` падением сервера.

### Legacy-маршруты

Старые пути работают, но скрыты из документации:

```text
/lessonsByGroup/{date}/{group}
/schedule/{date}/{group}
/{date}/{group}
/teacher/{date}/{teacher}
/teacher/{teacher}
/teachers
/teachers/{date}
/rooms
/rooms/{date}
/room/{date}/{room}
/groups
/dates
/ver
```

В новом коде используй читаемые `/get...` маршруты.

---

## 5. Модель данных для клиента

### Одна пара

```json
{
  "id": 11,
  "date": "2026-11-24",
  "group": "9ПР-2.25",
  "lessonNumber": 1,
  "number": "1",
  "subject": "Основы проектной деятельности",
  "teacher": "Боярская Т.А.",
  "room": "401",
  "DO": 0
}
```

Поля:

| Поле | Тип | Комментарий |
| --- | --- | --- |
| `id` | int | ID строки в дневной базе |
| `date` | string | `YYYY-MM-DD` |
| `group` | string | Нормализованное название группы |
| `lessonNumber` | int | Номер пары. Может быть `0` для линейки/классного часа |
| `number` | string | Строковый дубль `lessonNumber` для старых клиентов |
| `subject` | string | Предмет, уже очищен от OCR-мусора |
| `teacher` | string | Может быть пустым или содержать несколько ФИО |
| `room` | string | Может быть пустым; кабинеты нормализованы |
| `DO` | int | `1` — дистанционное обучение, `0` — обычное |

Важные особенности:

- Времени начала/конца пары нет. Только `lessonNumber`. Звонковую сетку клиент задаёт сам.
- Подгруппы зашиты в текст предмета: `1 п/гр Информатика`, `2 п/гр ...`.
- У одной группы на день нет двух строк с одинаковым `lessonNumber`.
- `teacher` может содержать несколько преподавателей через пробел.
- `room` может быть `318/418`, `301/ДО`, `""`.
- `lessonNumber = 0` встречается у линеек и подобных событий.
- Мусорные предметы вроде `_______` и OCR-каша сервер отфильтровывает.

### Справочники

Группа:

```json
{"group": "9ПР-2.25"}
```

Преподаватель:

```json
{"teacher": "Кетрова А.А."}
```

Кабинет:

```json
{"room": "209"}
```

---

## 6. Эндпоинты

### 6.1 Состояние и даты

```http
GET /health
GET /getDates
```

`/getDates`:

```json
{
  "dates": ["2026-09-01", "2026-09-02", "2026-11-24"]
}
```

### 6.2 Справочники

```http
GET /getGroups
GET /getGroups?date=2026-11-24

GET /getTeachers?date=2026-11-24
GET /getAllTeachers

GET /getRooms?date=2026-11-24
GET /getAllRooms
```

`GET /getGroups`:

```json
{
  "date": "2026-11-24",
  "type": "group",
  "groups": [
    {"group": "9АБД-1.25П"},
    {"group": "9ПР-2.25"}
  ]
}
```

`GET /getTeachers`:

```json
{
  "date": "2026-11-24",
  "teachers": [
    {"teacher": "Кетрова А.А."}
  ]
}
```

`GET /getAllTeachers` — все преподаватели из всех баз, без `date`.

`GET /getRooms`:

```json
{
  "date": "2026-11-24",
  "rooms": [
    {"room": "209"},
    {"room": "318/418"}
  ]
}
```

`GET /getAllRooms` — все кабинеты из всех баз, без `date`.

### 6.3 Расписание группы

```http
GET /getLessonByGroup/{group}
GET /getLessonByGroup/{group}?date=YYYY-MM-DD
```

Пример:

```bash
curl "http://127.0.0.1:8000/getLessonByGroup/9%D0%9F%D0%A0-2.25?date=2026-11-24"
```

Ответ:

```json
{
  "date": "2026-11-24",
  "group": "9ПР-2.25",
  "schedule": [
    {
      "id": 11,
      "date": "2026-11-24",
      "group": "9ПР-2.25",
      "lessonNumber": 1,
      "number": "1",
      "subject": "Основы проектной деятельности",
      "teacher": "Боярская Т.А.",
      "room": "401",
      "DO": 0
    }
  ]
}
```

### 6.4 Расписание преподавателя

```http
GET /getLessonByTeacher/{teacher}
GET /getLessonByTeacher/{teacher}?date=YYYY-MM-DD
```

Без даты:

- берётся последняя дата, когда у преподавателя есть пары;
- в ответе есть `dates` — все даты с его занятиями.

```json
{
  "date": "2026-11-20",
  "teacher": "Кетрова А.А.",
  "dates": ["2026-01-14", "2026-01-15", "..."],
  "schedule": [
    {
      "id": 11,
      "date": "2026-11-20",
      "group": "9ПР-3.25К",
      "lessonNumber": 1,
      "subject": "Архитектура аппаратных средств",
      "teacher": "Кетрова А.А.",
      "room": "401",
      "DO": 0
    }
  ]
}
```

С датой — поведение обычное, поле `dates` не возвращается.

Имя преподавателя можно передать:

```text
Кетрова--А.А.
```

или с обычным пробелом, закодированным в URL:

```text
%D0%9A%D0%B5%D1%82%D1%80%D0%BE%D0%B2%D0%B0%20%D0%90.%D0%90.
```

### 6.5 Расписание кабинета

```http
GET /getLessonByRoom/{room}
GET /getLessonByRoom/{room}?date=YYYY-MM-DD
```

Без даты — последняя дата, когда кабинет используется, плюс поле `dates`.

```bash
curl "http://127.0.0.1:8000/getLessonByRoom/209"
curl "http://127.0.0.1:8000/getLessonByRoom/318/418?date=2026-11-24"
```

Ответ:

```json
{
  "date": "2026-11-24",
  "room": "209",
  "dates": ["2026-01-12", "..."],
  "schedule": [
    {
      "id": 93,
      "date": "2026-11-24",
      "group": "9ПР-3.24",
      "lessonNumber": 1,
      "subject": "1 п/гр УП.01.01",
      "teacher": "Коломейцева Ю.В.",
      "room": "209",
      "DO": 0
    }
  ]
}
```

### 6.6 Push-уведомления

Регистрация устройства:

```http
POST /register
Content-Type: application/json
X-API-Key: ...    # только если на сервере задан REGISTER_API_KEY
```

```json
{"token": "FCM_REGISTRATION_TOKEN"}
```

Токен: 10–4096 символов, только `A-Za-z0-9_:.-`.

Ответ:

```json
{"status": "ok"}
```

Отправка всем:

```http
POST /send_all
Content-Type: application/json
X-API-Key: ADMIN_API_KEY
```

```json
{
  "title": "Новое расписание",
  "body": "Добавлено расписание на 2026-11-24"
}
```

Ответ:

```json
{"success": 10, "failed": 0, "total": 10}
```

`ADMIN_API_KEY` нельзя встраивать в мобильное приложение. Отправку push делает
только сервер/админ-панель.

### 6.7 Файлы

```http
GET /bingo-download
```

Отдаёт APK.

---

## 7. Рекомендуемый сценарий клиента

1. При старте запросить `/health` или `/getDates`.
2. Показать список групп (`/getGroups`).
3. Запомнить выбранную группу.
4. Для выбранной даты запросить `/getLessonByGroup/{group}?date=...`.
5. Если `404`:
   - показать «На эту дату расписания пока нет»;
   - предложить последнюю доступную дату из `detail`/`/health`;
   - не очищать уже показанное расписание.
6. Кэшировать справочники групп/преподавателей/кабинетов на 5–10 минут.

### Пример на JS

```js
const API = "http://127.0.0.1:8000";

async function api(path) {
  const response = await fetch(API + path);
  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `HTTP ${response.status}`);
  }

  return data;
}

async function loadSchedule(group, date) {
  const path =
    `/getLessonByGroup/${encodeURIComponent(group)}` +
    (date ? `?date=${encodeURIComponent(date)}` : "");

  return api(path);
}

// Пример
const { latest_date } = await api("/health");
const { groups } = await api("/getGroups");
const schedule = await loadSchedule(groups[0].group, latest_date);
```

### Преподаватели в UI

- Список для поиска: `/getAllTeachers`.
- Карточка преподавателя без даты: `/getLessonByTeacher/{teacher}`.
- Поле `dates` можно использовать для переключателя дней.
- Для конкретного дня: `/getLessonByTeacher/{teacher}?date=...`.

### Кабинеты в UI

- Список для поиска: `/getAllRooms`.
- Карточка кабинета без даты: `/getLessonByRoom/{room}`.
- Для конкретного дня: `/getLessonByRoom/{room}?date=...`.
- Кабинеты с `/` кодируй через `encodeURIComponent`.

---

## 8. Что важно не сломать

- Не менять формат `lessonNumber` на строку — клиенты уже могут использовать число.
- `number` оставлен для старых клиентов, его лучше не удалять.
- `DO` — именно `0/1`, не boolean.
- При отсутствии даты сервер сам выбирает последнюю. Клиент должен смотреть на `date` в ответе.
- Не хардкодить `latest_date`: базы обновляются фоновым парсером.
- Не включать `ADMIN_API_KEY` в клиентское приложение.
- Если нужен браузерный клиент с другого origin — настроить `CORS_ORIGINS`.
- `REGISTER_API_KEY` — не настоящая защита, если он зашит в APK. Для публичного приложения лучше оставить регистрацию без ключа, но с rate limit.

---

## 9. Известные ограничения

- API только для чтения расписания. Создания/редактирования пар нет.
- Нет времени звонков, только номер пары.
- Нет отдельного endpoint для «сегодня»: используй `/health` + `latest_date` или дату клиента. Если базы за сегодня ещё нет — будет `404`.
- Нет WebSocket. Обновление — polling или FCM push.
- Нет пагинации: справочники небольшие.
- Нет авторизации на чтение: данные расписания публичные.
- OCR-очистка эвристическая: явный мусор убирается, но опечатки вроде
  `прораммных` могут остаться.
- Подгруппы — часть текста предмета, а не отдельное поле.

---

## 10. Как добавить новый endpoint

1. Логику работы с БД добавить в `db.py`.
2. HTTP-обёртку — в `server.py`.
3. Использовать `_validate_date`, `_resolve_date`, `_clean_lookup`.
4. Не отдавать наружу `sqlite3.Error`.
5. Добавить тест в `tests/test_api.py`.
6. Обновить этот файл и `README.md`.
7. Проверить `/docs` и `openapi.json`, чтобы маршрут был читаемым.

Соглашения:

- новые маршруты — `/get...`;
- старые alias'ы — `include_in_schema=False`;
- даты — query `?date=`, кроме legacy;
- path-параметры с кириллицей — `encodeURIComponent`;
- кабинеты — `{room:path}`.

---

## 11. Быстрый промпт для новой сессии

Скопируй в новую сессию:

```text
Разрабатываем клиент к серверу расписания из /home/ninewer/Downloads/hosting.
Сначала прочитай APP_DEVELOPMENT.md и server.py.
Сервер уже готов, менять его API без необходимости не нужно.
Стек клиента: <укажи стек>.
Нужно: <список экранов/функций>.
Учитывай, что даты могут отсутствовать и API возвращает 404,
а latest_date нельзя хардкодить.
```

---

## 12. Чеклист перед началом

- [ ] Сервер запускается: `START_SCHEDULE_PARSER=0 .venv/bin/uvicorn server:app --reload`
- [ ] `/health` возвращает `status: ok`
- [ ] `/docs` открывается
- [ ] `/getGroups` возвращает группы
- [ ] `/getLessonByGroup/9ПР-2.25` возвращает пары
- [ ] Тесты проходят: `.venv/bin/python -m unittest discover`
- [ ] В клиенте есть обработка `404`, `401`, `429`, `503`
- [ ] Кириллица и `/` в path кодируются
- [ ] `ADMIN_API_KEY` не попал в клиент
