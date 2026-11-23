import os

import telebot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

bot = telebot.TeleBot(TOKEN) if TOKEN else None


def send_message(text: str):
    # Отправка намеренно отключена; включайте только вместе с настроенным токеном.
    # if bot and CHAT_ID:
    #     bot.send_message(CHAT_ID, text)
    pass
