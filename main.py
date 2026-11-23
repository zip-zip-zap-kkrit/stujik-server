import logging
from datetime import date
import time
import json
import os

from downloader import list_pdfs, download_pdf
from db import database_path, init_db, save_entries
from telenotifications import send_message
from pdfparser import parse_pdf
from server import send_all

from config import (
    BASE_DIR,
    YANDEX_PUBLIC_URL,
    PDF_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s"
)

INDEX_FILE = os.path.join(BASE_DIR, "processed_files.json")

def load_index():
    if not os.path.exists(INDEX_FILE):
        return set()

    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logging.error("Не удалось прочитать %s: %s", INDEX_FILE, exc)
        return set()

    return set(data) if isinstance(data, list) else set()

def save_index(index):
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(
            sorted(index),
            f,
            ensure_ascii=False,
            indent=2
        )

def main():
    print("\r------------------------------------------------------------")
    pdfs = list_pdfs(
        public_key=YANDEX_PUBLIC_URL,
        path="/КРАСНОЯРСКИЙ РАБОЧИЙ 2026-2027"
    )

    processed = load_index()

    for pdf in pdfs:
        pdf_key = pdf.date.isoformat()

        if pdf_key in processed:
            logging.info("✅%s уже обработан", pdf_key)
            continue

        download_pdf(pdf.download_url, pdf.name)
        logging.info(f"Обрабатываю %s", pdf_key)
        send_message(f"Обрабатываю {pdf_key}")

        pdf_path = f"{PDF_DIR}/{pdf.name}"

        entries = parse_pdf(pdf_path, pdf.date)
        if not entries:
            logging.error("Не удалось разобрать расписание за %s", pdf_key)
            send_message(f"Не удалось разобрать расписание за {pdf_key}")
            continue

        db_path = database_path(pdf_key)
        init_db(db_path)
        save_entries(db_path, entries)

        processed.add(pdf_key)
        save_index(processed)

        send_message(f"Отработал {pdf_key} ({len(entries)} записей)")
        send_all(title="Добавлено новое расписание!", body=f"на {pdf_key}")


if __name__ == "__main__":
    today = date.today()
    print(today)
    send_message(f"Начал работу \n\n{today}")
    # send_all(title="server", body="начал работу!")
    # os.system("clear")
    while True:
        try:
            main()
        except Exception as e:
            send_message(f"Произошла ошибка: {e}")
            logging.error("Произошла ошибка: %s", e)

        for i in range(300):
            emoji = ["🕛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚"]
            print(f"\r{emoji[i%len(emoji)]} Жду {300-i} {"-"*(int(i/20)+1)}", end="")
            time.sleep(1)
