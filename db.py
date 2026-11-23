"""Работа с базами данных расписания.

Каждый учебный день хранится в отдельном файле ``databases/YYYY-MM-DD.db``.
Модуль намеренно не знает ничего про FastAPI: его можно использовать из
парсера, тестов и любых других скриптов.

Все публичные функции принимают дату в формате ISO (``YYYY-MM-DD``) и
проверяют её перед обращением к файлу, поэтому подставить путь через
параметр ``date`` невозможно.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger("db")

BASE_DIR = Path(__file__).resolve().parent
DATABASES_DIR = BASE_DIR / "databases"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WHITESPACE_RE = re.compile(r"\s+")
_TEACHER_RE = re.compile(r"[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ]\.[А-ЯЁ]\.?")
_ROOM_SLASH_RE = re.compile(r"\s*([/-])\s*")
_DIGIT_SPACE_RE = re.compile(r"(?<=\d)\s+(?=\d)")
_ROOM_NUMBER_RE = re.compile(
    r"^\d{1,4}[А-ЯЁA-Z]?(?:[/-]\d{1,4}[А-ЯЁA-Z]?)*(?:/ДО)?$"
)
_ROOM_NAME_RE = re.compile(
    r"^[А-ЯЁA-Z][а-яёa-z]+(?:[ -][А-ЯЁA-Zа-яёa-z0-9]+)*$"
)

_teacher_index_lock = threading.Lock()
_teacher_index: _TeacherIndex | None = None
_room_index_lock = threading.Lock()
_room_index: _RoomIndex | None = None

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    `group` TEXT NOT NULL,
    lessonNumber INTEGER NOT NULL,
    subject TEXT NOT NULL,
    teacher TEXT NOT NULL,
    room TEXT NOT NULL,
    DO INTEGER NOT NULL,
    UNIQUE(date, `group`, lessonNumber)
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_schedule_date_group
    ON schedule_entries(date, `group`, lessonNumber);
CREATE INDEX IF NOT EXISTS idx_schedule_date_teacher
    ON schedule_entries(date, teacher);
"""


@dataclass(slots=True)
class ScheduleEntry:
    """Одна пара в расписании."""

    id: int | None
    date: str
    group: str
    lessonNumber: int
    subject: str
    teacher: str
    room: str
    DO: bool


@dataclass(frozen=True, slots=True)
class _TeacherIndex:
    """Кэш всех преподавателей и дат, в которые они ведут занятия."""

    signature: tuple[tuple[str, int, int], ...]
    teachers: dict[str, str]
    dates: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _RoomIndex:
    """Кэш всех кабинетов и дат, в которые они используются."""

    signature: tuple[tuple[str, int, int], ...]
    rooms: dict[str, str]
    dates: dict[str, tuple[str, ...]]


# ---------------------------------------------------------------------------
# Пути и валидация
# ---------------------------------------------------------------------------


def normalize_date(value: str | Date) -> str:
    """Возвращает дату в ISO-формате или бросает ``ValueError``."""
    if isinstance(value, Date):
        return value.isoformat()

    if not isinstance(value, str):
        raise ValueError("Дата должна быть строкой в формате YYYY-MM-DD")

    value = value.strip()
    if not _ISO_DATE_RE.fullmatch(value):
        raise ValueError(f"Некорректный формат даты: {value!r}")

    try:
        Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Некорректная дата: {value!r}") from exc

    return value


def normalize_group(value: object) -> str:
    """Убирает переводы строк и повторные пробелы в названии группы."""
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _extract_teachers(value: object) -> list[str]:
    """Разбивает ячейку с преподавателями на отдельные ФИО.

    В одной ячейке может быть несколько преподавателей:
    ``Трефилова Н.В. Немкова Н.Н.`` → два отдельных преподавателя.
    """
    text = normalize_group(value)
    if not text:
        return []

    matches = _TEACHER_RE.findall(text)
    names = matches or [text]
    return list(dict.fromkeys(names))


def normalize_room(value: object) -> str:
    """Нормализует номер кабинета.

    В PDF иногда встречаются пробелы внутри номера: ``1 1 2`` или
    ``2 1 3 /2 1 6``. Приводим их к ``112`` и ``213/216``.
    """
    text = normalize_group(value)
    if not text:
        return ""

    text = _ROOM_SLASH_RE.sub(r"\1", text)
    return _DIGIT_SPACE_RE.sub("", text).strip()


