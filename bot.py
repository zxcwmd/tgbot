import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    pass


def api(method, **params):
    url = f'https://api.telegram.org/bot{os.environ["BOT_TOKEN"]}/{method}'
    request = Request(url, json.dumps(params).encode(), {'Content-Type': 'application/json'})
    try:
        response = urlopen(request, timeout=40)
    except HTTPError as error:
        response = error
    with response:
        data = json.load(response)
    if not data['ok']:
        raise TelegramError(f'{method}: Telegram error {data["error_code"]}')
    return data['result']


def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY, file TEXT NOT NULL, name TEXT NOT NULL,
            price INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            source INTEGER UNIQUE
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, buyer INTEGER NOT NULL, file TEXT NOT NULL,
            price INTEGER NOT NULL, charge TEXT UNIQUE, delivered INTEGER NOT NULL DEFAULT 0
        );
    ''')
    return db


def matches(order, buyer, payment):
    return (order is not None and order['buyer'] == buyer
            and payment['currency'] == 'XTR' and order['price'] == payment['total_amount'])


def handle(db, admin, update):
    if q := update.get('pre_checkout_query'):
        order = db.execute('SELECT * FROM orders WHERE id=?', (q['invoice_payload'],)).fetchone()
        ok = matches(order, q['from']['id'], q) and order['charge'] is None
        api('answerPreCheckoutQuery', pre_checkout_query_id=q['id'], ok=ok,
            **({} if ok else {'error_message': 'Счёт недействителен. Создайте новый через /catalog.'}))
        return
    msg = update.get('message', {})
    if msg.get('chat', {}).get('type') != 'private':
        return
    chat = msg['chat']['id']
    buyer = msg['from']['id']

    def send(text, **options):
        api('sendMessage', chat_id=chat, text=text, **options)

    if payment := msg.get('successful_payment'):
        order = db.execute('SELECT * FROM orders WHERE id=?', (payment['invoice_payload'],)).fetchone()
        charge = payment['telegram_payment_charge_id']
        if matches(order, buyer, payment) and order['charge'] in (None, charge):
            with db:
                db.execute('UPDATE orders SET charge=? WHERE id=?', (charge, order['id']))
        else:
            logging.error('Не удалось сопоставить оплату со счётом: %s', charge)
            send('Не удалось обработать оплату. Напишите в /paysupport.')
        return

    text = msg.get('text', '').split()
    command = text[0].split('@')[0] if text else ''
    if command in ('/start', '/catalog'):
        products = db.execute('SELECT * FROM products WHERE active=1 ORDER BY id').fetchall()
        send('Каталог. Для покупки отправьте команду рядом с файлом:' if products else 'Каталог пока пуст.')
        for product in products:
            send(f'{product["name"]} — {product["price"]} ⭐\n/buy_{product["id"]}')
        if buyer == admin:
            send('Для добавления отправьте документ с подписью «цена название». Снять с продажи: /hide ID.')
    elif command.startswith('/buy_') and command[5:].isascii() and command[5:].isdigit() and len(command) <= 23:
        product = db.execute('SELECT * FROM products WHERE id=? AND active=1', (command[5:],)).fetchone()
        if not product:
            send('Товар не найден. Откройте /catalog.')
            return
        payload = secrets.token_urlsafe(24)
        with db:
            db.execute('INSERT INTO orders(id,buyer,file,price) VALUES (?,?,?,?)',
                       (payload, buyer, product['file'], product['price']))
        api('sendInvoice', chat_id=chat, title=product['name'], description='Цифровой файл',
            payload=payload, currency='XTR', provider_token='', start_parameter='catalog',
            prices=[{'label': product['name'], 'amount': product['price']}])
    elif command in ('/paysupport', '/support'):
        send(f'Поддержка по покупкам: {os.environ["SUPPORT_CONTACT"]}. Укажите товар и время оплаты.')
    elif command == '/terms':
        send('Вы покупаете цифровой файл за Telegram Stars. После оплаты бот отправит его в этот чат. Повторной выдачи нет: сохраните сообщение или скачайте файл. По вопросам оплаты и возврата: /paysupport.')
    elif buyer == admin and (doc := msg.get('document')):
        parts = msg.get('caption', '').split(maxsplit=1)
        if (len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit()
                or len(parts[0]) > 5 or not 1 <= int(parts[0]) <= 10000 or len(parts[1]) > 32):
            send('Подпись: цена (1–10000) и название (до 32 символов). Например: 50 Мой файл')
            return
        with db:
            db.execute('INSERT OR IGNORE INTO products(file,name,price,source) VALUES (?,?,?,?)',
                       (doc['file_id'], parts[1], int(parts[0]), msg['message_id']))
        product = db.execute('SELECT id FROM products WHERE source=?', (msg['message_id'],)).fetchone()
        send(f'Товар №{product["id"]} добавлен. Проверить: /catalog.')
    elif buyer == admin and command == '/hide':
        if len(text) != 2 or not text[1].isascii() or not text[1].isdigit():
            send('Формат: /hide ID')
            return
        with db:
            result = db.execute('UPDATE products SET active=0 WHERE id=? AND active=1', (text[1],))
        send('Товар скрыт.' if result.rowcount else 'Активный товар с таким ID не найден.')


def deliver(db):
    for order in db.execute('SELECT * FROM orders WHERE charge IS NOT NULL AND delivered=0 LIMIT 20').fetchall():
        try:
            api('sendDocument', chat_id=order['buyer'], document=order['file'], protect_content=True)
        except (TelegramError, OSError, ValueError):
            logging.warning('Не удалось отправить заказ %s; будет повторная попытка', order['id'])
            continue
        with db:
            db.execute('UPDATE orders SET delivered=1 WHERE id=?', (order['id'],))


def configure_polling(remove_webhook=False):
    """Switch transports only when explicitly authorized, preserving queued updates."""
    if not api('getWebhookInfo').get('url'):
        return
    if not remove_webhook:
        raise SystemExit('У бота активен webhook. Для переноса задайте SWITCH_TO_POLLING=1 и перезапустите.')
    if not api('deleteWebhook', drop_pending_updates=False):
        raise TelegramError('Telegram не подтвердил отключение webhook')
    logging.info('Webhook отключён; ожидающие обновления сохранены.')


def main():
    logging.basicConfig(level=logging.INFO)
    for key in ('BOT_TOKEN', 'ADMIN_ID', 'SUPPORT_CONTACT'):
        if not os.environ.get(key):
            raise SystemExit(f'Укажите {key}')
    admin = int(os.environ['ADMIN_ID'])
    db = open_db(os.environ.get('DB_PATH', 'data/shop.sqlite3'))
    while True:
        try:
            configure_polling(os.environ.get('SWITCH_TO_POLLING') == '1')
            break
        except TelegramError as error:
            if str(error).endswith((' 401', ' 404')):
                raise SystemExit('Telegram не принял BOT_TOKEN. Проверьте токен.') from None
            logging.warning('Не удалось проверить webhook; повтор через 5 секунд')
        except (OSError, ValueError):
            logging.warning('Telegram недоступен; повтор через 5 секунд')
        time.sleep(5)
    logging.info('Бот запущен в режиме long polling.')
    offset = 0
    while True:
        try:
            updates = api('getUpdates', offset=offset, timeout=15,
                          allowed_updates=['message', 'pre_checkout_query'])
            for update in updates:
                try:
                    handle(db, admin, update)
                except TelegramError as error:
                    if not str(error).endswith((' 400', ' 403')):
                        raise
                    logging.warning('%s (update %s)', error, update['update_id'])
                offset = update['update_id'] + 1
            deliver(db)
        except TelegramError as error:
            if str(error) == 'getUpdates: Telegram error 409':
                raise SystemExit('Конфликт Telegram: проверьте webhook и остановите другие экземпляры бота.') from None
            logging.warning('%s; повтор через 5 секунд', error)
            time.sleep(5)
        except (OSError, ValueError):
            logging.warning('Сбой связи с Telegram; повтор через 5 секунд')
            time.sleep(5)


if __name__ == '__main__':
    main()
