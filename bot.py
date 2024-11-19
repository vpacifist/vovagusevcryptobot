from telegram import Bot
from telegram.ext import CommandHandler, Updater
from dotenv import load_dotenv
import os

# Загрузка переменных из .env файла
load_dotenv()

# Получение токена из переменной окружения
TOKEN = os.getenv("TOKEN")

bot = Bot(token=TOKEN)

def start(update, context):
    update.message.reply_text("Привет! Я твой бот.")

updater = Updater(token=TOKEN, use_context=True)
dispatcher = updater.dispatcher

# Добавляем обработчик команды /start
dispatcher.add_handler(CommandHandler("start", start))

updater.start_polling()
updater.idle()