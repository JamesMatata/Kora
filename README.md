# Kora

**School operations for Kenya — roster, fees, and a WhatsApp agent that reminds parents and helps them pay.**

Kora is a multi-tenant Django platform for schools. Admins and bursars run classes, fee plans, and collections from one dashboard. Parents get fee reminders on WhatsApp, can pay by M-Pesa (STK), promise a date, or hand the chat to staff when a human is needed.

## Features

- **School workspace** — grades, streams, roster, attendance, staff roles (admin, bursar, teacher)
- **Fee operations** — term fee plans, invoices, ledger, bursaries/discounts, promises, receipts
- **M-Pesa** — Daraja STK push and Paybill C2B reconciliation (sandbox or production per school)
- **WhatsApp fee agent** — Twilio inbound/outbound, Gemini-assisted understanding, rules-first payment menu
- **Staff console** — claim escalated chats and reply to parents from the dashboard
- **Quiet hours & reminders** — scheduled overdue reminders with school quiet-hour settings

## Stack

| Layer | Technology |
| --- | --- |
| App | Django 5.2 |
| Database | SQLite (local default) |
| WhatsApp | Twilio |
| Payments | Safaricom Daraja (M-Pesa) |
| Agent | Google Gemini (`google-genai`) |

## Quick start

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # or: cp .env.example .env
```

Edit `.env` with at least:

- `SECRET_KEY`
- `GEMINI_API_KEY` (for the WhatsApp agent)
- Twilio + Daraja keys when testing live messaging / STK

Then:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Configuration

See `.env.example` for every supported variable. Important notes:

- **Twilio webhook** must include the school UUID:  
  `/api/v1/communications/twilio/whatsapp/?tenant_id=<school-uuid>`
- **Daraja callbacks** use the school webhook secret:  
  `?tenant_id=<uuid>&token=<secret>`
- Set `DARAJA_ENVIRONMENT=sandbox` for Safaricom sandbox STK (shortcode `174379`)
- Encrypt school credentials with `KORA_CREDENTIALS_KEY`, then  
  `python manage.py reencrypt_credentials` if you rotate the key

### Optional scheduled jobs

```bash
python manage.py run_fee_reminders
python manage.py enforce_platform_billing
python manage.py backup_database
```

## Project layout

```
apps/
  academics/        # Grades, streams, students, attendance
  communications/   # WhatsApp agent, Twilio webhooks, chat sessions
  dashboard/        # School UI (ledger, console, settings)
  finance/          # Fee plans, invoices, Daraja, receipts
  tenants/          # Schools, memberships, billing, marketing pages
config/             # Django settings & URLs
templates/          # Django templates
static/             # CSS and assets
```

## Security

- Never commit `.env`, `DEMO_CREDENTIALS.txt`, or live API keys
- School M-Pesa credentials are stored encrypted at rest
- Production (`DEBUG=False`) enables HTTPS redirect, secure cookies, and HSTS

## License

Proprietary — all rights reserved unless otherwise stated by the author.
