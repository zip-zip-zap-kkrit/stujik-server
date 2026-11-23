# Расписание ККРИТ'а

Сервис скачивает PDF с расписанием с Яндекс.Диска, раскладывает его по
SQLite-базам и отдаёт через FastAPI.

> Инструкция для разработки клиентского приложения:
> [`APP_DEVELOPMENT.md`](APP_DEVELOPMENT.md)

## Структура

- `db.py` — слой работы с базами расписания (`databases/YYYY-MM-DD.db`).
- `pdfparser.py` — разбор PDF и нормализация данных.
- `main.py` — фоновый парсер: скачивание, разбор и сохранение.
- `server.py` — HTTP API и push-уведомления Firebase.
- `index.html` — старая web-версия (в API-документации скрыта).
- `tests/` — unit- и API-тесты.

## Запуск

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# Таблица для push-токенов
.venv/bin/python init_db.py

# API + фоновый парсер
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Если парсер не нужен (например, при разработке):

```bash
START_SCHEDULE_PARSER=0 .venv/bin/uvicorn server:app --reload
```

Документация API: `http://127.0.0.1:8000/docs`

## Обработка PDF

- заглушки вида `_______` и `_______ _____` удаляются;
- OCR-каша из отдельных букв (`К Р л у а д с н с е н в ы`) отбрасывается;
- известные маркеры (`Линейка`, `Классный час`, `СЕССИЯ`) сохраняются без
  мусорного хвоста;
- номера кабинетов нормализуются, явный OCR-мусор отфильтровывается;
- старые мусорные записи, уже лежащие в базах, также не отдаются через API.

## API

Основные читаемые маршруты:

| Метод | Путь | Описание |
| --- | --- | --- |
| `GET` | `/health` | Состояние сервиса и самая свежая дата |
| `GET` | `/getDates` | Все доступные даты |
| `GET` | `/getGroups` | Группы (`?date=YYYY-MM-DD`) |
| `GET` | `/getAllTeachers` | Все преподаватели из всех баз |
| `GET` | `/getTeachers` | Преподаватели (`?date=YYYY-MM-DD`) |
| `GET` | `/getAllRooms` | Все кабинеты из всех баз |
| `GET` | `/getRooms` | Кабинеты (`?date=YYYY-MM-DD`) |
| `GET` | `/getLessonByGroup/{group}` | Расписание группы (`?date=YYYY-MM-DD`) |
| `GET` | `/getLessonByTeacher/{teacher}` | Расписание преподавателя |
| `GET` | `/getLessonByRoom/{room}` | Расписание кабинета (`?date=YYYY-MM-DD`) |
| `POST` | `/register` | Регистрация push-токена |
| `POST` | `/send_all` | Push всем устройствам |
| `GET` | `/bingo-download` | APK-файл |

Если `date` не указан:

- для групп и справочников берётся самая свежая база;
- для преподавателя — последняя дата, в которую у него есть занятия;
- для кабинета — последняя дата, в которую он используется.

У преподавателя и кабинета в ответе без даты есть поле `dates` — все дни
с занятиями. `/getAllRooms` отдаёт объединённый список кабинетов по всем
базам, даже если в последней базе кабинета уже нет.

Номера кабинетов нормализуются: OCR-варианты вида `1 1 2` и
`2 1 3 /2 1 6` превращаются в `112` и `213/216`, явный мусор отфильтровывается.

Примеры:

```bash
curl "http://127.0.0.1:8000/getGroups"
curl "http://127.0.0.1:8000/getLessonByGroup/9%D0%9F%D0%A0-2.25?date=2026-10-01"
curl "http://127.0.0.1:8000/getLessonByRoom/209?date=2026-10-01"
curl "http://127.0.0.1:8000/getLessonByRoom/318/418?date=2026-10-01"
curl "http://127.0.0.1:8000/getAllRooms"
curl "http://127.0.0.1:8000/getLessonByRoom/317"
curl "http://127.0.0.1:8000/getLessonByTeacher/%D0%9A%D0%B5%D1%82%D1%80%D0%BE%D0%B2%D0%B0--%D0%90.%D0%90."
```

Старые маршруты (`/lessonsByGroup/...`, `/teacher/...`, `/rooms/...`,
`/groups`, `/dates`) продолжают работать как скрытые alias'ы.
Индекс преподавателей строится в памяти при первом запросе и автоматически
пересобирается при изменении баз.

## Безопасность и настройки

Служебные методы закрыты API-ключом:

| Переменная | Назначение |
| --- | --- |
| `ADMIN_API_KEY` | Обязательный ключ для `POST /send_all` |
| `REGISTER_API_KEY` | Необязательный ключ для `POST /register` |
| `CORS_ORIGINS` | Список origin через запятую; по умолчанию CORS выключен |
| `MAX_DEVICE_TOKENS` | Максимум push-токенов (по умолчанию `5000`) |
| `MAX_REQUEST_BODY_BYTES` | Максимум тела запроса (по умолчанию `1000000`) |
| `REGISTER_RATE_LIMIT` | Лимит регистраций с одного IP за окно |
| `REGISTER_RATE_WINDOW` | Окно лимита регистраций в секундах |
| `SEND_RATE_LIMIT` | Лимит отправок push с одного IP в минуту |
| `FIREBASE_CREDENTIALS` | Путь к service account JSON (по умолчанию `serviceAccountKey.json`) |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота для уведомлений |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram |
| `CLOUDCONVERT_API_KEY` | Ключ CloudConvert |
| `YANDEX_TOKEN` | OAuth-токен Яндекс.Диска |
| `YANDEX_SECRET` | Секрет приложения Яндекс.Диска |

Ключ передаётся в заголовке:

```bash
curl -X POST "http://127.0.0.1:8000/send_all" \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"Расписание","body":"Добавлено новое расписание"}'
```

Если `ADMIN_API_KEY` не задан, `POST /send_all` отключён и возвращает `503`.
Если задан `REGISTER_API_KEY`, регистрация устройства тоже требует заголовок
`X-API-Key`.

Секреты (Telegram, CloudConvert, Яндекс, Firebase) не должны попадать в
исходники — они читаются из переменных окружения. Если старые ключи уже
лежали в репозитории, их стоит перевыпустить. Файл service account Firebase
добавлен в `.gitignore`.

Дополнительно:

- даты проверяются до обращения к файлам — path traversal закрыт;
- SQL-запросы параметризованы;
- группа/преподаватель/кабинет ограничены по длине;
- push-токены проверяются по формату и длине;
- ошибки SQLite не отдаются клиенту;
- ответы получают security-заголовки;
- старые маршруты скрыты из OpenAPI, чтобы документация оставалась читаемой.

## Тесты

```bash
START_SCHEDULE_PARSER=0 .venv/bin/python -m unittest discover -v
```

API-тесты используют реальные данные группы `9ПР-2.25`.
