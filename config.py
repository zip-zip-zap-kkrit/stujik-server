import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

YANDEX_PUBLIC_URL_OLD = "https://disk.yandex.ru/d/0vmvyzN6Yfd43A"
YANDEX_PUBLIC_URL = "https://disk.yandex.ru/d/hJzFXhOcZv9buQ"
PDF_DIR = str(BASE_DIR / "data" / "pdfs")

# Секреты не храним в исходниках. Значения задаются через окружение.
CLOUDCONVERT_API_KEY = os.getenv("CLOUDCONVERT_API_KEY", "")
YASECRET = os.getenv("YANDEX_SECRET", "")
YATOKEN = os.getenv("YANDEX_TOKEN", "")
