async function telegram(env, method, params) {
  let response;
  try {
    response = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(params), signal: AbortSignal.timeout(6500),
    });
  } catch {
    throw new Error(`${method}: network error`);
  }
  const result = await response.json();
  if (!result.ok) {
    const error = new Error(`${method}: Telegram ${result.error_code}`);
    error.code = result.error_code;
    throw error;
  }
  return result.result;
}

const sql = (env, query, ...args) => env.DB.prepare(query).bind(...args);
const orderFor = (env, payment) => sql(env, 'SELECT * FROM orders WHERE id=?', payment.invoice_payload).first();
const matches = (order, buyer, payment) => order && order.buyer === buyer &&
  payment.currency === 'XTR' && order.price === payment.total_amount;

export async function deliver(env, id) {
  const now = Date.now();
  // An atomic lease prevents concurrent webhook and cron attempts for the same order.
  const order = await sql(env, `UPDATE orders SET retry_at=? WHERE id=? AND charge IS NOT NULL
    AND delivered=0 AND retry_at<=? RETURNING buyer,file`, now + 300000, id, now).first();
  if (!order) return;
  try {
    await telegram(env, 'sendDocument', { chat_id: order.buyer, document: order.file, protect_content: true });
  } catch (error) {
    console.warn('Delivery delayed', id, error.message);
    return;
  }
  await sql(env, 'UPDATE orders SET delivered=1 WHERE id=?', id).run();
}

