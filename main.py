import telebot
import json
import requests
from datetime import datetime


bot  = telebot.TeleBot("") #тут в лапки вставити токен з BotFather
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привіт!")


@bot.message_handler(content_types=["text"])
def send_welcome2(message):
    if message.text == "Привіт":
        bot.reply_to(message, "Привіт!")
    else:
        try:
            today = datetime.today().strftime('%Y%m%d')
            bank_api = f"https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={message.text}&date={today}&json"
            r = requests.get(url = bank_api)
            data = r.json()
            value = data[0]["rate"]
            bot.reply_to(message, f"Привіт, курс {message.text} на сьогодні: {value}")
        except:
            bot.reply_to(message, "Помилка, таку валюту не знайдено")




bot.infinity_polling(none_stop=True, interval=0)
