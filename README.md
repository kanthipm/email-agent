# email-agent

Watches your Gmail and Outlook inboxes. When an email needs a reply, it texts you a summary and a
draft written in your voice. You edit the draft by texting back, and nothing is sent until you say
"send". You can also text it "email Sarah about the invoice" or "reply to the landlord's email" and it
figures out who and what from your past mail.

## How it works

- **Poller** scans each inbox every 30 minutes, never between 10pm and 7am local time (`QUIET_START_HOUR` / `QUIET_END_HOUR`). Overnight mail is picked up at 7am. Groq (`openai/gpt-oss-120b`, same as medpull-ortho) decides whether a human reply is expected
  (newsletters, receipts, notifications, FYI threads are skipped) and drafts one in your style.
- **SMS** goes over Sendblue (iMessage/SMS). Only texts from `MY_PHONE` are accepted.
- **Approval gate**: a draft carries a version number. Every edit bumps it. `send_draft` refuses unless
  the exact current version was shown to you in a text. "Shorten it and send" therefore shows you the
  new text and asks again instead of sending.
- **Style**: your last dozen sent emails from each account are fed to the model as examples.
- State lives in `agent.db` (SQLite): processed emails, drafts, and the SMS conversation.

## Setup

```powershell
cd email-agent
.venv\Scripts\activate          # already created; or: python -m venv .venv && pip install -r requirements.txt
```

1. **Keys**: fill in `MY_NAME` in `.env`. Groq and Sendblue keys are already copied from medpull-ortho.
2. **Gmail**: in Google Cloud Console create a project, enable the Gmail API, create an OAuth client of
   type Desktop, and either download the JSON to `credentials/gmail_client_secret.json` or paste the
   client ID and secret into `.env` as `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, then:
   ```powershell
   python scripts\auth_gmail.py
   ```
3. **Outlook**: the agent needs an Entra app registration to talk to Microsoft Graph. A university
   tenant (Duke) usually does not let students register apps, so register it somewhere you can:
   sign in to https://entra.microsoft.com with a *personal* Microsoft account (create one if needed;
   every account gets its own directory), App registrations > New, supported account types
   "Accounts in any organizational directory and personal Microsoft accounts", Authentication >
   "Allow public client flows" = Yes, API permissions > Microsoft Graph delegated `Mail.ReadWrite`,
   `Mail.Send`, `User.Read`. Put the Application (client) ID in `.env` as `OUTLOOK_CLIENT_ID`, keep
   `OUTLOOK_TENANT=common`, then sign in with the Duke account:
   ```powershell
   python scriptsuth_outlook.py
   ```
   If Duke answers "Need admin approval" at sign-in, the tenant blocks user consent for outside apps.
   Then either ask Duke OIT to approve the app, or add a rule in Duke Outlook that forwards mail to
   your Gmail and set `OUTLOOK_ENABLED=false`; the agent will see Duke mail through Gmail and reply
   from the Gmail address.
   Skip either provider with `GMAIL_ENABLED=false` / `OUTLOOK_ENABLED=false`.
4. **Webhook**: run the server and expose it (for example `ngrok http 8000`). In the Sendblue dashboard
   set the inbound webhook to `https://<host>/webhooks/sendblue/<SENDBLUE_WEBHOOK_SECRET>`.
   The value is in `.env`.

## Run

```powershell
python main.py                  # server on :8000, poller + SMS worker start with it
python scripts\chat.py          # talk to the agent in the terminal instead of by text
python scripts\chat.py --poll   # scan inboxes once, then chat
```

The first poll only records "now" and looks forward, so your backlog is never triaged.

## Texting it

```
New email (gmail) from Jane <jane@x.com>
Subject: Lunch?
Jane wants to know if Thursday lunch works.

Draft #4 (gmail, reply, v1)
To: jane@x.com
Subject: Re: Lunch?

Sure, Thursday works for me.

Reply "send" to send it, or tell me what to change.
```

- `make it warmer and suggest 12:30` → shows the new draft, asks again
- `send` → sends draft #4
- `email Priya and ask if the deck is ready` → finds the Priya you email most, drafts, asks to confirm
- `reply to the email about the lease renewal` → searches, reads the thread, drafts a reply
- `what's pending` / `drop #4`

## Layout

```
app/config.py      env
app/store.py       sqlite: processed mail, drafts, chat history
app/sms.py         sendblue send + webhook parsing
app/mail/base.py   EmailMessage / Contact / MailProvider interface
app/mail/gmail.py  Gmail API provider
app/mail/outlook.py Microsoft Graph provider
app/llm.py         groq: triage + drafting, SMS agent loop with tools
app/poller.py      inbox scan loop
app/server.py      fastapi: webhook, health, background threads
tests/             offline tests (pytest)
```
