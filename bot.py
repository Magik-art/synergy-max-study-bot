"""Учебный MAX-бот. Обычный запуск — диалог в консоли, --max — webhook."""

import argparse
from dataclasses import dataclass
from getpass import getpass
import hmac
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

import httpx
from nltk.stem.snowball import RussianStemmer


# Загружаем тексты рядом со скриптом, независимо от папки запуска.
FAQ = json.loads(Path(__file__).with_name("faq.json").read_text("utf-8"))
TEXTS = FAQ["messages"]
STEMMER = RussianStemmer()
BACK = (("Все темы", "menu"),)


def normalize(text):
    """Выравниваем регистр, символы Unicode и буквы ё/е."""
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def tokens(text):
    # Основы слов сравниваем целиком; повтор слова не увеличивает его вес.
    return frozenset(STEMMER.stem(word) for word in re.findall(r"[а-яa-z0-9]+", normalize(text)))


# Правила готовим один раз: слова превращаем в множества основ.
TOPICS = {}
for item in FAQ["topics"]:
    key = item["id"]
    if key in TOPICS or not re.fullmatch(r"[a-z_]+", key):
        raise ValueError("У темы должен быть уникальный ID из латинских букв и подчёркиваний.")
    rules = [(tokens(r["words"]), int(r["weight"]), tokens(r.get("context", ""))) for r in item["rules"]]
    if not item["text"] or not rules or any(not words for words, _, _ in rules):
        raise ValueError("Теме нужны текст ответа и непустые правила.")
    TOPICS[key] = {**item, "rules": rules}
if not 1 <= len(TOPICS) <= 8:
    raise ValueError("В меню должно быть от одной до восьми тем.")
MENU = tuple((item["title"], f"topic:{key}") for key, item in TOPICS.items())


@dataclass
class Answer:
    """Готовый ответ: текст, выбранная тема и кнопки."""
    text: str
    topic_id: str
    choices: tuple = MENU


def reply(key, topic_id="unknown", choices=MENU):
    # Служебные сообщения берём из того же JSON, что и ответы по темам.
    return Answer(TEXTS[key].format(reviewed_at=FAQ["reviewed_at"]), topic_id, choices)


def topic(key):
    return Answer(TOPICS[key]["text"], key, BACK) if key in TOPICS else reply("missing_topic")


def answer(text):
    """Выбираем ответ по команде, короткой реплике или ключевым словам."""
    if not isinstance(text, str) or not text.strip():
        return reply("empty")
    if len(text) > 4000:
        return reply("too_long")

    # Команды разбираем раньше обычных вопросов.
    text = normalize(text).strip()
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    if command in {"/start", "/help", "/menu"}:
        return reply("menu", "menu")
    if command == "/about":
        return reply("about", "about", BACK)
    phrase = " ".join(re.findall(r"[а-яa-z0-9]+", text))
    if phrase in {"привет", "здравствуйте", "добрый день", "добрый вечер", "меню", "помощь", "начать"}:
        return reply("menu", "menu")
    if phrase in {"спасибо", "благодарю", "большое спасибо"}:
        return reply("thanks", "thanks")
    if text.startswith("/"):
        return reply("unknown_command")
    if phrase in {"синергия", "университет", "университет синергия"}:
        return topic("university")

    # Для темы берём наибольший вес: нужны все слова правила и подходящий контекст.
    words = tokens(text)
    scores = {}
    for key, item in TOPICS.items():
        weights = [weight for required, weight, context in item["rules"]
                   if required <= words and (not context or context & words)]
        scores[key] = max(weights, default=0)
    best = max(scores.values())
    if best < 2:
        return reply("unknown")
    winners = [key for key, score in scores.items() if score == best]
    if len(winners) > 1:
        buttons = tuple((TOPICS[key]["title"], f"topic:{key}") for key in winners)
        return reply("ambiguous", "ambiguous", buttons)
    return topic(winners[0])


def valid_id(value):
    # True и False не должны попадать в адреса сообщений.
    return type(value) is int and value != 0


def human(user):
    return isinstance(user, dict) and valid_id(user.get("user_id")) and not user.get("is_bot", False)


def dialog(message):
    recipient = message.get("recipient") if isinstance(message, dict) else None
    return recipient if isinstance(recipient, dict) and recipient.get("chat_type") == "dialog" else None


