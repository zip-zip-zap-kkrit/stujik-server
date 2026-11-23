"""Тесты API и слоя работы с базами данных.

Запуск:

    START_SCHEDULE_PARSER=0 python -m unittest discover -s tests -v

Тесты используют реальную группу ``9ПР-2.25`` из учебных баз.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

# Отключаем автозапуск парсера до импорта server.
os.environ["START_SCHEDULE_PARSER"] = "0"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["REGISTER_API_KEY"] = "test-register-key"
_TEST_TOKENS_DIR = tempfile.mkdtemp(prefix="schedule-tokens-")
os.environ["TOKENS_DB"] = str(Path(_TEST_TOKENS_DIR) / "tokens.db")

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
from db import ScheduleEntry, clean_subject, database_path, normalize_date  # noqa: E402
from pdfparser import parse_pdf  # noqa: E402
from server import app  # noqa: E402
from utils import parse_date_from_filename  # noqa: E402

GROUP = "9ПР-2.25"
DATE = "2026-10-01"
LATEST_DATE = db.available_dates()[-1]
TEACHER_LATEST_DATE = db.latest_teacher_date("Кетрова А.А.")
PROJECT_DIR = Path(__file__).resolve().parents[1]
PDF_PATH = PROJECT_DIR / "data" / "pdfs" / "01.10.2026.pdf"
ADMIN_HEADERS = {"X-API-Key": os.environ["ADMIN_API_KEY"]}
REGISTER_HEADERS = {"X-API-Key": os.environ["REGISTER_API_KEY"]}


def tearDownModule() -> None:
    shutil.rmtree(_TEST_TOKENS_DIR, ignore_errors=True)


class ApiTest(unittest.TestCase):
    """Проверки HTTP-контрактов."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertGreater(payload["dates"], 0)
        self.assertIsNotNone(payload["latest_date"])

    def test_version(self) -> None:
        response = self.client.get("/ver")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ver", response.json())

    def test_groups_contains_target_group(self) -> None:
        response = self.client.get("/groups")
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertEqual(payload["type"], "group")
        groups = [item["group"] for item in payload["groups"]]
        self.assertIn(GROUP, groups)
        self.assertTrue(all("\n" not in item for item in groups))

    def test_schedule_for_group(self) -> None:
        response = self.client.get(f"/lessonsByGroup/{DATE}/{GROUP}")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["date"], DATE)
        self.assertEqual(payload["group"], GROUP)
        self.assertEqual(len(payload["schedule"]), 4)

        lessons = payload["schedule"]
        self.assertEqual([lesson["lessonNumber"] for lesson in lessons], [1, 2, 3, 4])
        self.assertEqual([lesson["number"] for lesson in lessons], ["1", "2", "3", "4"])

        first = lessons[0]
        self.assertEqual(first["subject"], "Архитектура аппаратных средств")
        self.assertEqual(first["teacher"], "Кетрова А.А.")
        self.assertEqual(first["room"], "203")
        self.assertIsInstance(first["DO"], int)

    def test_schedule_subjects_are_clean(self) -> None:
        response = self.client.get(
            f"/getLessonByGroup/{GROUP}?date={LATEST_DATE}"
        )
        self.assertEqual(response.status_code, 200, response.text)

        subjects = [lesson["subject"] for lesson in response.json()["schedule"]]
        self.assertTrue(subjects)
        self.assertNotIn("_______", subjects)
        for subject in subjects:
            self.assertEqual(clean_subject(subject), subject)

    def test_schedule_route_aliases(self) -> None:
        paths = [
            f"/schedule/{DATE}/{GROUP}",
            f"/{DATE}/{GROUP}",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["group"], GROUP)

    def test_groups_for_date(self) -> None:
        response = self.client.get(f"/groups?date={DATE}")
        self.assertEqual(response.status_code, 200)
        groups = [item["group"] for item in response.json()["groups"]]
        self.assertIn(GROUP, groups)

    def test_unknown_group_returns_404(self) -> None:
        response = self.client.get(f"/lessonsByGroup/{DATE}/НЕТ-ТАКОЙ-ГРУППЫ")
        self.assertEqual(response.status_code, 404)

    def test_missing_date_returns_404(self) -> None:
        response = self.client.get("/lessonsByGroup/1999-01-01/9ПР-2.25")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Последняя доступная дата", response.json()["detail"])

    def test_invalid_date_returns_400(self) -> None:
        for value in ("not-a-date", "2026-13-40"):
            with self.subTest(value=value):
                response = self.client.get(f"/lessonsByGroup/{value}/{GROUP}")
                self.assertEqual(response.status_code, 400, response.text)

    def test_teachers(self) -> None:
        response = self.client.get(f"/teachers/{DATE}")
        self.assertEqual(response.status_code, 200)

        teachers = [item["teacher"] for item in response.json()["teachers"]]
        self.assertIn("Кетрова А.А.", teachers)

    def test_teacher_schedule(self) -> None:
        response = self.client.get(f"/teacher/{DATE}/Кетрова--А.А.")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["teacher"], "Кетрова А.А.")
        self.assertTrue(payload["schedule"])
        self.assertIn(GROUP, {lesson["group"] for lesson in payload["schedule"]})

    def test_all_teachers_are_indexed(self) -> None:
        all_response = self.client.get("/teachers")
        day_response = self.client.get(f"/teachers/{DATE}")
        self.assertEqual(all_response.status_code, 200, all_response.text)
        self.assertEqual(day_response.status_code, 200, day_response.text)

        all_teachers = {
            item["teacher"] for item in all_response.json()["teachers"]
        }
        day_teachers = {
            item["teacher"] for item in day_response.json()["teachers"]
        }
        self.assertIn("Кетрова А.А.", all_teachers)
        self.assertGreater(len(all_teachers), len(day_teachers))
        self.assertTrue(day_teachers.issubset(all_teachers))

    def test_teacher_latest_without_date(self) -> None:
        response = self.client.get("/teacher/Кетрова--А.А.")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["teacher"], "Кетрова А.А.")
        self.assertEqual(payload["date"], TEACHER_LATEST_DATE)
        self.assertIn(TEACHER_LATEST_DATE, payload["dates"])
        self.assertGreater(len(payload["dates"]), 1)
        self.assertTrue(payload["schedule"])

    def test_teacher_latest_with_date_parameter(self) -> None:
        response = self.client.get(
            "/teacher/Кетрова--А.А.",
            params={"date": "2026-09-30"},
        )
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["date"], "2026-09-30")
        self.assertNotIn("dates", payload)

    def test_unknown_teacher_returns_404(self) -> None:
        response = self.client.get("/teacher/НЕТ--Н.Н.")
        self.assertEqual(response.status_code, 404)

    def test_readable_reference_endpoints(self) -> None:
        response = self.client.get("/getDates")
        self.assertEqual(response.status_code, 200)
        self.assertIn(DATE, response.json()["dates"])

        response = self.client.get("/getGroups")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["date"], LATEST_DATE)
        groups = [item["group"] for item in response.json()["groups"]]
        self.assertIn(GROUP, groups)

        response = self.client.get(f"/getTeachers?date={DATE}")
        self.assertEqual(response.status_code, 200, response.text)
        teachers = [item["teacher"] for item in response.json()["teachers"]]
        self.assertIn("Кетрова А.А.", teachers)

        response = self.client.get("/getAllTeachers")
        self.assertEqual(response.status_code, 200, response.text)
        all_teachers = [item["teacher"] for item in response.json()["teachers"]]
        self.assertIn("Кетрова А.А.", all_teachers)
        self.assertGreaterEqual(len(all_teachers), len(teachers))

        response = self.client.get(f"/getRooms?date={DATE}")
        self.assertEqual(response.status_code, 200, response.text)
        rooms = [item["room"] for item in response.json()["rooms"]]
        self.assertIn("209", rooms)

        response = self.client.get("/getAllRooms")
        self.assertEqual(response.status_code, 200, response.text)
        all_rooms = {item["room"] for item in response.json()["rooms"]}
        latest_rooms = {
            item["room"] for item in self.client.get("/getRooms").json()["rooms"]
        }
        self.assertTrue(latest_rooms.issubset(all_rooms))
        self.assertGreater(len(all_rooms), len(latest_rooms))
        self.assertIn("317", all_rooms)

    def test_readable_lesson_by_group(self) -> None:
        cases = (
            ("", LATEST_DATE),
            (f"?date={DATE}", DATE),
        )
        for suffix, expected_date in cases:
            with self.subTest(suffix=suffix):
                response = self.client.get(
                    f"/getLessonByGroup/{GROUP}{suffix}"
                )
                self.assertEqual(response.status_code, 200, response.text)

                payload = response.json()
                self.assertEqual(payload["date"], expected_date)
                self.assertEqual(payload["group"], GROUP)
                self.assertTrue(payload["schedule"])

    def test_readable_lesson_by_teacher(self) -> None:
        response = self.client.get("/getLessonByTeacher/Кетрова--А.А.")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["date"], TEACHER_LATEST_DATE)
        self.assertEqual(payload["teacher"], "Кетрова А.А.")
        self.assertIn(TEACHER_LATEST_DATE, payload["dates"])

        response = self.client.get(
            "/getLessonByTeacher/Кетрова--А.А.",
            params={"date": "2026-09-30"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["date"], "2026-09-30")

    def test_readable_lesson_by_room(self) -> None:
        response = self.client.get(f"/getLessonByRoom/209?date={DATE}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["room"], "209")
        self.assertEqual(len(response.json()["schedule"]), 5)

        response = self.client.get(f"/getLessonByRoom/318/418?date={DATE}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["room"], "318/418")
        self.assertEqual(len(response.json()["schedule"]), 5)

    def test_readable_lesson_by_room_without_date(self) -> None:
        room = "317"
        expected_date = db.latest_room_date(room)
        if expected_date is None:
            self.skipTest(f"Кабинет {room} отсутствует в базах")

        response = self.client.get(f"/getLessonByRoom/{room}")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["date"], expected_date)
        self.assertEqual(payload["room"], room)
        self.assertIn(expected_date, payload["dates"])
        self.assertTrue(payload["schedule"])

    def test_openapi_contains_readable_routes(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)

        paths = response.json()["paths"]
        self.assertIn("/getAllTeachers", paths)
        self.assertIn("/getAllRooms", paths)
        self.assertIn("/getLessonByGroup/{group}", paths)
        self.assertIn("/getLessonByTeacher/{teacher}", paths)
        self.assertIn("/getLessonByRoom/{room}", paths)
        self.assertNotIn("/lessonsByGroup/{date}/{group}", paths)
        self.assertNotIn("/teacher/{date}/{teacher}", paths)

    def test_security_headers(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_rejects_too_large_body(self) -> None:
        response = self.client.post(
            "/register",
            json={"token": "test-device-token-1234567890"},
            headers={**REGISTER_HEADERS, "Content-Length": "2000000"},
        )
        self.assertEqual(response.status_code, 413)

    def test_send_all_without_devices(self) -> None:
        with closing(sqlite3.connect(os.environ["TOKENS_DB"])) as connection:
            connection.execute("DELETE FROM device_tokens")
            connection.commit()

        response = self.client.post(
            "/send_all",
            json={"title": "Тест", "body": "Проверка"},
            headers=ADMIN_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"success": 0, "failed": 0, "total": 0},
        )

        # Старый формат с query-параметрами тоже должен работать.
        response = self.client.post(
            "/send_all?title=Тест&body=Проверка",
            headers=ADMIN_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 0)

    def test_send_all_requires_admin_key(self) -> None:
        response = self.client.post("/send_all", json={"title": "x", "body": "y"})
        self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/send_all",
            json={"title": "x", "body": "y"},
            headers={"X-API-Key": "wrong-key"},
        )
        self.assertEqual(response.status_code, 401)

    def test_rooms(self) -> None:
        cases = (
            ("/rooms", LATEST_DATE),
            (f"/rooms/{DATE}", DATE),
        )
        for path, expected_date in cases:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)

                payload = response.json()
                self.assertEqual(payload["date"], expected_date)
                rooms = [item["room"] for item in payload["rooms"]]
                self.assertIn("209", rooms)
                self.assertIn("318/418", rooms)
                self.assertTrue(all(room.strip() for room in rooms))

    def test_room_schedule(self) -> None:
        response = self.client.get(f"/room/{DATE}/209")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["date"], DATE)
        self.assertEqual(payload["room"], "209")
        self.assertEqual(len(payload["schedule"]), 5)
        self.assertTrue(
            all(lesson["room"] == "209" for lesson in payload["schedule"])
        )
        self.assertIn(
            GROUP,
            {lesson["group"] for lesson in payload["schedule"]},
        )

    def test_room_schedule_with_slash_in_name(self) -> None:
        # Кабинеты вида 318/418 должны работать и как путь, и как %2F.
        for path in (
            f"/room/{DATE}/318/418",
            f"/room/{DATE}/318%2F418",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)

                payload = response.json()
                self.assertEqual(payload["room"], "318/418")
                self.assertEqual(len(payload["schedule"]), 5)

    def test_unknown_room_returns_404(self) -> None:
        response = self.client.get(f"/room/{DATE}/999")
        self.assertEqual(response.status_code, 404)

    def test_invalid_room_format_returns_400(self) -> None:
        response = self.client.get(f"/getLessonByRoom/НЕТ-ТАКОГО-КАБИНЕТА")
        self.assertEqual(response.status_code, 400)

    def test_register_is_idempotent(self) -> None:
        token = "test-device-token-1234567890"
        for _ in range(2):
            response = self.client.post(
                "/register",
                json={"token": token},
                headers=REGISTER_HEADERS,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})

        with closing(sqlite3.connect(os.environ["TOKENS_DB"])) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM device_tokens WHERE token = ?",
                (token,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_register_requires_key_and_valid_token(self) -> None:
        response = self.client.post(
            "/register",
            json={"token": "test-device-token-1234567890"},
        )
        self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/register",
            json={"token": "short token"},
            headers=REGISTER_HEADERS,
        )
        self.assertEqual(response.status_code, 422)

        response = self.client.post(
            "/register",
            json={"token": "bad token with spaces"},
            headers=REGISTER_HEADERS,
        )
        self.assertEqual(response.status_code, 422)

    def test_webapp_uses_canonical_api_route(self) -> None:
        response = self.client.get("/webapp")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/lessonsByGroup/", response.text)
        self.assertIn("encodeURIComponent(currentGroup)", response.text)


