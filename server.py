"""HTTP API расписания колледжа.

Публичные читаемые маршруты:

    GET /getGroups
    GET /getTeachers
    GET /getRooms
    GET /getDates
    GET /getLessonByGroup/{group}
    GET /getLessonByTeacher/{teacher}
    GET /getLessonByRoom/{room}

Старые маршруты оставлены как скрытые alias'ы для совместимости.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterator

import firebase_admin
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from firebase_admin import credentials, messaging
from pydantic import BaseModel, Field

from db import (
    available_dates,
    database_exists,
    is_plausible_room,
    load_all_rooms,
    load_all_teachers,
    load_groups,
    load_room_schedule,
    load_rooms,
    load_schedule,
    load_teacher_schedule,
    load_teachers,
    normalize_date,
    normalize_group,
    normalize_room,
    room_dates,
    teacher_dates,
)

BASE_DIR = Path(__file__).resolve().parent
TOKENS_DB = Path(os.getenv("TOKENS_DB", str(BASE_DIR / "tokens.db")))
FIREBASE_CREDENTIALS = Path(
    os.getenv("FIREBASE_CREDENTIALS", str(BASE_DIR / "serviceAccountKey.json"))
)

VERSION = "2.4.0"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
REGISTER_API_KEY = os.getenv("REGISTER_API_KEY", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MAX_DEVICE_TOKENS = max(1, _env_int("MAX_DEVICE_TOKENS", 5000))
MAX_REQUEST_BODY_BYTES = max(1024, _env_int("MAX_REQUEST_BODY_BYTES", 1_000_000))
REGISTER_RATE_LIMIT = max(1, _env_int("REGISTER_RATE_LIMIT", 120))
REGISTER_RATE_WINDOW = max(1, _env_int("REGISTER_RATE_WINDOW", 3600))
SEND_RATE_LIMIT = max(1, _env_int("SEND_RATE_LIMIT", 10))

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]


# ---------------------------------------------------------------------------
# Валидация входных данных
# ---------------------------------------------------------------------------


def _parser_enabled() -> bool:
    value = os.getenv("START_SCHEDULE_PARSER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _validate_date(date: str) -> str:
    try:
        return normalize_date(date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_date(date: str | None) -> str:
    """Дата из запроса или самая свежая доступная дата."""
    if date is not None:
        day = _validate_date(date)
        _require_database(day)
        return day

    dates = available_dates()
    if not dates:
        raise HTTPException(status_code=404, detail="Нет доступных баз расписания")

    return dates[-1]


def _require_database(date: str) -> None:
    if database_exists(date):
        return

    dates = available_dates()
    latest = dates[-1] if dates else None
    detail = f"Расписание на {date} не найдено"
    if latest:
        detail += f". Последняя доступная дата: {latest}"

    raise HTTPException(status_code=404, detail=detail)


def _clean_lookup(value: str, field: str, max_length: int) -> str:
    cleaned = normalize_group(value)
    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail=f"Поле «{field}» не может быть пустым",
        )
    if len(cleaned) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"Слишком длинное значение поля «{field}»",
        )
    return cleaned


def _teacher_lookup(value: str) -> str:
    return _clean_lookup(value.replace("--", " "), "teacher", 200)


def _room_lookup(value: str) -> str:
    cleaned = normalize_room(value)
    if not cleaned or not is_plausible_room(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Некорректный номер кабинета",
        )
    if len(cleaned) > 50:
        raise HTTPException(
            status_code=400,
            detail="Слишком длинное значение поля «room»",
        )
    return cleaned


def _schedule_payload(day: str, group: str) -> dict:
    _require_database(day)

    schedule = load_schedule(group, day)
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail=f"Нет расписания для группы {group} на {day}",
        )

    return {
        "date": day,
        "group": schedule[0]["group"],
        "schedule": schedule,
    }


def _teacher_payload(
    day: str,
    teacher: str,
    *,
    dates: list[str] | None = None,
) -> dict:
    schedule = load_teacher_schedule(day, teacher)
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail=f"Нет расписания для преподавателя {teacher} на {day}",
        )

    payload = {
        "date": day,
        "teacher": teacher,
        "schedule": schedule,
    }
    if dates is not None:
        payload["dates"] = dates

    return payload


def _room_payload(
    day: str,
    room: str,
    *,
    dates: list[str] | None = None,
) -> dict:
    _require_database(day)

    schedule = load_room_schedule(day, room)
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail=f"Нет расписания для кабинета {room} на {day}",
        )

    payload = {
        "date": day,
        "room": schedule[0]["room"],
        "schedule": schedule,
    }
    if dates is not None:
        payload["dates"] = dates

    return payload


# ---------------------------------------------------------------------------
# Ограничение частоты запросов
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Простой in-memory rate limiter по IP."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.limit:
                return False

            hits.append(now)
            return True


def _client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


_register_limiter = _RateLimiter(REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW)
_send_limiter = _RateLimiter(SEND_RATE_LIMIT, 60)


# ---------------------------------------------------------------------------
# Авторизация служебных методов
# ---------------------------------------------------------------------------


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_admin(api_key: str | None = Depends(_api_key_header)) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_KEY не настроен",
        )

    if not api_key or not secrets.compare_digest(api_key, ADMIN_API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Неверный API-ключ",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def _require_register_key(api_key: str | None = Depends(_api_key_header)) -> None:
    if not REGISTER_API_KEY:
        return

    if not api_key or not secrets.compare_digest(api_key, REGISTER_API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Неверный API-ключ",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ---------------------------------------------------------------------------
# SQLite для push-токенов
# ---------------------------------------------------------------------------


@contextmanager
def token_connection() -> Iterator[sqlite3.Connection]:
    TOKENS_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(TOKENS_DB, timeout=5, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_tokens_db() -> None:
    with token_connection() as connection:
        connection.execute(TOKENS_SCHEMA)


# ---------------------------------------------------------------------------
# Firebase
# ---------------------------------------------------------------------------


def _ensure_firebase() -> None:
    if firebase_admin._apps:
        return

    if not FIREBASE_CREDENTIALS.is_file():
        logger.error("Не найден ключ Firebase: %s", FIREBASE_CREDENTIALS)
        raise RuntimeError("Push-уведомления не настроены")

    try:
        firebase_admin.initialize_app(
            credentials.Certificate(str(FIREBASE_CREDENTIALS))
        )
        logger.info("Firebase инициализирован")
    except Exception as exc:
        logger.exception("Не удалось инициализировать Firebase")
        raise RuntimeError("Push-уведомления недоступны") from exc


def send_all(title: str, body: str) -> dict:
    """Отправляет push всем зарегистрированным устройствам."""
    init_tokens_db()

    with token_connection() as connection:
        tokens = [
            row["token"]
            for row in connection.execute(
                "SELECT token FROM device_tokens"
            ).fetchall()
        ]

    if not tokens:
        logger.info("Push не отправлен: нет зарегистрированных устройств")
        return {"success": 0, "failed": 0, "total": 0}

    _ensure_firebase()

    success = 0
    failed = 0
    broken_tokens: list[str] = []

    # Firebase принимает максимум 500 токенов за один запрос.
    for offset in range(0, len(tokens), 500):
        chunk = tokens[offset:offset + 500]
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            tokens=chunk,
        )
        response = messaging.send_each_for_multicast(message)
        success += response.success_count
        failed += response.failure_count

        for index, result in enumerate(response.responses):
            if not result.success:
                broken_tokens.append(chunk[index])

    if broken_tokens:
        with token_connection() as connection:
            connection.executemany(
                "DELETE FROM device_tokens WHERE token = ?",
                [(token,) for token in broken_tokens],
            )

    logger.info(
        "Push отправлен: успешно=%d, ошибок=%d, всего=%d",
        success,
        failed,
        len(tokens),
    )
    return {"success": success, "failed": failed, "total": len(tokens)}


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tokens_db()

    parser_process = None
    if _parser_enabled():
        logger.info("Запускаю фоновый парсер расписания")
        parser_process = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "main.py")],
            cwd=BASE_DIR,
        )
    else:
        logger.info("Фоновый парсер отключён (START_SCHEDULE_PARSER=0)")

    app.state.parser_process = parser_process
    try:
        yield
    finally:
        if parser_process and parser_process.poll() is None:
            logger.info("Останавливаю фоновый парсер")
            parser_process.terminate()
            try:
                parser_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                parser_process.kill()


app = FastAPI(
    title="Расписание колледжа",
    description="API расписания по группам, преподавателям и кабинетам.",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_tags=[
        {"name": "meta", "description": "Состояние сервиса и версия"},
        {"name": "schedule", "description": "Расписание и справочники"},
        {"name": "push", "description": "Push-уведомления"},
    ],
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
    )


def _apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.middleware("http")
async def common_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            return _apply_security_headers(
                JSONResponse(
                    status_code=400,
                    content={"detail": "Некорректный Content-Length"},
                )
            )

        if length > MAX_REQUEST_BODY_BYTES:
            return _apply_security_headers(
                JSONResponse(
                    status_code=413,
                    content={"detail": "Слишком большое тело запроса"},
                )
            )

    response = await call_next(request)
    return _apply_security_headers(response)


class RegisterRequest(BaseModel):
    token: str = Field(
        min_length=10,
        max_length=4096,
        pattern=r"^[A-Za-z0-9_:.-]+$",
        description="FCM registration token",
    )


class PushRequest(BaseModel):
    title: str = Field(default="Привет 👋", max_length=200)
    body: str = Field(default="Push от FastAPI сервера", max_length=2000)


# ---------------------------------------------------------------------------
# Мета
# ---------------------------------------------------------------------------


@app.get("/", tags=["meta"])
@app.get("/health", tags=["meta"])
async def health() -> dict:
    dates = available_dates()
    return {
        "status": "ok",
        "version": VERSION,
        "latest_date": dates[-1] if dates else None,
        "dates": len(dates),
        "docs": "/docs",
    }


@app.get("/ver", tags=["meta"], include_in_schema=False)
async def version() -> dict:
    return {"ver": VERSION}


@app.get("/getDates", tags=["schedule"])
async def get_dates() -> dict:
    return {"dates": available_dates()}


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------


@app.get("/getGroups", tags=["schedule"])
async def get_groups(
    date: str | None = Query(
        default=None,
        max_length=10,
        description="YYYY-MM-DD; по умолчанию последняя доступная дата",
    ),
) -> dict:
    day = _resolve_date(date)
    return {
        "date": day,
        "type": "group",
        "groups": load_groups(day),
    }


@app.get("/getAllTeachers", tags=["schedule"])
async def get_all_teachers() -> dict:
    """Все преподаватели из всех баз, без привязки к дате."""
    return {"teachers": load_all_teachers()}


@app.get("/getTeachers", tags=["schedule"])
async def get_teachers(
    date: str | None = Query(
        default=None,
        max_length=10,
        description="YYYY-MM-DD; по умолчанию последняя доступная дата",
    ),
) -> dict:
    day = _resolve_date(date)
    return {"date": day, "teachers": load_teachers(day)}


@app.get("/getAllRooms", tags=["schedule"])
async def get_all_rooms() -> dict:
    """Все кабинеты из всех баз, без привязки к дате."""
    return {"rooms": load_all_rooms()}


@app.get("/getRooms", tags=["schedule"])
async def get_rooms(
    date: str | None = Query(
        default=None,
        max_length=10,
        description="YYYY-MM-DD; по умолчанию последняя доступная дата",
    ),
) -> dict:
    day = _resolve_date(date)
    return {"date": day, "rooms": load_rooms(day)}


# ---------------------------------------------------------------------------
# Расписание
# ---------------------------------------------------------------------------


@app.get("/getLessonByGroup/{group}", tags=["schedule"])
async def get_lesson_by_group(
    group: str,
    date: str | None = Query(
        default=None,
        max_length=10,
        description="YYYY-MM-DD; по умолчанию последняя доступная дата",
    ),
) -> dict:
    day = _resolve_date(date)
    return _schedule_payload(day, _clean_lookup(group, "group", 100))


@app.get("/getLessonByTeacher/{teacher}", tags=["schedule"])
async def get_lesson_by_teacher(
    teacher: str,
    date: str | None = Query(
        default=None,
        max_length=10,
        description="YYYY-MM-DD; без даты — последняя дата с занятиями",
    ),
) -> dict:
    formatted_teacher = _teacher_lookup(teacher)
    logger.info("Расписание преподавателя: %s", formatted_teacher)

    if date is not None:
        day = _resolve_date(date)
        return _teacher_payload(day, formatted_teacher)

    dates = teacher_dates(formatted_teacher)
    if not dates:
        raise HTTPException(
            status_code=404,
            detail=f"Преподаватель {formatted_teacher} не найден в расписании",
        )

    return _teacher_payload(dates[-1], formatted_teacher, dates=dates)


@app.get("/getLessonByRoom/{room:path}", tags=["schedule"])
async def get_lesson_by_room(
    room: str,
    date: str | None = Query(
        default=None,
        max_length=10,
        description="YYYY-MM-DD; без даты — последняя дата с занятиями",
    ),
) -> dict:
    formatted_room = _room_lookup(room)
    logger.info("Расписание кабинета: %s", formatted_room)

    if date is not None:
        day = _resolve_date(date)
        return _room_payload(day, formatted_room)

    dates = room_dates(formatted_room)
    if not dates:
        raise HTTPException(
            status_code=404,
            detail=f"Кабинет {formatted_room} не найден в расписании",
        )

    return _room_payload(dates[-1], formatted_room, dates=dates)


# ---------------------------------------------------------------------------
# Push-уведомления
# ---------------------------------------------------------------------------


@app.post("/register", tags=["push"])
async def register(
    request: Request,
    payload: RegisterRequest,
    _: None = Depends(_require_register_key),
) -> dict:
    if not _register_limiter.allow(_client_key(request)):
        raise HTTPException(
            status_code=429,
            detail="Слишком много регистраций. Попробуйте позже",
        )

    try:
        init_tokens_db()
        with token_connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM device_tokens WHERE token = ?",
                (payload.token,),
            ).fetchone()

            if not exists:
                count = connection.execute(
                    "SELECT COUNT(*) FROM device_tokens"
                ).fetchone()[0]
                if count >= MAX_DEVICE_TOKENS:
                    raise HTTPException(
                        status_code=429,
                        detail="Лимит зарегистрированных устройств исчерпан",
                    )

                connection.execute(
                    "INSERT INTO device_tokens(token) VALUES (?)",
                    (payload.token,),
                )

        return {"status": "ok"}
    except HTTPException:
        raise
    except sqlite3.Error:
        logger.exception("Не удалось сохранить push-токен")
        raise HTTPException(
            status_code=500,
            detail="Не удалось сохранить устройство",
        ) from None


@app.post("/send_all", tags=["push"])
async def send_all_endpoint(
    request: Request,
    payload: PushRequest | None = Body(default=None),
    title: str | None = Query(default=None, max_length=200),
    body: str | None = Query(default=None, max_length=2000),
    _: None = Depends(_require_admin),
) -> dict:
    if not _send_limiter.allow(_client_key(request)):
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов на отправку",
        )

    final_title = payload.title if payload is not None else (title or "Привет 👋")
    final_body = (
        payload.body
        if payload is not None
        else (body or "Push от FastAPI сервера")
    )

    try:
        return send_all(final_title, final_body)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Ошибка отправки push-уведомлений")
        raise HTTPException(
            status_code=502,
            detail="Не удалось отправить push-уведомления",
        ) from exc


# ---------------------------------------------------------------------------
# Файлы
# ---------------------------------------------------------------------------


@app.get("/bingo-download", include_in_schema=False)
async def download_bingo() -> FileResponse:
    file_path = BASE_DIR / "apks" / "bingo.apk"
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(
        path=file_path,
        filename="bingo.apk",
        media_type="application/vnd.android.package-archive",
    )


@app.get("/webapp", response_class=HTMLResponse, include_in_schema=False)
async def webapp() -> str:
    index_path = BASE_DIR / "index.html"
    return index_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Старые alias'ы (скрыты из документации)
# ---------------------------------------------------------------------------


@app.get("/groups", include_in_schema=False)
async def legacy_groups(date: str | None = Query(default=None)) -> dict:
    if date is not None:
        return {
            "type": "group",
            "groups": load_groups(_resolve_date(date)),
        }
    return {"type": "group", "groups": load_groups()}


@app.get("/dates", include_in_schema=False)
async def legacy_dates() -> dict:
    return {"dates": available_dates()}


@app.get("/lessonsByGroup/{date}/{group}", include_in_schema=False)
@app.get("/schedule/{date}/{group}", include_in_schema=False)
async def legacy_lessons_by_group(group: str, date: str) -> dict:
    day = _validate_date(date)
    return _schedule_payload(day, _clean_lookup(group, "group", 100))


@app.get("/teacher/{date}/{teacher}", include_in_schema=False)
async def legacy_teacher_by_date(date: str, teacher: str) -> dict:
    day = _validate_date(date)
    _require_database(day)
    return _teacher_payload(day, _teacher_lookup(teacher))


@app.get("/teacher/{teacher}", include_in_schema=False)
async def legacy_teacher_latest(
    teacher: str,
    date: str | None = Query(default=None),
) -> dict:
    formatted_teacher = _teacher_lookup(teacher)

    if date is not None:
        return _teacher_payload(_resolve_date(date), formatted_teacher)

    dates = teacher_dates(formatted_teacher)
    if not dates:
        raise HTTPException(
            status_code=404,
            detail=f"Преподаватель {formatted_teacher} не найден в расписании",
        )
    return _teacher_payload(dates[-1], formatted_teacher, dates=dates)


@app.get("/teachers", include_in_schema=False)
async def legacy_all_teachers() -> dict:
    return {"teachers": load_all_teachers()}


@app.get("/teachers/{date}", include_in_schema=False)
async def legacy_teachers_by_date(date: str) -> dict:
    day = _validate_date(date)
    _require_database(day)
    return {"date": day, "teachers": load_teachers(day)}


@app.get("/rooms", include_in_schema=False)
async def legacy_rooms_latest() -> dict:
    day = _resolve_date(None)
    return {"date": day, "rooms": load_rooms(day)}


@app.get("/rooms/{date}", include_in_schema=False)
async def legacy_rooms_by_date(date: str) -> dict:
    day = _validate_date(date)
    _require_database(day)
    return {"date": day, "rooms": load_rooms(day)}


@app.get("/room/{date}/{room:path}", include_in_schema=False)
async def legacy_room_by_date(date: str, room: str) -> dict:
    day = _validate_date(date)
    return _room_payload(day, _room_lookup(room))


# Совместимость со старым webapp: /YYYY-MM-DD/ГРУППА.
# Регистрируется последним, чтобы не перехватывать другие маршруты.
app.add_api_route(
    "/{date}/{group}",
    legacy_lessons_by_group,
    methods=["GET"],
    include_in_schema=False,
)
