"""Register the Telegram webhook without putting credentials in shell history."""
import os
from getpass import getpass
from urllib.parse import urlsplit

from bot import api

if __name__ == '__main__':
    url = input('Webhook URL (https://…workers.dev/telegram): ').strip()
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path != '/telegram':
        raise SystemExit('Укажите HTTPS-адрес Worker с путём /telegram')
    os.environ['BOT_TOKEN'] = getpass('BOT_TOKEN: ').strip()
    secret = getpass('WEBHOOK_SECRET: ').strip()
    try:
        ok = api('setWebhook', url=url, secret_token=secret,
                 allowed_updates=['message', 'pre_checkout_query'])
    except Exception:
        raise SystemExit('Не удалось установить webhook. Проверьте токен, секрет и адрес Worker.') from None
    if not ok:
        raise SystemExit('Telegram не подтвердил установку webhook.')
    print('Webhook установлен.')
