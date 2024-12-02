from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import PeerIdInvalidError
from telethon.tl.types import PeerChannel
from openai import OpenAI
from datetime import datetime
import pytz
import json
from dotenv import load_dotenv
import os
import aio_pika
from aio_pika import connect
import ssl

load_dotenv()
toronto_tz = pytz.timezone("America/Toronto")

# Replace these with your API credentials
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Replace these with the rabbitmq information
RABBITMQ_HOST=os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT=int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USER=os.getenv("RABBITMQ_USER")
RABBITMQ_PASSWORD=os.getenv("RABBITMQ_PASSWORD")
SIGNAL_MQ_NAME=os.getenv("SIGNAL_MQ_NAME")
RABBITMQ_URL= f"amqps://{RABBITMQ_USER}:{RABBITMQ_PASSWORD}@{RABBITMQ_HOST}/"

# Replace with your OpenAI API key
MODEL=os.getenv("MODEL")
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY")
openaiClient = OpenAI(api_key=OPENAI_API_KEY)

# Replace with Telegram source group, target group to send and from specific user id
# SOURCE_GROUP = int(os.getenv("SOURCE_GROUP"))
TARGET_USER = int(os.getenv("TARGET_USER"))
SPECIFIC_USERS = os.getenv("SPECIFIC_USER")

specific_users_array = json.loads(SPECIFIC_USERS) if SPECIFIC_USERS else []

# Initialize the Telegram client
phone_client = TelegramClient('phone_session', API_ID, API_HASH)
bot_client = TelegramClient('bot_session', API_ID, API_HASH)
mq_signal_channel = None

@phone_client.on(events.NewMessage)
async def handler(event):
    sender = await event.get_sender()
    print(f"Message: \n from: {sender.username} \n content: {event.message.text} \n in: {event.chat_id}")

    if event.sender and event.sender.id in specific_users_array:
        response = prompt_openai("The following text after :: as the input contains a message from a telegram channel, analyze it and if " 
            +" the message contains a signal for trading that includes direction and the time is given in AST (Arabia Standard Time) with (hh:mm) format in the input."
            +" AST is (GMT+3). EST is (GMT-5). IRST is (GMT+3:30). "
            +" If the message contains a signal then convert it to JSON with the following conditions and field:" 
            +" Toronto_time: input time converted to EST (Estern Time Zone) for Toronto in (hh:mm) format, Tehran_time: input time converted to IRST (Iran Standard Time) for Tehran, "
            + ", type (BTC/USDT), direction (UP or DOWN), stage which is value [1-6] given in the input, account_portion is a float value representing the percent of the account stated in the input (1% means 0.01) " 
            +", time type is a integer value given in the input. If it doesn't contain information about signal only return 'NONE'. Only return 'NONE' or json values extracted from input. " 
            +" The Json contains these fields (Toronto_time, Tehran_time, type, direction, time_type, stage, account_portion). "
            +"Don't include extra words in your json string, it should be convertable in python to dictionary. :: " + event.message.text)
        if(response != "NONE"):
            response = clean_convert(response)
            response = add_date(response)
            response_str = json.dumps(response)
            await publish_message(SIGNAL_MQ_NAME, response_str)
            response = prompt_openai("Convert this json to a well written small text as a signal that can be forwarded to a telegram channel "
                                     +" for trading use symbols that can be consumes easy, be consistent in your format :" + response_str)
            await bot_client.send_message(TARGET_USER, "" + response + "")
            print(response)


def add_date(data):
    today = datetime.now(toronto_tz)
    formatted_date = today.strftime("%Y-%m-%d")
    time = data["Toronto_time"]
    date_time_str = f"{formatted_date} {time}"
    localized_date_time = toronto_tz.localize(datetime.strptime(date_time_str, "%Y-%m-%d %H:%M"))
    epoch_time = int(localized_date_time.timestamp()) * 1000
    data["toronto_date_time"] = date_time_str
    data["epoc"] = epoch_time
    return data

def prompt_openai(prompt_text):
    try:
        completion = openaiClient.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt_text},
            ]
        )
        response = completion.choices[0].message.content
        return response
    except Exception as e:  # Handle any API or runtime errors
        return f"An error occurred: {e}"

def clean_convert(chatgpt_string):
    cleaned_string = chatgpt_string.replace("```", "").replace("json", "").strip()
    print(cleaned_string)
    try:
        data = json.loads(cleaned_string)
        return data
    except json.JSONDecodeError as e:
        print("Error decoding JSON:", e)


async def setup_rabbitmq():
    global mq_signal_channel
    try:
        connection = await connect(
            RABBITMQ_URL,
            heartbeat=30,  # Send heartbeats every 30 seconds
            timeout=60     # Timeout for establishing the connection
        )
        mq_signal_channel = await connection.channel()
        print("Connected to RabbitMQ!")
    except Exception as e:
        print(f"RabbitMQ connection failed: {e}")

async def publish_message(queue_name, message):
    global mq_signal_channel
    try:
        if mq_signal_channel is None or mq_signal_channel.is_closed:
            print("RabbitMQ channel is closed. Reconnecting...")
            await setup_rabbitmq()
        # Ensure the queue exists
        await mq_signal_channel.declare_queue(queue_name)
        # Publish the message
        await mq_signal_channel.default_exchange.publish(
            aio_pika.Message(body=message.encode()),
            routing_key=queue_name,
        )
        print(f"Message published to queue '{queue_name}': {message}")
    except Exception as e:
        print(f"Failed to publish message: {e}")


async def main():
    await setup_rabbitmq()
    await phone_client.start(phone=PHONE)
    await bot_client.start(bot_token=BOT_TOKEN)
    me = await phone_client.get_me()
    me = await bot_client.get_me()
    print(f"Logged in as {me.username}")
    print("Bot is running...")
    await phone_client.run_until_disconnected()
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    with phone_client:
        phone_client.loop.run_until_complete(main())
    with bot_client:
        bot_client.loop.run_until_complete(main())