export async function handle(env, update) {
  const q = update.pre_checkout_query;
  if (q) {
    const order = await orderFor(env, q);
    const ok = Boolean(matches(order, q.from.id, q) && !order.charge);
    try {
      await telegram(env, 'answerPreCheckoutQuery', { pre_checkout_query_id: q.id, ok,
        ...(ok ? {} : { error_message: 'Счёт недействителен. Создайте новый через /catalog.' }) });
    } catch (error) {
      // Telegram doesn't accept answers to expired or already answered queries.
      if (error.code !== 400) throw error;
    }
    return;
  }
  const msg = update.message;
  if (msg?.chat?.type !== 'private' || !msg.from) return;
  const buyer = msg.from.id;
  const admin = String(buyer) === env.ADMIN_ID;
  const send = (text) => telegram(env, 'sendMessage', { chat_id: msg.chat.id, text });
  const payment = msg.successful_payment;
  if (payment) {
    const order = await orderFor(env, payment);
    const charge = payment.telegram_payment_charge_id;
    if (!matches(order, buyer, payment) || (order.charge && order.charge !== charge)) {
      console.error('Unmatched payment', charge);
      await send('Не удалось обработать оплату. Обратитесь в /paysupport.');
      return;
    }
    const recorded = await sql(env, `UPDATE orders SET charge=? WHERE id=? AND (charge IS NULL OR charge=?)
      RETURNING id`, charge, order.id, charge).first();
    if (!recorded) {
      console.error('Conflicting payment', charge);
      await send('Не удалось обработать оплату. Обратитесь в /paysupport.');
      return;
    }
    await deliver(env, order.id);
    return;
  }
  const [raw = '', arg, ...extra] = (msg.text ?? '').trim().split(/\s+/);
  const command = raw.split('@')[0];
  if (command === '/start' || command === '/catalog') {
    const page = /^[1-9][0-9]{0,5}$/.test(arg ?? '') ? Number(arg) : 1;
    const { results } = await sql(env, 'SELECT id,name,price FROM products WHERE active=1 ORDER BY id LIMIT 21 OFFSET ?', (page - 1) * 20).all();
    const lines = results.slice(0, 20).map(p => `${p.name} — ${p.price} ⭐ /buy_${p.id}`);
    if (page > 1) lines.push(`Назад: отправьте /catalog ${page - 1}`);
    if (results.length > 20) lines.push(`Далее: отправьте /catalog ${page + 1}`);
    if (admin) lines.push('Добавить: отправьте документ с подписью «цена название». Скрыть: /hide ID.');
    await send(lines.length ? `Каталог\n\n${lines.join('\n')}` : 'Каталог пока пуст.');
  } else if (/^\/buy_[0-9]{1,16}$/.test(command)) {
    const product = await sql(env, 'SELECT * FROM products WHERE id=? AND active=1', command.slice(5)).first();
    if (!product) return send('Товар не найден. Откройте /catalog.');
    const id = `order-${update.update_id}`;
    const inserted = await sql(env, `INSERT OR IGNORE INTO orders(id,buyer,file,price) VALUES (?,?,?,?)`,
      id, buyer, product.file, product.price).run();
    if (!inserted.meta.changes) return;
    try {
      await telegram(env, 'sendInvoice', { chat_id: msg.chat.id, title: product.name, description: 'Цифровой файл',
        payload: id, currency: 'XTR', provider_token: '', start_parameter: 'catalog',
        prices: [{ label: product.name, amount: product.price }] });
    } catch (error) {
      // Keep the order valid in case Telegram accepted the invoice before the connection failed.
      console.warn('Invoice response unavailable', id, error.message);
      await send('Не удалось подтвердить отправку счёта. Если счёт не появился, попробуйте снова через /catalog.');
    }
  } else if (command === '/paysupport' || command === '/support') {
    await send(`Поддержка по покупкам: ${env.SUPPORT_CONTACT}. Укажите товар и время оплаты.`);
  } else if (command === '/terms') {
    await send('Вы покупаете цифровой файл за Telegram Stars. Бот отправит его после оплаты. Повторной выдачи по запросу нет. Вопросы по оплате и возврату: /paysupport.');
  } else if (admin && msg.document) {
    const match = (msg.caption ?? '').trim().match(/^(\d{1,5})\s+(.+)$/su);
    if (!match || Number(match[1]) < 1 || Number(match[1]) > 10000 || [...match[2]].length > 32) {
      return send('Отправьте документ с подписью «цена название». Например: 50 Мой файл (название до 32 символов).');
    }
    await sql(env, 'INSERT OR IGNORE INTO products(file,name,price,source) VALUES (?,?,?,?)',
      msg.document.file_id, match[2], Number(match[1]), msg.message_id).run();
    const product = await sql(env, 'SELECT id FROM products WHERE source=?', msg.message_id).first();
    await send(`Товар №${product.id} добавлен. Проверить: /catalog.`);
  } else if (admin && command === '/hide') {
    if (!/^[0-9]{1,16}$/.test(arg ?? '') || extra.length) return send('Формат: /hide ID');
    const result = await sql(env, 'UPDATE products SET active=0 WHERE id=? AND active=1', arg).run();
    await send(result.meta.changes ? 'Товар скрыт.' : 'Активный товар не найден.');
  }
}

export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname !== '/telegram') return new Response('Not found', { status: 404 });
    if (request.method !== 'POST') return new Response('Method not allowed', { status: 405 });
    if (!env.WEBHOOK_SECRET || request.headers.get('X-Telegram-Bot-Api-Secret-Token') !== env.WEBHOOK_SECRET) {
      return new Response('Forbidden', { status: 403 });
    }
    if (!env.BOT_TOKEN || !env.ADMIN_ID || !env.SUPPORT_CONTACT || !env.DB) {
      return new Response('Missing configuration', { status: 503 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response('Invalid JSON', { status: 400 }); }
    try {
      await handle(env, update);
      return new Response('OK');
    } catch (error) {
      console.error('Update failed', update?.update_id, error.message);
      return new Response('Retry later', { status: 500 });
    }
  },
  async scheduled(_event, env) {
    if (!env.BOT_TOKEN) return;
    const { results } = await sql(env, `SELECT id FROM orders WHERE charge IS NOT NULL
      AND delivered=0 AND retry_at<=? ORDER BY retry_at LIMIT 10`, Date.now()).all();
    for (const { id } of results) await deliver(env, id);
  },
};