def is_plausible_room(value: object) -> bool:
    """Отсеивает явный OCR-мусор из колонки кабинетов."""
    name = normalize_room(value)
    if not name:
        return False

    return bool(
        _ROOM_NUMBER_RE.fullmatch(name)
        or _ROOM_NAME_RE.fullmatch(name)
    )


_SUBJECT_MARKERS = (
    "Линейка",
    "Классный час",
    "СЕССИЯ",
    "Экзамен",
    "Зачёт",
    "Зачет",
    "Консультация",
)
_SUBJECT_LETTER_RE = re.compile(r"[^A-Za-zА-Яа-яЁё]")


def _subject_letter_count(word: str) -> int:
    return len(_SUBJECT_LETTER_RE.sub("", word))


def _is_garbage_subject(text: str) -> bool:
    """Похоже ли название предмета на OCR-кашу из отдельных букв."""
    letters = _SUBJECT_LETTER_RE.sub("", text)
    if not letters:
        return True

    words = [
        word
        for word in text.split()
        if _subject_letter_count(word) > 0
    ]
    if not words:
        return True

    single_letter_words = sum(
        1 for word in words if _subject_letter_count(word) == 1
    )
    if len(words) >= 4 and single_letter_words / len(words) >= 0.45:
        return True

    if len(words) >= 6:
        average_length = sum(
            _subject_letter_count(word) for word in words
        ) / len(words)
        if average_length <= 2.3:
            return True

    return False


def clean_subject(value: object) -> str | None:
    """Очищает название пары и отбрасывает OCR-мусор.

    Возвращает ``None``, если запись нужно выбросить. Заглушки
    ``_______`` удаляются. Если от строки остаётся известный маркер
    вроде ``Линейка``, он сохраняется без мусорного хвоста.
    """
    text = normalize_group(value)
    if not text:
        return None

    text = normalize_group(re.sub(r"_{2,}", " ", text))
    if not text:
        return None

    if not _is_garbage_subject(text):
        return text

    folded = text.casefold()
    for marker in _SUBJECT_MARKERS:
        if folded.startswith(marker.casefold()):
            return marker

    return None


def database_path(value: str | Date) -> Path:
    """Путь к базе за конкретную дату (без выхода за пределы ``databases``)."""
    return DATABASES_DIR / f"{normalize_date(value)}.db"


def database_exists(value: str | Date) -> bool:
    return database_path(value).is_file()


def available_dates() -> list[str]:
    """Отсортированный список дат, для которых есть файл базы."""
    if not DATABASES_DIR.is_dir():
        return []

    return sorted(
        path.stem
        for path in DATABASES_DIR.glob("*.db")
        if path.is_file() and _ISO_DATE_RE.fullmatch(path.stem)
    )


def latest_database_path() -> Path | None:
    dates = available_dates()
    return database_path(dates[-1]) if dates else None


# ---------------------------------------------------------------------------
# Подключение к SQLite
# ---------------------------------------------------------------------------


