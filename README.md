# Telegram → GitHub Uploader Bot

A Telegram bot built with **[wzgram](https://wzgram.com)** (Pyrogram fork) that lets you upload a project ZIP file directly to GitHub.

## Deploy 
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sirdozo/tggit)

## Features

- Send a `.zip` of any project to the bot
- Bot extracts the archive (skips `__MACOSX`, `.DS_Store`, etc.)
- Choose **create a new repository** or **upload into an existing one**
- Single atomic commit using the GitHub Git Data API (works with text + binary files)
- Optional: private repo, custom branch, commit message, target sub-folder
- Per-user GitHub Personal Access Token storage (simple local JSON)

## Prerequisites

1. **Telegram API credentials**  
   Go to https://my.telegram.org/apps → create an application → copy `api_id` and `api_hash`.

2. **Bot token**  
   Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

3. **GitHub Personal Access Token**  
   https://github.com/settings/tokens → Generate new token (classic) with the **`repo`** scope.

## Installation

```bash
git clone <this-repo>
cd telegram-github-bot

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

Copy the example env file and fill in your values:

```bash
cp .env.example .env
# edit .env with your API_ID, API_HASH, BOT_TOKEN
```

## Run

```bash
python bot.py
```

## Usage (inside Telegram)

1. `/start` or `/help`
2. `/set_token ghp_xxxxxxxxxxxxxxxx`  
   (the message containing the token is deleted automatically for privacy)
3. Send a **ZIP file** of your project.
4. Answer the questions:
   - `new` or `existing`
   - repository name (`my-cool-project` or `owner/my-cool-project`)
   - optional settings, e.g.  
     `private branch=main msg=Initial commit from Telegram path=src`

The bot will reply with links to the repository and the created commit.

### Other commands

| Command          | Description                     |
|------------------|---------------------------------|
| `/status`        | Check if a token is stored      |
| `/clear_token`   | Remove your stored PAT          |
| `/help`          | Show help                       |

## Security notes

- The GitHub PAT is stored in plain text in `data/tokens.json` on the machine running the bot.  
  For production use, encrypt it or use a proper secrets store.
- Never commit your `.env` or `data/` folder.
- The bot only accepts private chats by design (`filters.private`).

## Limitations

- Only `.zip` archives are supported (easy to extend for `.tar.gz` etc.).
- Maximum ZIP size: 100 MB (configurable in `bot.py`).
- GitHub file size limit via API still applies (~100 MB per file).
- Binary files are uploaded correctly via base64 blobs.

## License

MIT – do whatever you want.
