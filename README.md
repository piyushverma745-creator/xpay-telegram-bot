# xPay payment links on Telegram

A Telegram bot that lets a merchant create xPay payment links by chatting:

```
You:  link $2500 for Rahul rahul@gmail.com +91 98765 43210 for website redesign
Bot:  Check the details 👇
      Amount: $2,500.00 · Customer: Rahul · Expires: Tue 10 Nov (7 days)
      [✅ Create] [✏️ Edit] [❌ Cancel]
You:  (taps Create)
Bot:  ✅ Link created for $2,500.00
      https://api.test.xpaycheckout.com/link/…
Bot:  Hi Rahul, here's your payment link for $2,500.00 (website redesign): https://…   ← forward this
  …later…
Bot:  👀 Rahul just opened your $2,500.00 payment link.
Bot:  💰 Payment received! $2,500.00 from Rahul · Visa ••4242
```

A personal project, not an official xPay product. It runs on the **xPay sandbox**, and refuses to start against
live xPay unless you explicitly allow it. A Telegram bot is free and needs no business verification.

---

## 1. Try it in your terminal first (5 minutes, no Telegram needed)

You need Python 3.10 or newer.

```bash
git clone https://github.com/piyushverma745-creator/xpay-telegram-bot.git
cd xpay-telegram-bot
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python chat.py --mock        # fake xPay: try the whole conversation
```

Type like you would in a chat (`link 500 for rahul`), type `1`/`2`/`3` to tap buttons, and use `/open` and
`/pay` to pretend the customer opened or paid your latest link.

Then try it against the **real sandbox**: copy `.env.example` to `.env`, put your sandbox keys in
`XPAY_PUBLIC_KEY` / `XPAY_PRIVATE_KEY`, and run `python chat.py`. Links it creates are real sandbox links.
Open one and pay with a test card from xPay's *Payment Test Simulation* docs page.

Run the tests any time with `python -m pytest`.

## 2. Run it on Telegram (about 10 minutes)

### a) Create the bot
In Telegram, open **@BotFather**, send `/newbot`, pick a display name, then a username ending in `bot`.
BotFather replies with a **token** like `123456789:AAH…`. Keep it private: anyone with it controls the bot.

### b) A public URL for your laptop
The bot reads Telegram messages by asking Telegram for them (long polling), so Telegram needs no URL. But the
**connect page** and **xPay's payment alerts** do. Use [ngrok](https://ngrok.com/) with your free static domain
(ngrok dashboard → *Domains*), so the URL never changes:

```bash
ngrok http 8000 --url=https://YOUR-NGROK-DOMAIN     # older ngrok versions: --domain=YOUR-NGROK-DOMAIN
```

### c) Configure and run (Mac or Linux)
In a second Terminal window, in this folder:

```bash
bash setup.sh          # installs packages, asks for your ngrok URL, writes .env
bash add-telegram.sh   # paste the BotFather token (hidden); it checks the token with Telegram
bash start.sh          # starts the bot
```

The log should say `Telegram bot @YourBot is listening`. On Windows, copy `.env.example` to `.env`, fill it in,
and run `uvicorn app.main:create_app --factory --port 8000`.

### d) Say hi
Open `t.me/YourBot` and press **Start**. The bot replies with a one-time **connect page**. Open it (on ngrok's
free plan, tap *Visit Site* on the warning screen first), paste your xPay sandbox public and private keys, and
you're set. Then send `link $50 for Rahul`.

When you connect, the bot registers a webhook on your xPay sandbox account through xPay's API, so you get
"opened" and "paid" alerts automatically.

---

## What merchants can say

| Message | What happens |
|---|---|
| `link $250 for Rahul` | Starts a link and asks for whatever's missing (email, phone) |
| `link ₹5,000 for Priya priya@mail.com +91 98765 43210 for coaching, expires in 3 days` | Everything in one go |
| `amount 300`, `name Rahul S`, `note logo design`, `currency EUR`, `expires in 2 days` | Edits the link you're making |
| `recent` | Last 5 links: paid, opened, expired… |
| `default currency INR` | Changes the default currency |
| `cancel` / `help` / `connect` / `disconnect` | What they say (also in the bot's `/` menu) |

Amounts understand `$`, `€`, `£`, `₹`, `Rs.`, codes like `AED`, words like `dollars`, `2.5k`, and `1,00,000`.
Customers the merchant has billed before are remembered by name, so `link 300 for rahul` goes straight to the
confirm card the second time.

## How it works

```
Telegram ◄──(long polling)──► app/main.py ──► engine.py (the conversation) ──► xPay API
                                   ▲                                              │
                                   └──────────── /webhook/xpay/<id> ◄─────────────┘  payment events
```

| File | What it does |
|---|---|
| `app/engine.py` | The conversation: states, questions, confirm card, creating links, payment alerts. |
| `app/parser.py` | Reads amounts, currencies, names, emails, phones, notes and expiry out of a message. Rule-based: fast, free and predictable. |
| `app/xpay.py` | xPay client: Basic Auth, generate link, webhook config, signature check (HMAC-SHA512). |
| `app/telegram.py` | Telegram Bot API: long polling, messages with buttons, `/commands`. |
| `app/main.py` | FastAPI server: the Telegram poller, the xPay webhook and the connect page. |
| `app/store.py` | SQLite. xPay keys and webhook secrets are encrypted with `SECRET_KEY`. |
| `chat.py` | Terminal version of the bot. |

**Safety details already handled:** xPay keys never go through the chat, only through a one-time connect page
that expires in 15 minutes. The bot token is never written to the logs. xPay webhooks with a missing or forged
signature are rejected, and retries are de-duplicated. Only private chats are answered. Each link request
carries an `Idempotency-Key`, so a double tap or a retry after a timeout can't create two links. The bot is
pinned to the sandbox.

## Things to know

- **Start first.** A Telegram bot can't message someone who hasn't pressed *Start*, so payment alerts reach a
  merchant only after they've opened the bot once.
- **One copy at a time.** Only one running copy of the bot can read a Telegram bot's messages. If the log says
  another copy is running, stop the other one.
- **Check this in the sandbox:** the bot matches payment events to links through the `receiptId` it sends when
  creating the link. If xPay's `intent.success` event for a link payment doesn't carry that `receiptId`, alerts
  still arrive, but `recent` won't flip that link to *Paid*. Pay one sandbox link and check `recent`.
- **ngrok's browser warning.** On the free plan, opening the connect page shows an ngrok "You are about to
  visit…" screen first. Tap *Visit Site*. Webhooks aren't affected.
- **Changing your public URL** (e.g. a different tunnel) is handled: on startup the bot moves each merchant's
  xPay webhook to the new URL.
- **Voice notes and images** get a polite "text only for now" reply.
- **Sandbox resets.** xPay can reset sandbox data. If the bot says your keys stopped working, send `connect`.

## Ideas for next

- **Understand anything:** add an LLM step (e.g. Claude) for Hinglish, typos and voice notes (Telegram sends
  audio you can transcribe). Keep it scoped to xPay actions.
- **More actions:** link status by customer name, today's collections (`List Intents`), refunds with a
  confirmation step.
- **Deploy:** any host that runs a Python web app (Render, Railway, Fly.io). Mount a disk for `bot.db` or
  switch the store to Postgres.
