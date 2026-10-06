#!/usr/bin/env python3

from __future__ import annotations
import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from logging import (
    ERROR,
    INFO,
    WARNING,
    FileHandler,
    StreamHandler,
    basicConfig,
    getLogger,
)
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from github import Auth, Github, GithubException, InputGitTreeElement
from github.Repository import Repository
from wzgram import Client, filters
from wzgram.errors import ListenerTimeout
from wzgram.types import Message
from web import start_web

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────
getLogger("wzgram").setLevel(ERROR)
getLogger("pyrogram").setLevel(ERROR)
getLogger("aiohttp").setLevel(WARNING)
getLogger("urllib3").setLevel(WARNING)

basicConfig(
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)
LOGGER = getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DATA_DIR = Path(__file__).parent / "data"
TOKENS_FILE = DATA_DIR / "tokens.json"
LOG_FILE = Path("log.txt")

MAX_ZIP_BYTES = 100 * 1024 * 1024


def _gh(token: str) -> Github:
    return Github(auth=Auth.Token(token))


def _load_tokens() -> Dict[str, str]:
    if not TOKENS_FILE.exists():
        return {}
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_tokens(tokens: Dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def get_user_token(user_id: int) -> Optional[str]:
    tokens = _load_tokens()
    return tokens.get(str(user_id))


def set_user_token(user_id: int, token: str) -> None:
    tokens = _load_tokens()
    tokens[str(user_id)] = token.strip()
    _save_tokens(tokens)


def delete_user_token(user_id: int) -> None:
    tokens = _load_tokens()
    tokens.pop(str(user_id), None)
    _save_tokens(tokens)


def _validate_token(token: str) -> Tuple[bool, str]:
    try:
        g = _gh(token)
        user = g.get_user()
        return True, f"Authenticated as **{user.login}**"
    except GithubException as e:
        msg = e.data.get("message", str(e)) if e.data else str(e)
        return False, f"Invalid token: {msg}"
    except Exception as e:
        return False, f"Error: {e}"


def _create_or_get_repo(
    token: str,
    repo_name: str,
    *,
    private: bool = True,
    description: str = "",
) -> Repository:
    g = _gh(token)
    user = g.get_user()

    try:
        LOGGER.info("Creating repo %s (private=%s, auto_init=True)", repo_name, private)
        repo = user.create_repo(
            name=repo_name,
            private=private,
            description=description or "Uploaded via Telegram bot",
            auto_init=True,
        )
        # Give GitHub a moment to finish initialising the default branch
        time.sleep(1.5)
        # Refresh so default_branch is populated
        repo = user.get_repo(repo_name)
        LOGGER.info("Repo created: %s  default_branch=%s", repo.html_url, repo.default_branch)
        return repo
    except GithubException as e:
        if e.status == 422 and "already exists" in str(e.data).lower():
            LOGGER.info("Repo %s already exists – using existing", repo_name)
            repo = user.get_repo(repo_name)
            return repo
        LOGGER.exception("Failed to create/get repo %s", repo_name)
        raise


def _ensure_branch(repo: Repository, branch: str) -> str:
    """
    Make sure `branch` exists.
    If the repo is completely empty, seed it with a .gitkeep via the Contents API
    (this is the only reliable way to initialise an empty GitHub repo).
    Returns the branch name that should be used for the upload.
    """
    # 1. Preferred branch already exists?
    try:
        repo.get_branch(branch)
        LOGGER.info("Branch '%s' already exists", branch)
        return branch
    except GithubException:
        pass

    # 2. Try the repo's default branch
    try:
        default = repo.default_branch
        if default and default != branch:
            try:
                repo.get_branch(default)
                LOGGER.info("Using existing default branch '%s' instead of '%s'", default, branch)
                return default
            except GithubException:
                pass
    except Exception:
        pass

    # 3. Try common names
    for candidate in ("main", "master"):
        if candidate == branch:
            continue
        try:
            repo.get_branch(candidate)
            LOGGER.info("Using existing branch '%s'", candidate)
            return candidate
        except GithubException:
            pass

    # 4. Repo is empty → seed it
    LOGGER.info("Repo is empty – seeding branch '%s' with .gitkeep", branch)
    try:
        repo.create_file(
            path=".gitkeep",
            message="Initial commit",
            content="",
            branch=branch,
        )
        LOGGER.info("Seeded empty repo on branch '%s'", branch)
        return branch
    except GithubException as e:
        msg = e.data.get("message", str(e)) if e.data else str(e)
        LOGGER.error("Failed to seed empty repo: %s", msg)
        raise


def _collect_files(extract_dir: Path) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    for root, dirs, filenames in os.walk(extract_dir):
        dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git")]
        for name in filenames:
            if name in (".DS_Store", "Thumbs.db"):
                continue
            full = Path(root) / name
            rel = full.relative_to(extract_dir).as_posix()
            try:
                files[rel] = full.read_bytes()
            except Exception:
                continue
    return files


def _upload_files_single_commit(
    repo: Repository,
    files: Dict[str, bytes],
    *,
    branch: str = "main",
    commit_message: str = "Upload from Telegram bot",
    target_subdir: str = "",
) -> str:
    if not files:
        raise ValueError("No files to upload")

    # Guarantee the branch exists (handles completely empty repos)
    branch = _ensure_branch(repo, branch)
    LOGGER.info("Uploading %d files to %s@%s", len(files), repo.full_name, branch)

    prefix = target_subdir.strip("/").strip()
    if prefix:
        prefix += "/"

    ref = repo.get_git_ref(f"heads/{branch}")
    base_sha = ref.object.sha
    base_tree = repo.get_git_tree(base_sha)
    parent = repo.get_git_commit(base_sha)

    element_list: List[InputGitTreeElement] = []

    for rel_path, content in files.items():
        full_path = prefix + rel_path
        try:
            text = content.decode("utf-8")
            element = InputGitTreeElement(
                path=full_path,
                mode="100644",
                type="blob",
                content=text,
            )
        except UnicodeDecodeError:
            b64 = base64.b64encode(content).decode("ascii")
            blob = repo.create_git_blob(b64, "base64")
            element = InputGitTreeElement(
                path=full_path,
                mode="100644",
                type="blob",
                sha=blob.sha,
            )
        element_list.append(element)

    tree = repo.create_git_tree(element_list, base_tree)
    commit = repo.create_git_commit(commit_message, tree, [parent])
    ref.edit(commit.sha)

    LOGGER.info("Commit created: %s", commit.html_url)
    return commit.html_url


# ─── Bot ────────────────────────────────────────────────────────────────────
app = Client(
    "github_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

HELP_TEXT = """
**Telegram → GitHub Uploader**

1. Set your GitHub Personal Access Token:
   `/set_token ghp_xxxxxxxxxxxx`
   (Token needs `repo` scope. Create one at https://github.com/settings/tokens)

2. Send a **ZIP file** of your project.

Bot will automatically:
• Create a **private** repo (or use existing if same name already exists)
• Use ZIP filename as repo name
• Branch `main`, commit message "Upload from Telegram bot"

Commands:
• `/start` – welcome
• `/set_token <PAT>` – save your GitHub token
• `/clear_token` – remove stored token
• `/help` – this message
• `/status` – show whether a token is stored
• `/log` – send the bot log file
"""


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply(
        "👋 Hi! I can upload the contents of a ZIP file straight to your GitHub account.\n\n"
        + HELP_TEXT
    )


@app.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    await message.reply(HELP_TEXT)


@app.on_message(filters.command("status") & filters.private)
async def status_handler(client: Client, message: Message):
    token = get_user_token(message.from_user.id)
    if token:
        ok, info = await asyncio.to_thread(_validate_token, token)
        if ok:
            await message.reply(f"✅ Token is set and valid.\n{info}")
        else:
            await message.reply(f"⚠️ Token is stored but invalid:\n{info}")
    else:
        await message.reply("❌ No token stored. Use `/set_token <your_PAT>`")


@app.on_message(filters.command("log") & filters.private)
async def log_handler(client: Client, message: Message):
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
        await message.reply("📭 Log file is empty.")
        return
    try:
        await message.reply_document(
            document=str(LOG_FILE),
            caption="📄 Bot log file",
        )
    except Exception as e:
        LOGGER.exception("Failed to send log")
        await message.reply(f"❌ Could not send log: {e}")


@app.on_message(filters.command("set_token") & filters.private)
async def set_token_handler(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)

    if len(parts) == 2:
        token = parts[1].strip()
    else:
        try:
            ans = await message.chat.ask(
                "Send me your GitHub Personal Access Token (it will be stored only on this server).\n"
                "Create one with **repo** scope: https://github.com/settings/tokens",
                filters=filters.text,
                timeout=120,
            )
            token = ans.text.strip()
        except ListenerTimeout:
            await message.reply("Timed out. Use `/set_token <token>` when ready.")
            return

    if not token or len(token) < 20:
        await message.reply("That does not look like a valid token.")
        return

    ok, info = await asyncio.to_thread(_validate_token, token)

    if not ok:
        await message.reply(f"❌ {info}")
        return

    set_user_token(message.from_user.id, token)

    try:
        await message.delete()
    except Exception:
        pass

    await message.reply(
        f"✅ Token saved.\n{info}\n\nYou can now send a ZIP file."
    )


@app.on_message(filters.command("clear_token") & filters.private)
async def clear_token_handler(client: Client, message: Message):
    delete_user_token(message.from_user.id)
    await message.reply("🗑️ Token removed.")


@app.on_message(filters.private & filters.document)
async def document_handler(client: Client, message: Message):
    user_id = message.from_user.id
    token = get_user_token(user_id)

    if not token:
        await message.reply(
            "You need to set a GitHub token first.\n"
            "Use `/set_token <PAT>` (needs `repo` scope)."
        )
        return

    doc = message.document
    file_name = doc.file_name or "file.zip"
    file_size = doc.file_size or 0

    if file_size > MAX_ZIP_BYTES:
        await message.reply(
            f"File is too large ({file_size // (1024 * 1024)} MB). "
            f"Max allowed: {MAX_ZIP_BYTES // (1024 * 1024)} MB."
        )
        return

    if not file_name.lower().endswith(".zip"):
        await message.reply("Please send a **.zip** archive of your project.")
        return

    status = await message.reply("📥 Downloading…")
    LOGGER.info("User %s sent ZIP: %s (%s bytes)", user_id, file_name, file_size)

    tmp_dir = Path(tempfile.mkdtemp(prefix="tg_gh_"))
    zip_path = tmp_dir / file_name
    extract_dir = tmp_dir / "extracted"

    try:
        await client.download_media(message, file_name=str(zip_path))
        await status.edit_text("📦 Extracting…")
        LOGGER.info("Downloaded to %s", zip_path)

        extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    continue
                zf.extract(member, extract_dir)

        files = await asyncio.to_thread(_collect_files, extract_dir)
        LOGGER.info("Extracted %d files", len(files))

        if not files:
            await status.edit_text(
                "The ZIP appears to be empty (or only contained junk files)."
            )
            return

        # ========== DEFAULTS ==========
        repo_name = Path(file_name).stem
        repo_name = re.sub(r"[^\w.\-]", "-", repo_name).strip("-") or "uploaded-project"
        private = True
        branch = "main"
        commit_message = "Upload from Telegram bot"
        target_subdir = ""
        # ==============================

        await status.edit_text(
            f"Found **{len(files)}** files.\n"
            f"Repo: `{repo_name}` (private, branch `{branch}`)\n\n"
            "🚀 Uploading to GitHub… this may take a moment."
        )

        def do_upload() -> Tuple[str, str]:
            repo = _create_or_get_repo(
                token,
                repo_name,
                private=private,
            )
            url = _upload_files_single_commit(
                repo,
                files,
                branch=branch,
                commit_message=commit_message,
                target_subdir=target_subdir,
            )
            return repo.html_url, url

        repo_url, commit_url = await asyncio.to_thread(do_upload)

        await status.edit_text(
            f"✅ **Done!**\n\n"
            f"Repository: {repo_url}\n"
            f"Commit: {commit_url}\n"
            f"Files uploaded: {len(files)}"
        )
        LOGGER.info("Upload success for user %s → %s", user_id, repo_url)

    except zipfile.BadZipFile:
        LOGGER.warning("Bad ZIP from user %s", user_id)
        await status.edit_text("❌ The file is not a valid ZIP archive.")
    except ValueError as e:
        LOGGER.error("ValueError: %s", e)
        await status.edit_text(f"❌ {e}")
    except GithubException as e:
        msg = e.data.get("message", str(e)) if e.data else str(e)
        LOGGER.error("GitHub error (status=%s): %s", e.status, msg)
        await status.edit_text(f"❌ GitHub error: {msg}")
    except Exception as e:
        LOGGER.exception("Unexpected error for user %s", user_id)
        await status.edit_text(
            f"❌ Unexpected error: {type(e).__name__}: {e}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        print("Please set API_ID, API_HASH and BOT_TOKEN environment variables.")
        raise SystemExit(1)

    async def main():
        port = int(os.getenv("PORT", "8080"))
        await start_web(port)
        await app.start()
        LOGGER.info("Bot started")
        print("Bot started…")
        await asyncio.Event().wait()

    asyncio.run(main())
