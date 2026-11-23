import re
from datetime import date

# dd.mm.yy или dd.mm.yyyy в любом месте имени файла.
DATE_RE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{2}|\d{4})(?!\d)")


def parse_date_from_filename(name: str) -> date | None:
    """Извлекает дату из имени файла.

    Поддерживает:
      * 02.12.25.pdf
      * 02.12.2025.pdf
      * Расписание_02.12.2025.pdf
    """
    match = DATE_RE.search(name)
    if not match:
        return None

    day, month, year = match.groups()
    if len(year) == 2:
        year = "20" + year

    try:
        return date(year=int(year), month=int(month), day=int(day))
    except ValueError:
        return None
