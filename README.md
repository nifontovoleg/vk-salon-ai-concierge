<p align="center">
  <strong>VK Salon AI Concierge</strong><br>
  <sub>AI-powered VK bot for beauty salons — booking, pricing, admin handoff</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/VKBottle-4-0077FF?logo=vk&logoColor=white" alt="VKBottle">
  <img src="https://img.shields.io/badge/OpenRouter-LLM-412991" alt="OpenRouter">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT">
</p>

---

## About

A **VK (VKontakte) community bot** for hair & beauty salons.

Clients message your group → the bot shows **real prices from the database**, helps **book appointments**, answers via **LLM**, and **hands off to a human admin** when needed.

> Prices and schedule are **never invented** — always from the database.

---

## Features

| Button | Result |
|--------|--------|
| **Book** | Booking guide + service list |
| **Services & prices** | Price list from DB |
| **Available slots** | Free time slots |
| **Admin** | Notify administrators |
| *"cancel"* | Return to bot |

Also supports **free-text** conversations via OpenRouter (DeepSeek, etc.).

---

## Quick start

```bash
git clone https://github.com/nifontovoleg/vk-salon-ai-concierge.git
cd vk-salon-ai-concierge

python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`:

```env
VK_GROUP_TOKEN=your_token
VK_GROUP_ID=123456789
OPENROUTER_API_KEY=your_key
```

```bash
python -m scripts.seed
python -m app.main
```

---

## VK setup

Enable in community settings:

- Community messages
- Long Poll API
- Event `message_new`

Token → `VK_GROUP_TOKEN`, group ID → `VK_GROUP_ID`.

---

## Stack

Python · VKBottle · SQLAlchemy · OpenRouter · SQLite / PostgreSQL

---

## FAQ

**Bot replies multiple times** — multiple instances running; keep only one process.

**OpenRouter 403** — switch model: `OPENROUTER_MODEL=deepseek/deepseek-chat`

**Stuck after Admin** — send `cancel` or run `python -m scripts.maintenance`

---

## License

[MIT](LICENSE)

<p align="center"><sub>vk-salon-ai-concierge</sub></p>
