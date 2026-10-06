#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from github import Github, GithubException, InputGitTreeElement
from github.Repository import Repository
from wzgram import Client, filters
from wzgram.errors import ListenerTimeout
from wzgram.types import Message

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DATA_DIR = Path(__file__).parent / "data"
TOKENS_FILE = DATA_DIR / "tokens.json"

MAX_ZIP_BYTES = 100 * 1024 * 1024

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
        g = Github(token)
        user = g.get_user()
        return True, f"Authenticated as **{user.login}**"
    except GithubException as e:
        return False, f"Invalid token: {e.data.get('message', str(e)) if e.data else str(e)}"
    except Exception as e:
        return False, f"Error: {e}"

def _create_or_get_repo(
    token: str,
    repo_name: str,
    *,
    create_new: bool,
    private: bool = False,
    description: str = "",
    auto_init: bool = False,
) -> Repository:
    g = Github(token)
    user = g.get_user()

    if create_new:
        try:
            repo = user.create_repo(
                name=repo_name,
                private=private,
                description=description or "Uploaded via Telegram bot",
                auto_init=auto_init,
            )
            return repo
        except GithubException as e:
            if e.status == 422 and "already exists" in str(e.data).lower():
                raise ValueError(
                    f"Repository `{repo_name}` already exists. Use existing-repo mode."
                )
            raise
    else:
        if "/" in repo_name:
            return g.get_repo(repo_name)
        return user.get_repo(repo_name)

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

    prefix = target_subdir.strip("/").strip()
    if prefix:
        prefix += "/"

    try:
        ref = repo.get_git_ref(f"heads/{branch}")
        base_sha = ref.object.sha
        base_tree = repo.get_git_tree(base_sha)
        parent = repo.get_git_commit(base_sha)
        parents = [parent]
        is_new_branch = False
    except GithubException:
        base_tree = None
        parents = []
        is_new_branch = True

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
    commit = repo.create_git_commit(commit_message, tree, parents)

    if is_new_branch:
        repo.create_git_ref(f"refs/heads/{branch}", commit.sha)
    else:
        ref.edit(commit.sha)

    return commit.html_url

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

3. Answer the questions the bot asks:
   - Create new repo **or** upload into existing one
   - Repository name
   - (optional) private / public, branch, commit message, sub-folder

Commands:
• `/start` – welcome
• `/set_token <PAT>` – save your GitHub token
• `/clear_token` – remove stored token
• `/help` – this message
• `/status` – show whether a token is stored
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="tg_gh_"))
    zip_path = tmp_dir / file_name
    extract_dir = tmp_dir / "extracted"

    try:
        await client.download_media(message, file_name=str(zip_path))
        await status.edit_text("📦 Extracting…")

        extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    continue
                zf.extract(member, extract_dir)

        files = await asyncio.to_thread(_collect_files, extract_dir)

        if not files:
            await status.edit_text(
                "The ZIP appears to be empty (or only contained junk files)."
            )
            return

        await status.edit_text(
            f"Found **{len(files)}** files.\n\n"
            "Do you want to **create a new repository** or **upload into an existing one**?\n"
            "Reply with `new` or `existing`."
        )

        try:
            mode_msg = await message.chat.ask(
                filters=filters.text & filters.user(user_id),
                timeout=120,
            )
        except ListenerTimeout:
            await status.edit_text("Timed out. Send the ZIP again when ready.")
            return

        mode = mode_msg.text.strip().lower()
        create_new = mode in ("new", "create", "n", "c")

        if mode not in (
            "new",
            "create",
            "n",
            "c",
            "existing",
            "exist",
            "e",
            "old",
        ):
            await message.reply("Please answer `new` or `existing`. Aborting.")
            return

        try:
            name_msg = await message.chat.ask(
                "Repository name"
                + (
                    " (will be created under your account)"
                    if create_new
                    else " (e.g. `my-project` or `owner/my-project`)"
                )
                + ":",
                filters=filters.text & filters.user(user_id),
                timeout=120,
            )
        except ListenerTimeout:
            await message.reply("Timed out.")
            return

        repo_name = name_msg.text.strip().replace(" ", "-")

        if not re.match(r"^[\w.\-]+(/[\w.\-]+)?$", repo_name):
            await message.reply("Invalid repository name.")
            return

        try:
            opts_msg = await message.chat.ask(
                "Optional settings (press Enter / send `-` to use defaults):\n"
                "• `private` – make the repo private (only for new)\n"
                "• `branch=main` – target branch\n"
                "• `msg=Your commit message`\n"
                "• `path=subdir` – upload into a sub-folder\n\n"
                "Example: `private branch=main msg=Initial upload path=src`",
                filters=filters.text & filters.user(user_id),
                timeout=120,
            )
            opts_text = opts_msg.text.strip()
        except ListenerTimeout:
            opts_text = ""

        private = False
        branch = "main"
        commit_message = "Upload from Telegram bot"
        target_subdir = ""

        if opts_text and opts_text != "-":
            if "private" in opts_text.lower():
                private = True

            m = re.search(r"branch=([^\s]+)", opts_text, re.I)
            if m:
                branch = m.group(1)

            m = re.search(r"msg=(.+?)(?:\s+\w+=|$)", opts_text, re.I)
            if m:
                commit_message = m.group(1).strip()

            m = re.search(r"path=([^\s]+)", opts_text, re.I)
            if m:
                target_subdir = m.group(1)

        await status.edit_text(
            "🚀 Uploading to GitHub… this may take a moment."
        )

        def do_upload() -> Tuple[str, str]:
            repo = _create_or_get_repo(
                token,
                repo_name,
                create_new=create_new,
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

    except zipfile.BadZipFile:
        await status.edit_text("❌ The file is not a valid ZIP archive.")
    except ValueError as e:
        await status.edit_text(f"❌ {e}")
    except GithubException as e:
        msg = e.data.get("message", str(e)) if e.data else str(e)
        await status.edit_text(f"❌ GitHub error: {msg}")
    except Exception as e:
        await status.edit_text(
            f"❌ Unexpected error: {type(e).__name__}: {e}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        print(
            "Please set API_ID, API_HASH and BOT_TOKEN environment variables "
            "(or in a .env file)."
        )
        raise SystemExit(1)

    print("Bot starting…")
    app.run()
