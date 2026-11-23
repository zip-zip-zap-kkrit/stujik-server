import re
from datetime import date as Date
from typing import List, Tuple

import pdfplumber

from db import (
    ScheduleEntry,
    clean_subject,
    is_plausible_room,
    normalize_group,
    normalize_room,
)

settings = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 5,
    "text_x_tolerance": 3,
    "text_y_tolerance": 3,
    "edge_min_length": 3,
}


def parse_pdf(pdf_path, pdf_date: str | Date) -> List[ScheduleEntry]:
    """Разбирает PDF с расписанием и возвращает список занятий."""
    day = pdf_date.isoformat() if isinstance(pdf_date, Date) else str(pdf_date)

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        tables = page.extract_tables(table_settings=settings)

        entries: List[ScheduleEntry] = []

        for table_index, table in enumerate(tables):
            if not table:
                continue

            header_index = 2 if table_index == 0 else 0
            if header_index >= len(table):
                continue

            header = table[header_index]
            groups: list[str] = []
            group_columns: list[int] = []

            # Группы начинаются с колонки 2 и идут через одну.
            for column in range(2, len(header), 2):
                group_name = normalize_group(header[column])
                if group_name:
                    groups.append(group_name)
                    group_columns.append(column)

            for row in table[header_index + 1:]:
                if not row or not row[0]:
                    continue

                try:
                    lesson_number = int(row[0])
                except (TypeError, ValueError):
                    continue

                for group, column in zip(groups, group_columns):
                    if column >= len(row):
                        continue

                    subject_cell = row[column]
                    room_cell = row[column + 1] if column + 1 < len(row) else None

                    if not subject_cell:
                        continue

                    subject, teacher = parse_subject_and_teacher(subject_cell)
                    subject = clean_subject(subject)
                    if subject is None:
                        continue

                    is_distance = False
                    room = ""

                    if room_cell:
                        value = room_cell.strip()
                        if value.upper() == "ДО":
                            is_distance = True
                        else:
                            room = normalize_room(value)
                            if not is_plausible_room(room):
                                room = ""

                    entries.append(
                        ScheduleEntry(
                            id=None,
                            date=day,
                            group=group,
                            lessonNumber=lesson_number,
                            subject=subject,
                            teacher=teacher,
                            room=room,
                            DO=is_distance,
                        )
                    )

        return entries


teacher_regex = re.compile(r"[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ]\.[А-ЯЁ]\.?")
_space_re = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _space_re.sub(" ", text.replace("\n", " ")).strip()


def parse_subject_and_teacher(text: str) -> Tuple[str, str]:
    """Возвращает пару (предмет, преподаватель)."""
    if not text:
        return "", ""

    text = normalize_text(text)
    matches = list(teacher_regex.finditer(text))

    if matches:
        teacher = " ".join(match.group(0) for match in matches)
        subject = text[:matches[0].start()].strip()
    else:
        subject = text
        teacher = ""

    return subject, teacher