def handle(update, client):
    """Превращаем событие MAX в ответ. Только личные диалоги с человеком."""
    if not isinstance(update, dict):
        return "ignored"
    kind = update.get("update_type")
    if kind == "bot_started":
        if not valid_id(update.get("chat_id")) or not human(update.get("user")):
            return "ignored"
        result, target = reply("menu", "menu"), {"chat_id": update["chat_id"]}
    elif kind == "message_created":
        message = update.get("message")
        recipient = dialog(message)
        if recipient is None or not human(message.get("sender")):
            return "ignored"
        # recipient.user_id — сам бот; при отсутствии chat_id отвечаем отправителю.
        chat_id = recipient.get("chat_id")
        target = {"chat_id": chat_id} if valid_id(chat_id) else {"user_id": message["sender"]["user_id"]}
        body = message.get("body")
        text = body.get("text") if isinstance(body, dict) else None
        result = answer(text) if isinstance(text, str) and text.strip() else reply("attachment")
    elif kind == "message_callback":
        callback = update.get("callback")
        if not isinstance(callback, dict) or not human(callback.get("user")) or dialog(update.get("message")) is None:
            return "ignored"
        callback_id = callback.get("callback_id")
        if not isinstance(callback_id, str) or not callback_id.strip():
            return "ignored"
        payload = callback.get("payload")
        result = topic(payload[6:]) if isinstance(payload, str) and payload.startswith("topic:") and len(payload) <= 128 else reply("menu", "menu")
        post(client, "/answers", {"callback_id": callback_id}, {"message": message_body(result)})
        return "answered"
    else:
        return "ignored"
    post(client, "/messages", target, message_body(result))
    return "sent"


def message_body(result):
    # Кнопки располагаем по две в строке; payload задаёт действие кнопки.
    buttons = [{"type": "callback", "text": label, "payload": value} for label, value in result.choices]
    keyboard = {"type": "inline_keyboard", "payload": {"buttons": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}}
    return {"text": result.text, "attachments": [keyboard] if buttons else []}


def post(client, path, params, body):
    # Ошибка отправки дойдёт до webhook; содержимое запросов в журнал не пишем.
    response = client.post(path, params={**params, "disable_link_preview": "true"}, json=body)
    if not response.is_success:
        raise ValueError("MAX отклонил запрос.")
    data = response.json()
    if not isinstance(data, dict) or data.get("success") is False:
        raise ValueError("MAX не подтвердил отправку.")


def create_app(token, secret, transport=None):
    """Учебный webhook: проверяем секрет, принимаем JSON, сразу отвечаем."""
    from fastapi import FastAPI, HTTPException, Request
    from starlette.concurrency import run_in_threadpool

    if not token or token in {"XXX", "ХХХ"} or any(c.isspace() for c in token):
        raise ValueError("Нужен действующий токен MAX.")
    if not re.fullmatch(r"[A-Za-z0-9-]{24,256}", secret) or secret.startswith("REPLACE-"):
        raise ValueError("Нужен секрет webhook: 24–256 латинских букв, цифр или дефисов.")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health():
        # Локальная проверка сервиса без токена и обращения к MAX.
        return {"status": "ok", "version": "educational", "topics": len(TOPICS)}

    def deliver(update):
        # Адрес API фиксирован; TLS проверяется, токен передаётся заголовком.
        with httpx.Client(base_url="https://platform-api.max.ru", headers={"Authorization": token},
                          timeout=20, trust_env=False, follow_redirects=False, transport=transport) as client:
            return handle(update, client)

    @app.post("/study-bot/max/webhook")
    async def webhook(request: Request):
        provided = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(provided.encode(), secret.encode()):
            raise HTTPException(403, "Неверный секрет.")
        if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
            raise HTTPException(415, "Ожидается JSON.")
        # Ограничиваем запрос до 64 КиБ, в том числе при передаче частями.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "Запрос слишком большой.")
        try:
            update = json.loads(raw)
            if not isinstance(update, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Некорректное событие.") from None
        try:
            return {"ok": True, "result": await run_in_threadpool(deliver, update)}
        except (httpx.RequestError, ValueError):
            raise HTTPException(503, "Не удалось отправить ответ.") from None

    return app


def main():
    parser = argparse.ArgumentParser(description="Учебный помощник в MAX")
    parser.add_argument("--max", action="store_true", help="Запустить webhook вместо консоли")
    args = parser.parse_args()
    if args.max:
        # Сервер берёт секреты из окружения; локально можно ввести их скрыто.
        import uvicorn
        try:
            token, secret = os.getenv("MAX_BOT_TOKEN"), os.getenv("MAX_WEBHOOK_SECRET")
            if (not token or not secret) and not sys.stdin.isatty():
                raise ValueError("Задайте MAX_BOT_TOKEN и MAX_WEBHOOK_SECRET в окружении сервера.")
            app = create_app(token or getpass("Токен MAX: ").strip(),
                             secret or getpass("Секрет webhook: ").strip())
            port = int(os.getenv("LISTEN_PORT", "8098"))
            if not 1 <= port <= 65535:
                raise ValueError("LISTEN_PORT должен быть от 1 до 65535.")
        except ValueError:
            parser.error("Не удалось запустить сервер: проверьте токен, секрет и LISTEN_PORT.")
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False,
                    proxy_headers=False, server_header=False)
        return
    # Обычный запуск в PyCharm работает без токена и запросов к MAX.
    print(reply("menu", "menu").text)
    print("\nЛокальное демо. /exit — выход.\n")
    while True:
        try:
            question = input("Ты: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question in {"/exit", "/quit"}:
            break
        print("\nБот:", answer(question).text, "\n")


if __name__ == "__main__":
    main()