@contextmanager
def connect(
    db_path: str | Path,
    *,
    read_only: bool = False,
    create: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Контекстный менеджер SQLite с корректным commit/rollback/close."""
    path = Path(db_path)

    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise FileNotFoundError(path)

    if read_only:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
    else:
        connection = sqlite3.connect(path, timeout=5)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")

    try:
        yield connection
        if not read_only:
            connection.commit()
    except Exception:
        if not read_only:
            connection.rollback()
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------


def init_db(db_path: str | Path) -> None:
    """Создаёт схему в указанном файле базы (если её ещё нет)."""
    path = Path(db_path)
    logger.info("Инициализация БД: %s", path)

    with connect(path, create=True) as connection:
        connection.execute(DB_SCHEMA)

        # На случай очень старых баз, где ещё не было колонки `group`.
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(schedule_entries)")
        }
        if columns and "group" not in columns:
            logger.warning("Добавляю колонку `group` в старую БД: %s", path)
            connection.execute(
                "ALTER TABLE schedule_entries "
                "ADD COLUMN `group` TEXT NOT NULL DEFAULT ''"
            )

        connection.executescript(INDEX_SCHEMA)


def delete_by_date(db_path: str | Path, date: str | Date) -> int:
    """Удаляет все записи за дату. Возвращает число удалённых строк."""
    path = Path(db_path)
    day = normalize_date(date)

    if not path.is_file():
        return 0

    logger.info("Удаление записей за дату %s из %s", day, path)
    with connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM schedule_entries WHERE date = ?",
            (day,),
        )
        return cursor.rowcount


def save_entries(
    db_path: str | Path,
    entries: Iterable[ScheduleEntry],
) -> int:
    """Полностью перезаписывает расписание за дату.

    Дубликаты по ключу ``(date, group, lessonNumber)`` схлопываются:
    остаётся последняя запись. Это защищает от повторяющихся строк,
    которые иногда появляются при разборе PDF.
    """
    path = Path(db_path)
    items = list(entries)

    if not items:
        logger.warning("Пустой список записей — сохранение пропущено")
        return 0

    day = normalize_date(items[0].date)
    dates = {normalize_date(item.date) for item in items}
    if dates != {day}:
        raise ValueError("Все записи должны относиться к одной дате")

    if not path.is_file():
        init_db(path)

    rows: dict[tuple[str, str, int], tuple] = {}
    skipped = 0

    for item in items:
        group = normalize_group(item.group)
        if not group:
            raise ValueError("У записи расписания отсутствует группа")

        try:
            lesson_number = int(item.lessonNumber)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Некорректный номер пары: {item.lessonNumber!r}"
            ) from exc

        subject = clean_subject(item.subject)
        if subject is None:
            skipped += 1
            continue

        room = normalize_room(item.room)
        if not is_plausible_room(room):
            room = ""

        key = (day, group, lesson_number)
        rows[key] = (
            day,
            group,
            lesson_number,
            subject,
            normalize_group(item.teacher),
            room,
            1 if item.DO else 0,
        )

    if not rows:
        logger.warning(
            "Все %d записей за %s отфильтрованы — БД не изменена",
            len(items),
            day,
        )
        return 0

    logger.info(
        "Сохранение %d записей за %s (уникальных: %d, пропущено: %d) в %s",
        len(items),
        day,
        len(rows),
        skipped,
        path,
    )

    with connect(path) as connection:
        connection.execute(
            "DELETE FROM schedule_entries WHERE date = ?",
            (day,),
        )
        connection.executemany(
            """
            INSERT INTO schedule_entries (
                date, `group`, lessonNumber,
                subject, teacher, room, DO
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            list(rows.values()),
        )

    logger.info("Записи успешно сохранены")
    return len(rows)


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


def _resolve_group(
    connection: sqlite3.Connection,
    date: str,
    group: str,
) -> str | None:
    """Ищет реальное название группы, учитывая лишние пробелы/переводы строк."""
    row = connection.execute(
        """
        SELECT `group`
        FROM schedule_entries
        WHERE date = ? AND `group` = ?
        LIMIT 1
        """,
        (date, group),
    ).fetchone()
    if row:
        return row["group"]

    target = normalize_group(group).casefold()
    for row in connection.execute(
        "SELECT DISTINCT `group` FROM schedule_entries WHERE date = ?",
        (date,),
    ):
        if normalize_group(row["group"]).casefold() == target:
            return row["group"]

    return None


def _lesson_to_dict(row: sqlite3.Row) -> dict | None:
    subject = clean_subject(row["subject"])
    if subject is None:
        return None

    lesson_number = int(row["lessonNumber"])
    return {
        "id": row["id"],
        "date": row["date"],
        "group": normalize_group(row["group"]),
        "lessonNumber": lesson_number,
        # Оставлено для старых клиентов, которые читали строковый `number`.
        "number": str(lesson_number),
        "subject": subject,
        "teacher": row["teacher"],
        "room": normalize_room(row["room"]),
        "DO": int(row["DO"]),
    }


def _rows_to_lessons(rows: list[sqlite3.Row]) -> list[dict]:
    return [
        lesson
        for row in rows
        if (lesson := _lesson_to_dict(row)) is not None
    ]


def load_schedule(group: str, date: str | Date) -> list[dict]:
    """Расписание указанной группы за день."""
    try:
        day = normalize_date(date)
        path = database_path(day)
    except ValueError:
        return []

    if not path.is_file():
        logger.warning("База за %s не найдена", day)
        return []

    with connect(path, read_only=True) as connection:
        actual_group = _resolve_group(connection, day, normalize_group(group))
        if actual_group is None:
            return []

        rows = connection.execute(
            """
            SELECT id, date, `group`, lessonNumber, subject, teacher, room, DO
            FROM schedule_entries
            WHERE date = ? AND `group` = ?
            ORDER BY lessonNumber, id
            """,
            (day, actual_group),
        ).fetchall()

    return _rows_to_lessons(rows)


def load_groups(date: str | Date | None = None) -> list[dict]:
    """Список групп за день (по умолчанию — из самой свежей базы)."""
    if date is None:
        path = latest_database_path()
    else:
        try:
            path = database_path(date)
        except ValueError:
            return []

    if path is None or not path.is_file():
        return []

    with connect(path, read_only=True) as connection:
        rows = connection.execute(
            'SELECT DISTINCT `group` FROM schedule_entries'
        ).fetchall()

    groups: dict[str, str] = {}
    for row in rows:
        name = normalize_group(row["group"])
        if name:
            groups.setdefault(name.casefold(), name)

    return [
        {"group": groups[key]}
        for key in sorted(groups)
    ]


def load_teachers(date: str | Date) -> list[dict]:
    """Список преподавателей за день."""
    try:
        day = normalize_date(date)
        path = database_path(day)
    except ValueError:
        return []

    if not path.is_file():
        return []

    with connect(path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT teacher
            FROM schedule_entries
            WHERE date = ? AND TRIM(teacher) <> ''
            ORDER BY teacher
            """,
            (day,),
        ).fetchall()

    teachers: dict[str, str] = {}
    for row in rows:
        for name in _extract_teachers(row["teacher"]):
            teachers.setdefault(name.casefold(), name)

    return [
        {"teacher": teachers[key]}
        for key in sorted(teachers)
    ]


def _database_signature() -> tuple[tuple[str, int, int], ...]:
    """Подпись всех баз: имя, mtime и размер.

    По ней индекс понимает, что появилась новая база или изменилась старая.
    """
    if not DATABASES_DIR.is_dir():
        return ()

    signature = []
    for path in sorted(DATABASES_DIR.glob("*.db")):
        if not path.is_file() or not _ISO_DATE_RE.fullmatch(path.stem):
            continue

        try:
            stat = path.stat()
        except OSError:
            continue

        signature.append((path.name, stat.st_mtime_ns, stat.st_size))

    return tuple(signature)


def _build_teacher_index(
    signature: tuple[tuple[str, int, int], ...],
) -> _TeacherIndex:
    teachers: dict[str, str] = {}
    dates: dict[str, list[str]] = {}

    for filename, _, _ in signature:
        day = Path(filename).stem
        path = DATABASES_DIR / filename

        try:
            with connect(path, read_only=True) as connection:
                rows = connection.execute(
                    """
                    SELECT DISTINCT teacher
                    FROM schedule_entries
                    WHERE date = ? AND TRIM(teacher) <> ''
                    """,
                    (day,),
                ).fetchall()
        except (sqlite3.Error, OSError):
            logger.exception("Не удалось проиндексировать преподавателей: %s", path)
            continue

        for row in rows:
            for name in _extract_teachers(row["teacher"]):
                key = name.casefold()
                teachers.setdefault(key, name)
                dates.setdefault(key, []).append(day)

    return _TeacherIndex(
        signature=signature,
        teachers=teachers,
        dates={
            key: tuple(sorted(set(values)))
            for key, values in dates.items()
        },
    )


def _ensure_teacher_index() -> _TeacherIndex:
    """Возвращает актуальный индекс преподавателей.

    Индекс строится один раз и автоматически пересобирается, когда меняется
    набор баз или время модификации хотя бы одной из них.
    """
    global _teacher_index

    signature = _database_signature()
    cached = _teacher_index
    if cached is not None and cached.signature == signature:
        return cached

    with _teacher_index_lock:
        cached = _teacher_index
        if cached is not None and cached.signature == signature:
            return cached

        index = _build_teacher_index(signature)
        _teacher_index = index
        logger.info(
            "Индекс преподавателей обновлён: %d преподавателей, %d баз",
            len(index.teachers),
            len(signature),
        )
        return index


def load_all_teachers() -> list[dict]:
    """Все преподаватели из всех баз, без привязки к конкретному дню."""
    index = _ensure_teacher_index()
    return [
        {"teacher": index.teachers[key]}
        for key in sorted(index.teachers)
    ]


def teacher_dates(teacher: str) -> list[str]:
    """Все даты, когда у преподавателя есть занятия."""
    target = normalize_group(teacher).casefold()
    if not target:
        return []

    index = _ensure_teacher_index()
    return list(index.dates.get(target, ()))


def latest_teacher_date(teacher: str) -> str | None:
    """Самая свежая дата с занятиями преподавателя."""
    dates = teacher_dates(teacher)
    return dates[-1] if dates else None


def _build_room_index(
    signature: tuple[tuple[str, int, int], ...],
) -> _RoomIndex:
    rooms: dict[str, str] = {}
    dates: dict[str, list[str]] = {}

    for filename, _, _ in signature:
        day = Path(filename).stem
        path = DATABASES_DIR / filename

        try:
            with connect(path, read_only=True) as connection:
                rows = connection.execute(
                    """
                    SELECT DISTINCT room
                    FROM schedule_entries
                    WHERE date = ? AND TRIM(room) <> ''
                    """,
                    (day,),
                ).fetchall()
        except (sqlite3.Error, OSError):
            logger.exception("Не удалось проиндексировать кабинеты: %s", path)
            continue

        for row in rows:
            name = normalize_room(row["room"])
            if not is_plausible_room(name):
                continue

            key = name.casefold()
            rooms.setdefault(key, name)
            dates.setdefault(key, []).append(day)

    return _RoomIndex(
        signature=signature,
        rooms=rooms,
        dates={
            key: tuple(sorted(set(values)))
            for key, values in dates.items()
        },
    )


def _ensure_room_index() -> _RoomIndex:
    """Возвращает актуальный индекс кабинетов."""
    global _room_index

    signature = _database_signature()
    cached = _room_index
    if cached is not None and cached.signature == signature:
        return cached

    with _room_index_lock:
        cached = _room_index
        if cached is not None and cached.signature == signature:
            return cached

        index = _build_room_index(signature)
        _room_index = index
        logger.info(
            "Индекс кабинетов обновлён: %d кабинетов, %d баз",
            len(index.rooms),
            len(signature),
        )
        return index


def load_all_rooms() -> list[dict]:
    """Все кабинеты из всех баз, без привязки к конкретному дню."""
    index = _ensure_room_index()
    return [
        {"room": index.rooms[key]}
        for key in sorted(index.rooms)
    ]


def room_dates(room: str) -> list[str]:
    """Все даты, в которые используется кабинет."""
    target = normalize_room(room).casefold()
    if not target:
        return []

    index = _ensure_room_index()
    return list(index.dates.get(target, ()))


def latest_room_date(room: str) -> str | None:
    """Самая свежая дата, в которую используется кабинет."""
    dates = room_dates(room)
    return dates[-1] if dates else None


def load_rooms(date: str | Date) -> list[dict]:
    """Список кабинетов, в которых есть занятия за день."""
    try:
        day = normalize_date(date)
        path = database_path(day)
    except ValueError:
        return []

    if not path.is_file():
        return []

    with connect(path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT room
            FROM schedule_entries
            WHERE date = ? AND TRIM(room) <> ''
            ORDER BY room
            """,
            (day,),
        ).fetchall()

    rooms: dict[str, str] = {}
    for row in rows:
        name = normalize_room(row["room"])
        if is_plausible_room(name):
            rooms.setdefault(name.casefold(), name)

    return [
        {"room": rooms[key]}
        for key in sorted(rooms)
    ]


def load_room_schedule(date: str | Date, room: str) -> list[dict]:
    """Расписание занятий в указанном кабинете за день."""
    try:
        day = normalize_date(date)
        path = database_path(day)
    except ValueError:
        return []

    if not path.is_file():
        return []

    target = normalize_room(room).casefold()
    if not target:
        return []

    with connect(path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT id, date, `group`, lessonNumber, subject, teacher, room, DO
            FROM schedule_entries
            WHERE date = ?
            ORDER BY lessonNumber, id
            """,
            (day,),
        ).fetchall()

    result = []
    for row in rows:
        normalized_room = normalize_room(row["room"]).casefold()
        if normalized_room == target:
            lesson = _lesson_to_dict(row)
            if lesson is not None:
                result.append(lesson)

    return result


def load_teacher_schedule(date: str | Date, teacher: str) -> list[dict]:
    """Расписание преподавателя за день.

    В одной ячейке может быть несколько преподавателей, поэтому ищем
    вхождение нормализованного имени в нормализованную строку.
    """
    try:
        day = normalize_date(date)
        path = database_path(day)
    except ValueError:
        return []

    if not path.is_file():
        return []

    target = normalize_group(teacher).casefold()
    if not target:
        return []

    with connect(path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT id, date, `group`, lessonNumber, subject, teacher, room, DO
            FROM schedule_entries
            WHERE date = ?
            ORDER BY lessonNumber, id
            """,
            (day,),
        ).fetchall()

    result = []
    for row in rows:
        normalized_teacher = normalize_group(row["teacher"]).casefold()
        if target in normalized_teacher:
            lesson = _lesson_to_dict(row)
            if lesson is not None:
                result.append(lesson)

    return result