class DatabaseTest(unittest.TestCase):
    """Проверки записи и чтения SQLite."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.databases_dir = Path(self.temp_dir.name)
        self.patcher = patch.object(db, "DATABASES_DIR", self.databases_dir)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_save_and_load_entries(self) -> None:
        path = database_path(DATE)
        db.init_db(path)

        saved = db.save_entries(
            path,
            [
                ScheduleEntry(None, DATE, GROUP, 1, "Математика", "Иванов И.И.", "101", False),
                ScheduleEntry(None, DATE, GROUP, 2, "Физика", "Петров П.П.", "102", True),
                # Дубликат по ключу должен схлопнуться в последнюю запись.
                ScheduleEntry(None, DATE, GROUP, 1, "Информатика", "Сидоров С.С.", "103", False),
            ],
        )
        self.assertEqual(saved, 2)

        lessons = db.load_schedule(GROUP, DATE)
        self.assertEqual(len(lessons), 2)
        self.assertEqual(lessons[0]["subject"], "Информатика")
        self.assertEqual(lessons[0]["teacher"], "Сидоров С.С.")
        self.assertEqual(lessons[1]["DO"], 1)

    def test_garbage_subjects_are_filtered(self) -> None:
        path = database_path(DATE)
        db.init_db(path)

        saved = db.save_entries(
            path,
            [
                ScheduleEntry(None, DATE, GROUP, 1, "Математика", "Иванов И.И.", "101", False),
                ScheduleEntry(None, DATE, GROUP, 2, "_______", "", "", False),
                ScheduleEntry(None, DATE, GROUP, 3, "К Р л у а д с н с е н в ы", "", "", False),
                ScheduleEntry(None, DATE, GROUP, 4, "Линейка К Р л у а д с н 414", "", "414", False),
            ],
        )
        self.assertEqual(saved, 2)

        lessons = db.load_schedule(GROUP, DATE)
        self.assertEqual(
            [lesson["subject"] for lesson in lessons],
            ["Математика", "Линейка"],
        )

        # Старые базы могли сохранить мусор до появления фильтра.
        # При чтении он всё равно не должен попадать в API.
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                INSERT INTO schedule_entries
                    (date, `group`, lessonNumber, subject, teacher, room, DO)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (DATE, GROUP, 5, "_______ _____", "", "", 0),
            )
            connection.commit()

        lessons = db.load_schedule(GROUP, DATE)
        self.assertEqual(len(lessons), 2)
        self.assertNotIn("_______ _____", {lesson["subject"] for lesson in lessons})

    def test_groups_are_normalized(self) -> None:
        path = database_path(DATE)
        db.init_db(path)
        db.save_entries(
            path,
            [
                ScheduleEntry(None, DATE, "9ПР-2.25", 1, "A", "", "", False),
                ScheduleEntry(None, DATE, "9РПО-1.26\nПРОФ", 1, "B", "", "", False),
            ],
        )

        groups = [item["group"] for item in db.load_groups()]
        self.assertIn("9РПО-1.26 ПРОФ", groups)

        # Поиск идёт и по «красивому» названию с обычным пробелом.
        lessons = db.load_schedule("9РПО-1.26 ПРОФ", DATE)
        self.assertEqual(len(lessons), 1)

    @unittest.skipUnless(PDF_PATH.is_file(), "Нет PDF для интеграционного теста")
    def test_parse_pdf_and_save(self) -> None:
        entries = parse_pdf(PDF_PATH, DATE)
        self.assertGreater(len(entries), 0)

        path = database_path(DATE)
        saved = db.save_entries(path, entries)
        self.assertGreater(saved, 0)

        lessons = db.load_schedule(GROUP, DATE)
        self.assertEqual(len(lessons), 4)
        self.assertEqual(lessons[0]["subject"], "Архитектура аппаратных средств")

    def test_mixed_dates_are_rejected(self) -> None:
        path = database_path(DATE)
        db.init_db(path)

        with self.assertRaises(ValueError):
            db.save_entries(
                path,
                [
                    ScheduleEntry(None, DATE, GROUP, 1, "A", "", "", False),
                    ScheduleEntry(None, "2026-10-02", GROUP, 1, "B", "", "", False),
                ],
            )

    def test_date_validation_blocks_path_traversal(self) -> None:
        for value in ("../../tokens", "2026-10-01/../../tokens", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_date(value)

    def test_rooms_and_room_schedule(self) -> None:
        path = database_path(DATE)
        db.init_db(path)
        db.save_entries(
            path,
            [
                ScheduleEntry(None, DATE, GROUP, 1, "A", "Иванов И.И.", " 209 ", False),
                ScheduleEntry(None, DATE, "9ИС-1.24", 2, "B", "Петров П.П.", "318/418", False),
                ScheduleEntry(None, DATE, "9ИС-1.24", 3, "C", "", "1 1 2", False),
                ScheduleEntry(None, DATE, "9ИС-1.24", 4, "D", "", "312.бак76,йындобовСт-рп", False),
                ScheduleEntry(None, DATE, "9ИС-1.24", 5, "E", "", "ЯИСРУКСКЭ", False),
            ],
        )

        rooms = [item["room"] for item in db.load_rooms(DATE)]
        self.assertIn("209", rooms)
        self.assertIn("318/418", rooms)
        self.assertIn("112", rooms)
        self.assertNotIn("1 1 2", rooms)
        self.assertNotIn("312.бак76,йындобовСт-рп", rooms)
        self.assertNotIn("ЯИСРУКСКЭ", rooms)

        lessons = db.load_room_schedule(DATE, "209")
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["group"], GROUP)
        self.assertEqual(lessons[0]["room"], "209")

        self.assertEqual(db.load_room_schedule(DATE, "318"), [])
        self.assertEqual(len(db.load_room_schedule(DATE, "318/418")), 1)

        normalized_lessons = db.load_room_schedule(DATE, "112")
        self.assertEqual(len(normalized_lessons), 1)
        self.assertEqual(normalized_lessons[0]["room"], "112")

    def test_room_index_across_dates(self) -> None:
        first_date = DATE
        second_date = "2026-10-02"

        for day, rooms in (
            (first_date, ("101", "1 0 2")),
            (second_date, ("101", "202")),
        ):
            path = database_path(day)
            db.init_db(path)
            db.save_entries(
                path,
                [
                    ScheduleEntry(None, day, GROUP, index, "A", "", room, False)
                    for index, room in enumerate(rooms, start=1)
                ],
            )

        rooms = [item["room"] for item in db.load_all_rooms()]
        self.assertIn("101", rooms)
        self.assertIn("102", rooms)
        self.assertIn("202", rooms)

        self.assertEqual(db.room_dates("101"), [first_date, second_date])
        self.assertEqual(db.room_dates("102"), [first_date])
        self.assertEqual(db.latest_room_date("101"), second_date)
        self.assertEqual(db.room_dates("Нет такого"), [])

    def test_teacher_index_across_dates(self) -> None:
        first_date = DATE
        second_date = "2026-10-02"

        for day, teachers in (
            (first_date, ("Иванов И.И.",)),
            (second_date, ("Иванов И.И.", "Петров П.П.")),
        ):
            path = database_path(day)
            db.init_db(path)
            db.save_entries(
                path,
                [
                    ScheduleEntry(None, day, GROUP, index, "A", teacher, "101", False)
                    for index, teacher in enumerate(teachers, start=1)
                ],
            )

        teachers = [item["teacher"] for item in db.load_all_teachers()]
        self.assertIn("Иванов И.И.", teachers)
        self.assertIn("Петров П.П.", teachers)

        self.assertEqual(
            db.teacher_dates("Иванов И.И."),
            [first_date, second_date],
        )
        self.assertEqual(db.latest_teacher_date("Иванов И.И."), second_date)
        self.assertEqual(db.teacher_dates("Петров П.П."), [second_date])
        self.assertEqual(db.teacher_dates("Нет Н.Н."), [])

    def test_teacher_matching(self) -> None:
        path = database_path(DATE)
        db.init_db(path)
        db.save_entries(
            path,
            [
                ScheduleEntry(None, DATE, GROUP, 1, "A", "Иванов И.И. Петров П.П.", "", False),
            ],
        )

        self.assertEqual(len(db.load_teacher_schedule(DATE, "Петров П.П.")), 1)
        self.assertEqual(len(db.load_teacher_schedule(DATE, "Сидоров С.С.")), 0)

        # В индекс преподаватели из общей ячейки попадают по отдельности.
        indexed = {item["teacher"] for item in db.load_all_teachers()}
        self.assertIn("Иванов И.И.", indexed)
        self.assertIn("Петров П.П.", indexed)
        self.assertEqual(db.teacher_dates("Петров П.П."), [DATE])


class SubjectCleanTest(unittest.TestCase):
    def test_placeholders_are_removed(self) -> None:
        self.assertIsNone(clean_subject("_______"))
        self.assertIsNone(clean_subject("_______ _____"))
        self.assertEqual(clean_subject("_______ Классный час"), "Классный час")
        self.assertEqual(clean_subject("Линейка _____"), "Линейка")

    def test_interleaved_ocr_is_removed(self) -> None:
        self.assertIsNone(clean_subject("К Р л у а д с н с е н в ы"))
        self.assertIsNone(
            clean_subject("_______ К К л л а а ч с к с о н в ы а й Т ч .В ас . 216")
        )
        self.assertEqual(clean_subject("Линейка К Р л у а д с н 414"), "Линейка")

    def test_normal_subjects_are_kept(self) -> None:
        subjects = [
            "Математика",
            "1 п/гр Информатика",
            "Иностранный язык в профессиональной деятельности",
            "МДК.01.01. Разработка прораммных модулей",
            "ДЕНЬ САМОСТОЯТЕЛ ЬНОЙ РАОТЫ",
        ]
        for subject in subjects:
            with self.subTest(subject=subject):
                self.assertEqual(clean_subject(subject), subject)


class FilenameDateTest(unittest.TestCase):
    def test_two_digit_year(self) -> None:
        parsed = parse_date_from_filename("02.12.25.pdf")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2025-12-02")

    def test_four_digit_year(self) -> None:
        parsed = parse_date_from_filename("Расписание_02.12.2025.pdf")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2025-12-02")

    def test_invalid_date(self) -> None:
        self.assertIsNone(parse_date_from_filename("32.13.2025.pdf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
