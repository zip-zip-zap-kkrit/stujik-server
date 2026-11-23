import logging
import requests
from datetime import date
from typing import List, NamedTuple
import os
from config import PDF_DIR

from utils import parse_date_from_filename

logger = logging.getLogger("downloader")

YANDEX_PUBLIC_API = "https://cloud-api.yandex.net/v1/disk/public/resources"


class PdfItem(NamedTuple):
    name: str
    date: date
    download_url: str

def list_pdfs(public_key: str, path: str) -> List[PdfItem]:
    logger.info("Получение списка PDF с Яндекс.Диска")
    # logger.info("Путь внутри ресурса: %s", path)

    params = {
        "public_key": public_key,
        "path": path,
        "limit": 1000
    }

    r = requests.get(YANDEX_PUBLIC_API, params=params, timeout=30)
    r.raise_for_status()

    data = r.json()

    # 🔴 ВАЖНО ДЛЯ ОТЛАДКИ
    logger.debug("RAW RESPONSE KEYS: %s", data.keys())

    embedded = data.get("_embedded")
    if not embedded:
        logger.warning("Нет _embedded в ответе")
        return []

    items = embedded.get("items", [])
    logger.info("Найдено элементов: %d", len(items))

    result: List[PdfItem] = []

    for item in items:
        logger.debug("ITEM RAW: %s", item)

        if item.get("type") != "file":
            continue

        name = item.get("name", "")
        # logger.info("ITEM: name=%s", name)

        if not name.lower().endswith(".pdf"):
            continue

        file_date = parse_date_from_filename(name)
        if not file_date:
            logger.warning("Не удалось извлечь дату из имени: %s", name)
            continue

        download_url = item.get("file")
        if not download_url:
            logger.warning("Нет прямой ссылки на файл: %s", name)
            continue

        result.append(
            PdfItem(
                name=name,
                date=file_date,
                download_url=download_url
            )
        )

        logger.info("✔ PDF принят: %s (%s)", name, file_date)

    return sorted(result, key=lambda x: x.date)

def download_pdf(download_url, name):
    os.makedirs(PDF_DIR, exist_ok=True)
    local_path = os.path.join(PDF_DIR, name)
    try:
        logger.info("Скачивание %s → %s", name, local_path)
        with requests.get(download_url, stream=True) as r_file:
            r_file.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r_file.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info("✔ Скачано: %s", name)
    except Exception as e:
        logger.error("Ошибка при скачивании %s: %s", name, e)
