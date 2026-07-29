# Telegram Contact Bot

Ek private contact bot jo users ko bina tera Telegram ID/number jane, tujhse contact karne deta hai.

---

## Features

- User ka message + username + IST time admin ko forward hota hai
- `/reply U001 <message>` se user ko reply karo
- User block/unblock kar sakte ho
- Text, Photo, Document, Voice, Audio, Video — sab support hai
- Har user ka unique internal ID (U001, U002…)
- PostgreSQL mein sab data save hota hai
- Rate limiting — spam protection

---

## Setup

### 1. Bot Token Lao

1. Telegram pe [@BotFather](https://t.me/BotFather) pe jao
2. `/newbot` command do
3. Bot ka naam aur username set karo
4. Token copy kar lo → yeh `BOT_TOKEN` hai

### 2. Tera Admin ID Pata Karo

1. Telegram pe [@userinfobot](https://t.me/userinfobot) pe jao
2. `/start` karo — woh tera numeric ID bata dega
3. Yeh `ADMIN_TELEGRAM_ID` hai

### 3. Railway pe Deploy

1. **GitHub pe push karo** — sirf `telegram-bot/` folder ke files push karo (ya poora repo)

2. **Railway pe naya project banao:**
   - [railway.app](https://railway.app) pe jao
   - "New Project" → "Deploy from GitHub repo"
   - Apna repo select karo

3. **PostgreSQL add karo:**
   - Railway project mein "New Service" → "Database" → "PostgreSQL"
   - Database create hone ke baad `DATABASE_URL` automatically environment mein aa jaati hai

4. **Environment Variables set karo** (Railway → Variables tab):
   ```
   BOT_TOKEN=your_bot_token_here
   ADMIN_TELEGRAM_ID=123456789
   DATABASE_URL=  ← Railway automatically set karta hai PostgreSQL se
   ```

5. **Root directory set karo** (agar poora repo push kiya hai):
   - Railway → Settings → "Root Directory" → `telegram-bot`

6. Deploy ho jaayega — bot automatically start ho jaayega!

---

## Admin Commands

| Command | Kya Karta Hai |
|---|---|
| `/reply U001 <message>` | User U001 ko reply bhejo |
| `/block U001` | User ko block karo |
| `/unblock U001` | User ko unblock karo |
| `/blocked` | Sare blocked users dekho |
| `/users` | Sare users ki list dekho |
| `/help` | Commands ki list |

---

## Message Format (Admin ko dikhega)

```
📩 New Message
👤 User: U001
🔖 Username: @rahul_dev
🕐 29 Jul 2026, 08:05 PM IST
━━━━━━━━━━━━━━━━━━━━━━━━━━
Bhai teri services ka rate kya hai?
━━━━━━━━━━━━━━━━━━━━━━━━━━
💬 Reply: /reply U001 <message>
```

---

## Local Testing (Optional)

```bash
cd telegram-bot
pip install -r requirements.txt
cp .env.example .env
# .env mein apni values fill karo
python main.py
```

---

## File Structure

```
telegram-bot/
├── main.py          # Bot handlers — sab kuch yahan hai
├── database.py      # PostgreSQL operations
├── config.py        # Environment variables
├── requirements.txt
├── .env.example     # Template — .env mein copy karo
├── Procfile         # Railway ke liye
└── README.md
```
