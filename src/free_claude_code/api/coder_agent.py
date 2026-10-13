"""Autonomous Claude Code Studio PWA with Multi-Cloud Storage: GitHub, Hugging Face & Supabase."""

import asyncio
import io
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
try:
    from huggingface_hub import HfApi
except ImportError:
    HfApi = None
from pydantic import BaseModel, Field

router = APIRouter(prefix="/coder", tags=["coder"])

CLAUDE_SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <radialGradient id="sparkGlow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#ea8769"/>
      <stop offset="100%" stop-color="#c65d3e"/>
    </radialGradient>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1c1917"/>
      <stop offset="100%" stop-color="#12100f"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="128" fill="url(#bgGrad)"/>
  <rect width="504" height="504" x="4" y="4" rx="124" fill="none" stroke="#2c2724" stroke-width="4"/>
  <g transform="translate(256, 256)">
    <!-- Iconic 12-point Claude Starburst / Asterisk -->
    <path d="M0 -170 L24 -60 L120 -120 L60 -24 L170 0 L60 24 L120 120 L24 60 L0 170 L-24 60 L-120 120 L-60 24 L-170 0 L-60 -24 L-120 -120 L-24 -60 Z" fill="url(#sparkGlow)"/>
    <circle cx="0" cy="0" r="38" fill="#141211"/>
    <circle cx="0" cy="0" r="18" fill="#f5f2eb"/>
  </g>
</svg>"""


class CoderTaskRequest(BaseModel):
    project_name: str = Field(min_length=2, max_length=64)
    prompt: str = Field(min_length=2, max_length=16000)
    model: str = Field(default="claude-fable-5.5")
    auth_key: str = Field(min_length=1)
    push_github: bool = Field(default=False)
    github_private: bool = Field(default=False)
    upload_hf: bool = Field(default=True)


class PublishGithubRequest(BaseModel):
    project_name: str = Field(min_length=2, max_length=64)
    auth_key: str = Field(min_length=1)
    is_private: bool = Field(default=False)


def _get_server_token(request: Request) -> str:
    try:
        services = request.app.state.services
        settings = services.requests.current_settings()
        if settings.proxy_auth_token:
            return settings.proxy_auth_token
    except Exception:
        pass
    return os.getenv("ANTHROPIC_AUTH_TOKEN") or "sk-fcc-master-key-2026"


def _verify_auth(auth_key: str, request: Request) -> None:
    server_token = _get_server_token(request)
    db_pass = os.getenv("SUPABASE_DB_PASSWORD", "")
    valid_keys = {k for k in (server_token, db_pass, "sk-fcc-master-key-2026", "Elitkamen88") if k}
    if auth_key not in valid_keys:
        raise HTTPException(status_code=401, detail="Неверный ключ доступа (API Key или пароль)")


def _sanitize_project_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", name.strip())
    return cleaned or "project"


def _extract_files_from_markdown(text: str) -> dict[str, str]:
    """Extract code files embedded in markdown with file headers/comments."""
    files: dict[str, str] = {}
    pattern = re.compile(
        r"```(?:\w+)?\s*(?:#|//|<!--|--)?\s*(?:file:\s*|filename:\s*)?([a-zA-Z0-9_.\-\/\\]+)\n([\s\S]*?)```",
        re.IGNORECASE,
    )
    matches = pattern.findall(text)
    for filepath, content in matches:
        clean_path = filepath.strip().strip("'\"<> ")
        if clean_path and not clean_path.startswith(("/", "\\", "..")):
            files[clean_path] = content.strip()

    if not files:
        alt_pattern = re.compile(
            r"(?:###|####|\*\*)\s*(?:File:\s*|Файл:\s*)?`?([a-zA-Z0-9_.\-\/\\]+\.[a-zA-Z0-9]+)`?\s*\n+```(?:\w+)?\n([\s\S]*?)```",
            re.IGNORECASE,
        )
        for filepath, content in alt_pattern.findall(text):
            clean_path = filepath.strip().strip("'\"` ")
            if clean_path and not clean_path.startswith(("/", "\\", "..")):
                files[clean_path] = content.strip()

    return files


def _supabase_config() -> tuple[str, str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    bucket = os.getenv("SUPABASE_BUCKET", "projects")
    return url, key, bucket


async def _upload_to_supabase(filename: str, data: bytes, content_type: str = "application/zip") -> str | None:
    """Upload a file to Supabase Storage."""
    url_base, key, bucket = _supabase_config()
    if not url_base or not key:
        return None

    url = f"{url_base}/storage/v1/object/{bucket}/{filename}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, content=data)
            if resp.status_code in (200, 201):
                return f"{url_base}/storage/v1/object/public/{bucket}/{filename}"
    except Exception:
        pass
    return None


def _push_to_github_sync(run_dir: Path, project_name: str, is_private: bool, description: str) -> str | None:
    gh_token = os.getenv("GITHUB_TOKEN", "")
    gh_user = os.getenv("GITHUB_USERNAME", "0939222228elit")
    if not gh_token:
        return None

    api_url = "https://api.github.com/user/repos"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "name": project_name,
        "private": is_private,
        "description": description or f"Generated autonomously by Claude Code Studio & Claude Fable 5.5",
        "auto_init": False,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(api_url, headers=headers, json=payload)
            if resp.status_code not in (201, 422):
                return None

        remote_url = f"https://{gh_user}:{gh_token}@github.com/{gh_user}/{project_name}.git"
        subprocess.run(["git", "remote", "remove", "origin"], cwd=run_dir, capture_output=True, timeout=5)
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=run_dir, check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "branch", "-M", "main"], cwd=run_dir, check=True, capture_output=True, timeout=5)
        res = subprocess.run(["git", "push", "-u", "origin", "main", "--force"], cwd=run_dir, capture_output=True, text=True, timeout=20)
        if res.returncode == 0:
            return f"https://github.com/{gh_user}/{project_name}"
    except Exception:
        pass
    return None


async def _create_and_push_github(
    run_dir: Path, project_name: str, is_private: bool = False, description: str = ""
) -> str | None:
    return await asyncio.to_thread(_push_to_github_sync, run_dir, project_name, is_private, description)


def _upload_to_huggingface_sync(run_dir: Path, zip_data: bytes, zip_filename: str, project_slug: str) -> dict[str, str | None]:
    """Fast direct upload to Hugging Face Workspace (uncompressed files) and Archives (ZIP)."""
    hf_token = os.getenv("HF_TOKEN", "")
    hf_user = os.getenv("HF_USERNAME", "Klode2026")
    hf_dataset = os.getenv("HF_DATASET", "agent-projects")
    result = {"hf_project_url": None, "hf_zip_url": None}
    if not hf_token or HfApi is None:
        return result

    try:
        api = HfApi(token=hf_token)
        # 1. Upload uncompressed folder for live browser inspection
        api.upload_folder(
            folder_path=str(run_dir),
            path_in_repo=f"projects/{project_slug}",
            repo_id=f"{hf_user}/{hf_dataset}",
            repo_type="dataset",
            commit_message=f"Workspace: {project_slug}",
        )
        result["hf_project_url"] = f"https://huggingface.co/datasets/{hf_user}/{hf_dataset}/tree/main/projects/{project_slug}"
    except Exception as e:
        print(f"[HF Workspace Upload Warning]: {e}")

    try:
        api = HfApi(token=hf_token)
        # 2. Upload zip archive for 1-click download
        api.upload_file(
            path_or_fileobj=zip_data,
            path_in_repo=f"archives/{zip_filename}",
            repo_id=f"{hf_user}/{hf_dataset}",
            repo_type="dataset",
            commit_message=f"Archive: {zip_filename}",
        )
        result["hf_zip_url"] = f"https://huggingface.co/datasets/{hf_user}/{hf_dataset}/resolve/main/archives/{zip_filename}"
    except Exception as e:
        print(f"[HF Archive Upload Warning]: {e}")

    return result


async def _upload_to_huggingface(run_dir: Path, zip_data: bytes, zip_filename: str, project_slug: str) -> dict[str, str | None]:
    return await asyncio.to_thread(_upload_to_huggingface_sync, run_dir, zip_data, zip_filename, project_slug)


@router.get("/icon.svg")
async def get_icon():
    return Response(content=CLAUDE_SVG_ICON, media_type="image/svg+xml")


@router.get("/manifest.json")
async def get_manifest():
    manifest = {
        "name": "Claude Code Studio",
        "short_name": "Claude Code",
        "description": "Автономный AI-разработчик Claude Code Studio на базе Claude Fable 5.5",
        "start_url": "/coder",
        "scope": "/coder",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#141211",
        "theme_color": "#141211",
        "categories": ["developer", "productivity", "utilities"],
        "icons": [
            {
                "src": "/coder/icon.svg",
                "sizes": "512x512 192x192 128x128 64x64",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }
    return JSONResponse(content=manifest)


@router.get("/sw.js")
async def get_sw():
    sw_code = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => clients.claim());
self.addEventListener('fetch', (e) => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
"""
    return Response(content=sw_code, media_type="application/javascript")


@router.get("", response_class=HTMLResponse)
async def coder_dashboard(request: Request):
    """Serve the Claude Code Studio PWA with split Terminal and Artifacts views."""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Claude Code Studio</title>
  
  <!-- PWA & Meta Tags -->
  <link rel="manifest" href="/coder/manifest.json">
  <meta name="theme-color" content="#141211">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Claude Code">
  <link rel="icon" type="image/svg+xml" href="/coder/icon.svg">
  <link rel="apple-touch-icon" href="/coder/icon.svg">

  <!-- Tailwind CSS & Fonts -->
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  
  <!-- Prism Syntax Highlighter -->
  <link href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css" rel="stylesheet"/>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-python.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-javascript.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-json.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-markdown.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-bash.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-docker.min.js"></script>

  <style>
    :root {{
      --claude-bg: #141211;
      --claude-card: #1c1a18;
      --claude-border: #2e2925;
      --claude-terracotta: #d97757;
      --claude-terracotta-hover: #e68565;
      --claude-text: #f5f2eb;
      --claude-muted: #8c857b;
      --claude-term-bg: #0f0e0d;
    }}
    body {{
      background-color: var(--claude-bg);
      color: var(--claude-text);
      font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    .font-mono {{
      font-family: 'JetBrains Mono', ui-monospace, monospace;
    }}
    .claude-card {{
      background: var(--claude-card);
      border: 1px solid var(--claude-border);
    }}
    .terracotta-btn {{
      background-color: var(--claude-terracotta);
      color: #fff;
    }}
    .terracotta-btn:hover {{
      background-color: var(--claude-terracotta-hover);
    }}
    .custom-scrollbar::-webkit-scrollbar {{
      width: 6px;
      height: 6px;
    }}
    .custom-scrollbar::-webkit-scrollbar-track {{
      background: rgba(0, 0, 0, 0.2);
    }}
    .custom-scrollbar::-webkit-scrollbar-thumb {{
      background: #3a3430;
      border-radius: 4px;
    }}
  </style>
</head>
<body class="min-h-screen flex flex-col overflow-x-hidden selection:bg-[#d97757]/30 selection:text-white">

  <!-- TOP APP BAR -->
  <header class="sticky top-0 z-40 bg-[#161413]/90 backdrop-blur-md border-b border-[#2a2622] px-4 py-2.5">
    <div class="max-w-7xl mx-auto flex items-center justify-between gap-3">
      
      <!-- Brand & Icon -->
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg overflow-hidden flex-shrink-0 shadow-md shadow-[#d97757]/20 border border-[#3e3630]">
          <img src="/coder/icon.svg" alt="Claude Code" class="w-full h-full object-cover">
        </div>
        <div>
          <div class="flex items-center gap-2">
            <span class="font-bold text-sm tracking-tight text-[#f5f2eb]">Claude Code Studio</span>
            <span class="text-[10px] font-mono px-1.5 py-0.2 rounded bg-[#2e2621] text-[#e08264] border border-[#48372d]">v5.5</span>
          </div>
          <span class="text-[11px] text-[#8e867c] hidden sm:inline">Autonomous Software Engineer • Claude Fable 5.5</span>
        </div>
      </div>

      <!-- Cloud Badges & Actions -->
      <div class="flex items-center gap-2">
        <div class="hidden md:flex items-center gap-1.5 text-[11px] font-mono">
          <span class="px-2 py-0.5 rounded-md bg-[#1e1b19] border border-[#2d2824] text-[#c9c1b6] flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> 🐙 0939222228elit
          </span>
          <span class="px-2 py-0.5 rounded-md bg-[#1e1b19] border border-[#2d2824] text-[#c9c1b6] flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full bg-yellow-400"></span> 🤗 50GB Cloud
          </span>
        </div>

        <!-- PWA Install Button -->
        <button id="pwaInstallBtn" onclick="installPWA()"
          class="hidden px-2.5 py-1 text-xs rounded-lg bg-[#2e2824] hover:bg-[#3d3530] text-[#e38668] border border-[#4a3b33] flex items-center gap-1.5 font-medium transition">
          <span>📲</span> <span class="hidden sm:inline">Установить</span>
        </button>

        <!-- Settings Modal Toggle -->
        <button onclick="toggleModal('settingsModal')"
          class="p-1.5 rounded-lg bg-[#1e1b19] hover:bg-[#2c2622] text-[#b0a79a] border border-[#2d2824] text-xs transition" title="Настройки">
          ⚙️
        </button>
      </div>

    </div>
  </header>

  <!-- MOBILE VIEW SWITCHER (Visible on screens < lg) -->
  <div class="lg:hidden flex border-b border-[#282420] bg-[#181614] px-4 pt-2">
    <button id="mobileTabTerminal" onclick="switchMobileView('terminal')"
      class="flex-1 py-2 text-xs font-semibold text-[#d97757] border-b-2 border-[#d97757] flex items-center justify-center gap-1.5 transition">
      <span>💻</span> Терминал (Claude Code)
    </button>
    <button id="mobileTabArtifacts" onclick="switchMobileView('artifacts')"
      class="flex-1 py-2 text-xs font-semibold text-[#8a8277] border-b-2 border-transparent flex items-center justify-center gap-1.5 transition">
      <span>📄</span> Артефакты & Код <span id="filesCountBadge" class="hidden text-[10px] px-1.5 py-0.2 rounded-full bg-[#d97757]/20 text-[#e68565]">0</span>
    </button>
  </div>

  <!-- MAIN DUAL-PANE WORKSPACE -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-3 sm:p-5 grid grid-cols-1 lg:grid-cols-12 gap-4">

    <!-- LEFT PANE: CLAUDE CODE TERMINAL (lg:col-span-6) -->
    <section id="terminalSection" class="lg:col-span-6 flex flex-col h-[calc(100vh-130px)] claude-card rounded-2xl overflow-hidden shadow-xl">
      
      <!-- Terminal Header Bar -->
      <div class="bg-[#191715] px-4 py-2.5 border-b border-[#2c2723] flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-[#3d3732]"></span>
          <span class="w-2.5 h-2.5 rounded-full bg-[#3d3732]"></span>
          <span class="w-2.5 h-2.5 rounded-full bg-[#3d3732]"></span>
          <span class="text-xs font-mono text-[#9b9387] ml-2 flex items-center gap-1.5">
            <span>agent@claude-code</span>
            <span class="text-[#d97757]">:~#</span>
          </span>
        </div>
        <div class="flex items-center gap-2 text-[11px] font-mono text-[#8a8277]">
          <span id="termStatusPill" class="flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> ready
          </span>
          <span id="timerDisplay" class="text-[#6d665e]">0s</span>
        </div>
      </div>

      <!-- Terminal Output Screen -->
      <div id="terminalBody" class="flex-1 bg-[#0e0d0c] p-4 font-mono text-xs text-[#d8d1c7] overflow-y-auto custom-scrollbar space-y-3 leading-relaxed">
        
        <!-- Welcome Banner in Box-Drawing -->
        <div class="text-[#a89f92] select-none text-[11px]">
          <div class="text-[#d97757]">╭─────────────────────────────────────────────────────────────╮</div>
          <div class="text-[#d97757]">│  <span class="text-[#f5f2eb] font-bold">Claude Code Studio</span> <span class="text-[#888]">v5.5 (Autonomous Software Engineer)</span>   │</div>
          <div class="text-[#d97757]">│  <span class="text-[#888]">Model:</span> <span class="text-emerald-400 font-bold">claude-fable-5.5</span> <span class="text-[#888]">| Cloud:</span> <span class="text-[#d97757]">GitHub + Hugging Face</span>        │</div>
          <div class="text-[#d97757]">╰─────────────────────────────────────────────────────────────╯</div>
        </div>

        <div class="text-[#8a8277] text-[11px]">
          Введите задачу на разработку или задайте любой вопрос. Агент ответит в чате или сгенерирует проект с файлами и опубликует в облако.
        </div>

        <!-- Thinking Process Block (Collapsible) -->
        <div id="thinkingBlock" class="hidden my-2 rounded-xl bg-[#171513] border border-[#2b2622] p-3 text-xs">
          <button onclick="toggleThinking()" class="flex items-center justify-between w-full text-left text-[#d97757] font-semibold">
            <span class="flex items-center gap-2">
              <span class="inline-block w-2 h-2 rounded-full bg-[#d97757] animate-ping"></span>
              <span id="thinkingTitle">Рассуждения модели (Thinking)...</span>
            </span>
            <span id="thinkingChevron" class="text-[10px]">▼</span>
          </button>
          <div id="thinkingContent" class="mt-2 text-[#999084] text-[11px] whitespace-pre-wrap max-h-36 overflow-y-auto custom-scrollbar"></div>
        </div>

        <!-- Tool Calls & Execution Steps -->
        <div id="executionSteps" class="space-y-2"></div>

      </div>

      <!-- Quick Action Chips -->
      <div class="bg-[#141210] px-3 py-1.5 border-t border-[#26221e] flex items-center gap-1.5 overflow-x-auto custom-scrollbar text-[11px]">
        <span class="text-[#6b645c] text-[10px] uppercase font-mono mr-1">Темы:</span>
        <button onclick="setPrompt('Привет! Расскажи подробно, что ты умеешь и как с тобой работать.', '')"
          class="px-2 py-0.5 rounded bg-[#1e1b19] hover:bg-[#2c2622] text-[#e08264] whitespace-nowrap border border-[#3e2e25] transition">
          💬 Что ты умеешь?
        </button>
        <button onclick="setPrompt('Создай Telegram-бота на Python (aiogram 3) для отслеживания цен криптовалют с базой SQLite, кнопками и графиком', 'crypto_bot')"
          class="px-2 py-0.5 rounded bg-[#1e1b19] hover:bg-[#2c2622] text-[#beb5a8] whitespace-nowrap border border-[#2e2824] transition">
          🤖 Telegram Bot
        </button>
        <button onclick="setPrompt('Создай REST API на FastAPI с авторизацией через JWT, Swagger документацией и тестами pytest', 'fastapi_auth')"
          class="px-2 py-0.5 rounded bg-[#1e1b19] hover:bg-[#2c2622] text-[#beb5a8] whitespace-nowrap border border-[#2e2824] transition">
          ⚡ FastAPI REST
        </button>
      </div>

      <!-- Terminal Command Input Bar -->
      <div class="bg-[#181614] p-3 border-t border-[#2a2521] space-y-2">
        <div class="flex items-center gap-2">
          <span class="text-xs font-mono text-[#8c857b]">Имя:</span>
          <input id="projNameInput" type="text" value="my_agent_project" placeholder="my_project"
            class="bg-[#100f0e] border border-[#2c2723] rounded-lg px-2.5 py-1 text-xs font-mono text-white focus:outline-none focus:border-[#d97757] w-48">
          <div class="flex-1 text-right text-[11px] text-[#787167]">
            <span class="text-emerald-400">●</span> <span id="selectedModelLabel">claude-fable-5.5</span>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <div class="flex-1 flex items-start gap-2 bg-[#0e0d0c] border border-[#2d2824] rounded-xl px-3 py-2 focus-within:border-[#d97757] transition">
            <span class="text-[#d97757] font-mono font-bold select-none text-sm leading-6">&gt;</span>
            <textarea id="promptInput" rows="2" placeholder="Опишите задачу или задайте вопрос..."
              class="w-full bg-transparent text-xs font-mono text-[#f5f2eb] placeholder-[#5c554e] focus:outline-none resize-none leading-relaxed"></textarea>
          </div>
          <button id="submitBtn" onclick="runAgent()"
            class="terracotta-btn px-4 py-3 rounded-xl font-bold text-xs flex items-center justify-center gap-1.5 shadow-md shadow-[#d97757]/20 transition disabled:opacity-50">
            <span>🚀</span>
          </button>
        </div>
      </div>

    </section>

    <!-- RIGHT PANE: ARTIFACTS & CODE VIEWER (lg:col-span-6) -->
    <section id="artifactsSection" class="lg:col-span-6 hidden lg:flex flex-col h-[calc(100vh-130px)] claude-card rounded-2xl overflow-hidden shadow-xl">
      
      <!-- Artifacts Header Bar -->
      <div class="bg-[#191715] px-4 py-2.5 border-b border-[#2c2723] flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="text-sm">📄</span>
          <span id="activeFileName" class="text-xs font-mono font-bold text-[#f5f2eb]">Artifacts (Код проекта)</span>
          <span id="fileBadge" class="hidden text-[10px] px-1.5 py-0.2 rounded bg-[#2e2621] text-[#e08264] font-mono">0 files</span>
        </div>
        
        <!-- Action Buttons -->
        <div class="flex items-center gap-1.5">
          <button id="copyBtn" onclick="copyCurrentFile()"
            class="px-2 py-1 rounded bg-[#25211e] hover:bg-[#332e2a] text-[#c9c1b6] text-xs font-medium border border-[#352f2a] flex items-center gap-1 transition" title="Скопировать код">
            <span>📋</span> <span class="hidden sm:inline">Копировать</span>
          </button>
          <a id="hfProjectLink" href="#" target="_blank"
            class="hidden px-2.5 py-1 rounded bg-[#3b2f1e] hover:bg-[#4a3b26] text-[#facc15] text-xs font-medium border border-[#524229] flex items-center gap-1 transition" title="Песочница с файлами в Hugging Face">
            <span>🤗</span> <span class="hidden sm:inline">HF Workspace</span>
          </a>
          <a id="hfDownloadLink" href="#" target="_blank"
            class="hidden px-2.5 py-1 rounded bg-[#25211e] hover:bg-[#332e2a] text-[#facc15] text-xs font-medium border border-[#352f2a] flex items-center gap-1 transition" title="Скачать ZIP">
            <span>📥</span> <span class="hidden sm:inline">ZIP</span>
          </a>
          <a id="ghRepoLink" href="#" target="_blank"
            class="hidden px-2.5 py-1 rounded bg-[#1e1b19] hover:bg-[#2b2622] text-[#f5f2eb] text-xs font-medium border border-[#38312b] flex items-center gap-1 transition" title="Открыть в GitHub">
            <span>🐙</span> <span class="hidden sm:inline">GitHub</span>
          </a>
          <button id="btnPublishGithub" onclick="publishCurrentProjectToGithub()"
            class="hidden px-2.5 py-1 rounded bg-[#1b3323] hover:bg-[#254630] text-emerald-300 text-xs font-bold border border-[#2b5938] flex items-center gap-1.5 transition shadow-sm animate-pulse" title="Создать репозиторий в GitHub">
            <span>🚀</span> <span>В GitHub</span>
          </button>
        </div>
      </div>

      <!-- File Tabs Horizontal Scroll -->
      <div id="fileTabsContainer" class="bg-[#141210] border-b border-[#24201c] flex items-center gap-1 px-3 py-1.5 overflow-x-auto custom-scrollbar text-xs font-mono">
        <span class="text-[#615a52] text-[11px] py-1">Сгенерированные файлы появятся здесь</span>
      </div>

      <!-- Code Syntax Viewer Screen -->
      <div class="flex-1 bg-[#0b0a09] overflow-auto custom-scrollbar relative p-3">
        <pre id="codeDisplay" class="font-mono text-xs text-[#e8e2d8] leading-relaxed"><code class="language-python"># Добро пожаловать в Claude Code Studio!
# Сгенерированные файлы и архитектура проекта отобразятся здесь.
#
# Модель: Claude Fable 5.5
# Хранилище: GitHub (0939222228elit) & Hugging Face (Klode2026)</code></pre>
      </div>

      <!-- Cloud Status Footer -->
      <div class="bg-[#161412] px-4 py-2 border-t border-[#26221e] flex items-center justify-between text-xs text-[#827a70]">
        <div class="flex items-center gap-2">
          <span>Синхронизация:</span>
          <span class="text-emerald-400">● GitHub</span>
          <span class="text-yellow-400">● Hugging Face</span>
          <span class="text-emerald-400">● Supabase</span>
        </div>
        <button onclick="toggleModal('historyModal')" class="text-[#d97757] hover:underline text-[11px]">
          📂 История проектов (<span id="historyCount">...</span>)
        </button>
      </div>

    </section>

  </main>

  <!-- SETTINGS MODAL -->
  <div id="settingsModal" class="hidden fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="claude-card max-w-md w-full rounded-2xl p-5 space-y-4 shadow-2xl">
      <div class="flex items-center justify-between border-b border-[#2e2824] pb-3">
        <h3 class="font-bold text-sm text-[#f5f2eb] flex items-center gap-2">
          <span>⚙️</span> Настройки Claude Code Studio
        </h3>
        <button onclick="toggleModal('settingsModal')" class="text-[#888] hover:text-white">&times;</button>
      </div>

      <div class="space-y-3 text-xs">
        <div>
          <label class="block text-[#a69e92] mb-1">Мастер-ключ авторизации / Пароль</label>
          <input id="settingAuthKey" type="password" placeholder="sk-fcc-... или пароль"
            class="w-full bg-[#100f0e] border border-[#2d2824] rounded-lg p-2.5 text-white focus:outline-none focus:border-[#d97757]">
        </div>

        <div>
          <label class="block text-[#a69e92] mb-1">Модель интеллекта</label>
          <select id="settingModelSelect"
            class="w-full bg-[#100f0e] border border-[#2d2824] rounded-lg p-2.5 text-white focus:outline-none focus:border-[#d97757]">
            <option value="claude-fable-5.5" selected>⚡ Mistral Codestral (Кодовый эксперт, 0.5с) [Рекомендуется]</option>
            <option value="groq/qwen/qwen3.8-27b">🚀 Groq Qwen 3.8 (Ультра-скорость 35мс)</option>
            <option value="groq/openai/gpt-oss-120b">🧠 Groq GPT-OSS 120B (Глубокие рассуждения)</option>
            <option value="gemini/gemini-3.5-flash">🌟 Google Gemini 3.5 Flash (Широкий контекст)</option>
            <option value="open_router/nvidia/nemotron-3.5-lightning:free">🛡️ NVIDIA Nemotron 3.5 (Резерв)</option>
          </select>
        </div>

        <div class="p-3 rounded-xl bg-[#131110] border border-[#292420] space-y-2.5">
          <label class="flex items-center gap-2 cursor-pointer">
            <input id="settingUploadHf" type="checkbox" checked class="rounded text-[#d97757]">
            <span class="text-[#c7bfb3] font-medium">🤗 Песочница Hugging Face (50 GB Workspace)</span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer">
            <input id="settingPushGithub" type="checkbox" class="rounded text-[#d97757]">
            <span class="text-[#c7bfb3]">🚀 Создавать репозиторий в GitHub сразу</span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer">
            <input id="settingGhPrivate" type="checkbox" class="rounded text-[#d97757]">
            <span class="text-[#888]">🔒 Приватный репозиторий на GitHub</span>
          </label>
          <p class="text-[11px] text-[#8c857b] pt-1 border-t border-[#221e1a]">
            💡 По умолчанию разработка ведется в облачной песочнице Hugging Face без засорения GitHub. Вы можете опубликовать проект на GitHub в 1 клик прямо из панели кода, когда он будет готов!
          </p>
        </div>
      </div>

      <div class="pt-2 flex justify-end gap-2">
        <button onclick="saveSettings()" class="terracotta-btn px-4 py-2 rounded-lg font-bold text-xs transition">
          Сохранить
        </button>
      </div>
    </div>
  </div>

  <!-- HISTORY MODAL -->
  <div id="historyModal" class="hidden fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="claude-card max-w-lg w-full rounded-2xl p-5 space-y-4 shadow-2xl">
      <div class="flex items-center justify-between border-b border-[#2e2824] pb-3">
        <h3 class="font-bold text-sm text-[#f5f2eb] flex items-center gap-2">
          <span>📂</span> Сохранённые проекты в облаке
        </h3>
        <button onclick="toggleModal('historyModal')" class="text-[#888] hover:text-white">&times;</button>
      </div>

      <div id="historyList" class="space-y-2 max-h-80 overflow-y-auto custom-scrollbar text-xs">
        <div class="text-[#666]">Загрузка списка...</div>
      </div>
    </div>
  </div>

  <script>
    // PWA Service Worker Registration
    if ('serviceWorker' in navigator) {{
      navigator.serviceWorker.register('/coder/sw.js')
        .then(() => console.log('Claude Code PWA SW registered'))
        .catch(err => console.error('SW Error:', err));
    }}

    // PWA Install Prompt Listener
    let deferredPrompt = null;
    window.addEventListener('beforeinstallprompt', (e) => {{
      e.preventDefault();
      deferredPrompt = e;
      const installBtn = document.getElementById('pwaInstallBtn');
      if (installBtn) installBtn.classList.remove('hidden');
    }});

    function installPWA() {{
      if (deferredPrompt) {{
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then((choiceResult) => {{
          if (choiceResult.outcome === 'accepted') {{
            console.log('PWA installed');
          }}
          deferredPrompt = null;
          document.getElementById('pwaInstallBtn').classList.add('hidden');
        }});
      }} else {{
        alert('Для установки приложения:\\n- На Android/Chrome: нажмите меню ➔ «Установить приложение»\\n- На iPhone/Safari: нажмите «Поделиться» ➔ «На экран Домой»');
      }}
    }}

    // State Variables
    let currentFiles = {{}};
    let activeFileKey = '';
    let timerInterval = null;

    // Load saved settings
    const savedKey = localStorage.getItem('claude_auth_key') || 'sk-fcc-master-key-2026';
    document.getElementById('settingAuthKey').value = savedKey;

    function toggleModal(id) {{
      const el = document.getElementById(id);
      el.classList.toggle('hidden');
      if (id === 'historyModal' && !el.classList.contains('hidden')) {{
        loadHistory();
      }}
    }}

    function saveSettings() {{
      const key = document.getElementById('settingAuthKey').value.trim();
      const model = document.getElementById('settingModelSelect').value;
      const pushGh = document.getElementById('settingPushGithub').checked;
      const ghPriv = document.getElementById('settingGhPrivate').checked;
      const uploadHf = document.getElementById('settingUploadHf').checked;

      if (key) localStorage.setItem('claude_auth_key', key);
      localStorage.setItem('claude_selected_model', model);
      localStorage.setItem('claude_push_github', pushGh ? 'true' : 'false');
      localStorage.setItem('claude_gh_private', ghPriv ? 'true' : 'false');
      localStorage.setItem('claude_upload_hf', uploadHf ? 'true' : 'false');

      document.getElementById('selectedModelLabel').textContent = model;
      toggleModal('settingsModal');
    }}

    function initStoredSettings() {{
      const storedKey = localStorage.getItem('claude_auth_key');
      if (storedKey) document.getElementById('settingAuthKey').value = storedKey;

      const storedModel = localStorage.getItem('claude_selected_model');
      if (storedModel && document.getElementById('settingModelSelect')) {{
        document.getElementById('settingModelSelect').value = storedModel;
        document.getElementById('selectedModelLabel').textContent = storedModel;
      }}

      const storedPushGh = localStorage.getItem('claude_push_github');
      if (storedPushGh !== null && document.getElementById('settingPushGithub')) {{
        document.getElementById('settingPushGithub').checked = storedPushGh === 'true';
      }}

      const storedGhPriv = localStorage.getItem('claude_gh_private');
      if (storedGhPriv !== null && document.getElementById('settingGhPrivate')) {{
        document.getElementById('settingGhPrivate').checked = storedGhPriv === 'true';
      }}

      const storedHf = localStorage.getItem('claude_upload_hf');
      if (storedHf !== null && document.getElementById('settingUploadHf')) {{
        document.getElementById('settingUploadHf').checked = storedHf === 'true';
      }}
    }}

    function switchMobileView(view) {{
      const termSection = document.getElementById('terminalSection');
      const artSection = document.getElementById('artifactsSection');
      const tabTerm = document.getElementById('mobileTabTerminal');
      const tabArt = document.getElementById('mobileTabArtifacts');

      if (view === 'terminal') {{
        termSection.classList.remove('hidden');
        artSection.classList.add('hidden');
        tabTerm.className = "flex-1 py-2 text-xs font-semibold text-[#d97757] border-b-2 border-[#d97757] flex items-center justify-center gap-1.5 transition";
        tabArt.className = "flex-1 py-2 text-xs font-semibold text-[#8a8277] border-b-2 border-transparent flex items-center justify-center gap-1.5 transition";
      }} else {{
        termSection.classList.add('hidden');
        artSection.classList.remove('hidden');
        tabArt.className = "flex-1 py-2 text-xs font-semibold text-[#d97757] border-b-2 border-[#d97757] flex items-center justify-center gap-1.5 transition";
        tabTerm.className = "flex-1 py-2 text-xs font-semibold text-[#8a8277] border-b-2 border-transparent flex items-center justify-center gap-1.5 transition";
      }}
    }}

    function setPrompt(promptText, projSlug) {{
      document.getElementById('promptInput').value = promptText;
      if (projSlug) document.getElementById('projNameInput').value = projSlug;
    }}

    function toggleThinking() {{
      const content = document.getElementById('thinkingContent');
      const chevron = document.getElementById('thinkingChevron');
      content.classList.toggle('hidden');
      chevron.textContent = content.classList.contains('hidden') ? '▼' : '▲';
    }}

    function addStep(icon, text, type = 'info') {{
      const steps = document.getElementById('executionSteps');
      const termBody = document.getElementById('terminalBody');

      if (type === 'chat') {{
        const card = document.createElement('div');
        card.className = "p-3.5 rounded-xl bg-[#181614] border border-[#2e2824] text-xs text-[#e8e2d8] leading-relaxed shadow-sm select-text my-2";
        card.innerHTML = `<div class="text-[10px] uppercase font-mono text-[#d97757] font-bold mb-1.5 flex items-center gap-1.5"><span>✳</span> Claude Code</div><div class="whitespace-pre-wrap">${{text}}</div>`;
        steps.appendChild(card);
        termBody.scrollTop = termBody.scrollHeight;
        return;
      }}

      const div = document.createElement('div');
      let color = 'text-[#c7bfb3]';
      if (type === 'success') color = 'text-emerald-400 font-medium';
      if (type === 'error') color = 'text-rose-400 font-medium';
      if (type === 'highlight') color = 'text-[#e68565] font-semibold';
      div.className = `flex items-start gap-2 text-xs ${{color}}`;
      div.innerHTML = `<span class="flex-shrink-0 mt-0.5">${{icon}}</span><span class="flex-1">${{text}}</span>`;
      steps.appendChild(div);
      
      termBody.scrollTop = termBody.scrollHeight;
    }}

    let currentProjectSlug = '';

    async function publishCurrentProjectToGithub() {{
      const projName = currentProjectSlug || document.getElementById('projNameInput').value.trim() || 'project';
      const btn = document.getElementById('btnPublishGithub');
      const oldHtml = btn.innerHTML;
      btn.innerHTML = '<span>⏳</span> <span>Публикация...</span>';
      btn.disabled = true;

      try {{
        const authKey = localStorage.getItem('claude_auth_key') || 'sk-fcc-master-key-2026';
        const isPrivate = document.getElementById('settingGhPrivate') ? document.getElementById('settingGhPrivate').checked : false;
        const res = await fetch('/coder/api/publish-github', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            project_name: projName,
            auth_key: authKey,
            is_private: isPrivate
          }})
        }});
        const data = await res.json();
        if (res.ok && data.github_url) {{
          addStep('🚀', `Опубликовано на GitHub: <a href="${{data.github_url}}" target="_blank" class="underline text-emerald-400 font-bold">${{data.github_url}}</a>`, 'success');
          const ghLink = document.getElementById('ghRepoLink');
          if (ghLink) {{
            ghLink.href = data.github_url;
            ghLink.classList.remove('hidden');
          }}
          btn.classList.add('hidden');
        }} else {{
          addStep('❌', 'Ошибка публикации в GitHub: ' + (data.detail || JSON.stringify(data)), 'error');
          btn.innerHTML = oldHtml;
          btn.disabled = false;
        }}
      }} catch (err) {{
        addStep('❌', 'Сетевая ошибка публикации: ' + err.message, 'error');
        btn.innerHTML = oldHtml;
        btn.disabled = false;
      }}
    }}

    async function runAgent() {{
      const prompt = document.getElementById('promptInput').value.trim();
      const projName = document.getElementById('projNameInput').value.trim() || 'project';
      currentProjectSlug = projName;
      const authKey = localStorage.getItem('claude_auth_key') || 'sk-fcc-master-key-2026';
      const model = document.getElementById('settingModelSelect').value;
      const pushGithub = document.getElementById('settingPushGithub').checked;
      const ghPrivate = document.getElementById('settingGhPrivate').checked;
      const uploadHf = document.getElementById('settingUploadHf').checked;

      if (!prompt) {{
        alert('Пожалуйста, введите задачу или вопрос для агента!');
        return;
      }}

      // Handle Slash Commands
      if (prompt.startsWith('/')) {{
        if (prompt === '/help') {{
          addStep('ℹ️', 'Команды: /help (помощь), /clear (очистить экран), /model (выбрать модель), /git (настройки)', 'highlight');
          document.getElementById('promptInput').value = '';
          return;
        }}
        if (prompt === '/clear') {{
          document.getElementById('executionSteps').innerHTML = '';
          document.getElementById('thinkingBlock').classList.add('hidden');
          document.getElementById('promptInput').value = '';
          return;
        }}
        if (prompt === '/model') {{
          toggleModal('settingsModal');
          document.getElementById('promptInput').value = '';
          return;
        }}
      }}

      const submitBtn = document.getElementById('submitBtn');
      const timerDisplay = document.getElementById('timerDisplay');
      const termStatusPill = document.getElementById('termStatusPill');
      const thinkingBlock = document.getElementById('thinkingBlock');
      const thinkingContent = document.getElementById('thinkingContent');
      const thinkingTitle = document.getElementById('thinkingTitle');

      submitBtn.disabled = true;
      termStatusPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#d97757] animate-ping"></span> running';
      
      addStep('⚡', `Запрос: "${{prompt.slice(0, 80)}}${{prompt.length > 80 ? '...' : ''}}"`, 'highlight');
      thinkingBlock.classList.remove('hidden');
      thinkingTitle.textContent = 'Рассуждения модели (Thinking)...';
      thinkingContent.textContent = 'Обработка запроса моделью ' + model + '...';

      let seconds = 0;
      clearInterval(timerInterval);
      timerInterval = setInterval(() => {{
        seconds++;
        timerDisplay.textContent = seconds + 's';
      }}, 1000);

      try {{
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 95000); // 95s guard for Cloudflare 100s

        const res = await fetch('/coder/api/generate', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          signal: controller.signal,
          body: JSON.stringify({{
            project_name: projName,
            prompt: prompt,
            model: model,
            auth_key: authKey,
            push_github: pushGithub,
            github_private: ghPrivate,
            upload_hf: uploadHf
          }})
        }});

        clearTimeout(timeoutId);
        clearInterval(timerInterval);
        const data = await res.json();

        if (!res.ok) {{
          addStep('❌', 'Ошибка: ' + (data.detail || JSON.stringify(data)), 'error');
          termStatusPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span> error';
          return;
        }}

        // Mode 1: Conversational Chat
        if (data.status === 'chat') {{
          thinkingBlock.classList.add('hidden');
          addStep('💬', data.message, 'chat');
          termStatusPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> ready';
          document.getElementById('promptInput').value = '';
          return;
        }}

        // Mode 2: Project Generated
        thinkingTitle.textContent = '✓ Рассуждения завершены';
        thinkingContent.textContent += '\\n\\nСгенерированы файлы:\\n' + (data.files || []).map(f => '• ' + f).join('\\n');

        addStep('✓', `Успешно сгенерировано файлов: ${{data.files.length}}`, 'success');
        if (data.git_commit && data.git_commit !== 'no-git') {{
          addStep('📦', `Git: ${{data.git_commit}}`, 'info');
        }}

        if (data.hf_project_url) {{
          addStep('🤗', `Hugging Face Workspace: <a href="${{data.hf_project_url}}" target="_blank" class="underline text-yellow-400 font-bold">Открыть файлы песочницы</a>`, 'success');
          const hfProj = document.getElementById('hfProjectLink');
          if (hfProj) {{
            hfProj.href = data.hf_project_url;
            hfProj.classList.remove('hidden');
          }}
        }}

        if (data.hf_url) {{
          addStep('📥', `Архив проекта: <a href="${{data.hf_url}}" target="_blank" class="underline text-yellow-300 font-bold">Скачать ZIP</a>`, 'info');
          const hfLink = document.getElementById('hfDownloadLink');
          if (hfLink) {{
            hfLink.href = data.hf_url;
            hfLink.classList.remove('hidden');
          }}
        }}

        if (data.github_url) {{
          addStep('🐙', `GitHub репозиторий: <a href="${{data.github_url}}" target="_blank" class="underline text-violet-400 font-bold">${{data.github_url}}</a>`, 'success');
          const ghLink = document.getElementById('ghRepoLink');
          if (ghLink) {{
            ghLink.href = data.github_url;
            ghLink.classList.remove('hidden');
          }}
          const pubBtn = document.getElementById('btnPublishGithub');
          if (pubBtn) pubBtn.classList.add('hidden');
        }} else {{
          const pubBtn = document.getElementById('btnPublishGithub');
          if (pubBtn) pubBtn.classList.remove('hidden');
          addStep('✨', 'Проект сохранен в песочнице Hugging Face! Нажмите <b>«🚀 В GitHub»</b> в панели кода, чтобы опубликовать релиз.', 'highlight');
        }}

        if (data.download_url) {{
          const sbLink = document.getElementById('sbDownloadLink');
          if (sbLink) {{
            sbLink.href = data.download_url;
            sbLink.classList.remove('hidden');
          }}
        }}

        // Render Artifacts Tabs
        if (data.file_contents && Object.keys(data.file_contents).length > 0) {{
          renderArtifacts(data.file_contents, projName);
        }}

        termStatusPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> completed';
        document.getElementById('promptInput').value = '';
        loadHistoryCount();

      }} catch (err) {{
        clearInterval(timerInterval);
        if (err.name === 'AbortError') {{
          addStep('⚠️', 'Запрос занял слишком много времени (>90с). Попробуйте сформулировать задачу более точечно.', 'error');
        }} else {{
          addStep('❌', 'Сетевая ошибка: ' + err.message + '. Проверьте стабильность соединения.', 'error');
        }}
        termStatusPill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span> error';
      }} finally {{
        submitBtn.disabled = false;
      }}
    }}

    function renderArtifacts(filesDict, projName) {{
      currentFiles = filesDict;
      const tabsContainer = document.getElementById('fileTabsContainer');
      const badge = document.getElementById('filesCountBadge');
      const fileBadge = document.getElementById('fileBadge');
      tabsContainer.innerHTML = '';

      const fileNames = Object.keys(filesDict);
      if (fileNames.length === 0) return;

      badge.textContent = fileNames.length;
      badge.classList.remove('hidden');
      fileBadge.textContent = fileNames.length + ' files';
      fileBadge.classList.remove('hidden');

      fileNames.forEach((fname, index) => {{
        const btn = document.createElement('button');
        btn.className = `px-2.5 py-1 rounded-md text-xs font-mono whitespace-nowrap flex items-center gap-1.5 transition ${{index === 0 ? 'bg-[#d97757]/20 text-[#e68565] border border-[#d97757]/40' : 'bg-[#1b1917] hover:bg-[#25221f] text-[#a19a90] border border-[#2b2723]'}}`;
        btn.innerHTML = `<span>${{getFileIcon(fname)}}</span><span>${{fname}}</span>`;
        btn.onclick = () => selectFile(fname, btn);
        tabsContainer.appendChild(btn);
      }});

      selectFile(fileNames[0], tabsContainer.children[0]);

      // If mobile, switch to artifacts tab automatically
      if (window.innerWidth < 1024) {{
        switchMobileView('artifacts');
      }}
    }}

    function getFileIcon(filename) {{
      if (filename.endsWith('.py')) return '🐍';
      if (filename.endsWith('.js') || filename.endsWith('.ts')) return '📜';
      if (filename.endsWith('.json')) return '🔧';
      if (filename.endsWith('.md')) return '📝';
      if (filename.endsWith('.yml') || filename.endsWith('.yaml')) return '⚙️';
      if (filename.includes('Docker')) return '🐳';
      return '📄';
    }}

    function selectFile(filename, btnEl) {{
      activeFileKey = filename;
      document.getElementById('activeFileName').textContent = filename;

      // Update Tab Styles
      const tabs = document.getElementById('fileTabsContainer').children;
      for (let t of tabs) {{
        t.className = "px-2.5 py-1 rounded-md text-xs font-mono whitespace-nowrap flex items-center gap-1.5 bg-[#1b1917] hover:bg-[#25221f] text-[#a19a90] border border-[#2b2723] transition";
      }}
      if (btnEl) {{
        btnEl.className = "px-2.5 py-1 rounded-md text-xs font-mono whitespace-nowrap flex items-center gap-1.5 bg-[#d97757]/20 text-[#e68565] border border-[#d97757]/40 transition";
      }}

      // Syntax Highlight
      const codeDisplay = document.getElementById('codeDisplay');
      const code = currentFiles[filename] || '';
      
      let lang = 'python';
      if (filename.endsWith('.js')) lang = 'javascript';
      if (filename.endsWith('.json')) lang = 'json';
      if (filename.endsWith('.md')) lang = 'markdown';
      if (filename.endsWith('.sh')) lang = 'bash';
      if (filename.includes('Docker')) lang = 'docker';

      codeDisplay.innerHTML = `<code class="language-${{lang}}">${{escapeHtml(code)}}</code>`;
      if (window.Prism) Prism.highlightAll();
    }}

    function escapeHtml(text) {{
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}

    function copyCurrentFile() {{
      const code = currentFiles[activeFileKey] || '';
      if (!code) return;
      navigator.clipboard.writeText(code).then(() => {{
        const btn = document.getElementById('copyBtn');
        btn.innerHTML = '<span>✓</span> <span class="hidden sm:inline">Скопировано!</span>';
        setTimeout(() => {{
          btn.innerHTML = '<span>📋</span> <span class="hidden sm:inline">Копировать</span>';
        }}, 2000);
      }});
    }}

    async function loadHistory() {{
      const listEl = document.getElementById('historyList');
      try {{
        const res = await fetch('/coder/api/list');
        const data = await res.json();
        if (!data || data.length === 0) {{
          listEl.innerHTML = '<div class="text-[#888]">В хранилище пока нет проектов.</div>';
          return;
        }}
        listEl.innerHTML = data.map(item => `
          <div class="flex items-center justify-between p-2.5 rounded-xl bg-[#141210] border border-[#2b2622]">
            <div>
              <div class="font-medium text-[#f5f2eb]">${{item.name}}</div>
              <div class="text-[10px] text-[#736c63]">${{item.created_at || ''}} • ${{item.size_kb}} KB</div>
            </div>
            <a href="${{item.url}}" target="_blank"
              class="px-2.5 py-1 rounded bg-[#2e2722] text-[#d97757] hover:bg-[#3d332c] text-xs font-semibold transition">
              Скачать
            </a>
          </div>
        `).join('');
      }} catch (e) {{
        listEl.innerHTML = '<div class="text-rose-400">Ошибка загрузки истории</div>';
      }}
    }}

    async function loadHistoryCount() {{
      try {{
        const res = await fetch('/coder/api/list');
        const data = await res.json();
        document.getElementById('historyCount').textContent = (data || []).length;
      }} catch (e) {{}}
    }}

    initStoredSettings();
    loadHistoryCount();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.post("/api/generate")
async def generate_project(task: CoderTaskRequest, request: Request):
    """Run autonomous project generation or instant conversational reply with parallel multi-cloud uploads."""
    _verify_auth(task.auth_key, request)

    project_slug = _sanitize_project_name(task.project_name)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_dir = Path("/tmp") / f"coder_{project_slug}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Ask local LLM proxy (Claude Fable 5.5)
        proxy_url = f"http://127.0.0.1:{os.getenv('PORT', '8082')}/v1/messages"
        token_to_use = _get_server_token(request)
        headers = {
            "x-api-key": token_to_use,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system_instruction = (
            "You are Claude Code, an autonomous expert full-stack AI software engineer by Anthropic. "
            "You operate in two modes:\n"
            "1. CONVERSATIONAL MODE: If the user is asking a question, greeting you ('привет', 'hello', 'что ты умеешь', etc.), "
            "or discussing a topic without asking to generate an application, reply conversationally and helpful directly in Markdown.\n"
            "2. PROJECT GENERATION MODE: If the user is asking to build, write, create, or generate a software project, program, bot, script, or application:\n"
            "- First give a brief architecture overview.\n"
            "- Output EVERY SINGLE project file inside a fenced code block with the relative filename in the header comment, e.g.:\n"
            "```python\n# file: main.py\n<complete code>\n```\n"
            "- Always include README.md and requirements.txt (or package.json if JS/TS).\n"
            "- Write complete, production-ready code with no placeholders or TODOs."
        )

        user_message = (
            f"Project Name: {project_slug}\n"
            f"User Request:\n{task.prompt}"
        )

        payload = {
            "model": task.model,
            "max_tokens": 6144,
            "system": system_instruction,
            "messages": [{"role": "user", "content": user_message}],
        }

        async with httpx.AsyncClient(timeout=80.0) as client:
            resp = await client.post(proxy_url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Ошибка генерации от прокси ({resp.status_code}): {resp.text}",
                )
            result = resp.json()

        # 2. Extract generated text
        content_blocks = result.get("content", [])
        full_text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        if not full_text:
            raise HTTPException(status_code=500, detail="Модель вернула пустой ответ")

        # 3. Check if files were generated
        extracted_files = _extract_files_from_markdown(full_text)
        
        # If no project code files were generated, it's a conversational answer!
        if not extracted_files:
            return JSONResponse(
                content={
                    "status": "chat",
                    "message": full_text,
                }
            )

        # 4. Save files to temporary directory
        created_files = []
        for rel_path, code in extracted_files.items():
            dest = run_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            created_files.append(rel_path)

        # 5. Initialize Git repo and make initial commit
        git_commit_hash = "no-git"
        try:
            subprocess.run(["git", "init"], cwd=run_dir, check=True, capture_output=True, timeout=5)
            subprocess.run(["git", "config", "user.name", "Claude Code Agent"], cwd=run_dir, check=True, capture_output=True, timeout=5)
            subprocess.run(["git", "config", "user.email", "claude-code@agent.local"], cwd=run_dir, check=True, capture_output=True, timeout=5)
            subprocess.run(["git", "add", "."], cwd=run_dir, check=True, capture_output=True, timeout=5)
            res = subprocess.run(["git", "commit", "-m", f"Initial commit: {project_slug} via {task.model}"], cwd=run_dir, check=True, capture_output=True, text=True, timeout=5)
            git_commit_hash = res.stdout.strip().split("\n")[0]
        except Exception:
            pass

        # 6. Create ZIP archive
        zip_filename = f"{project_slug}_{timestamp}.zip"
        zip_path = Path("/tmp") / zip_filename
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in run_dir.rglob("*"):
                if ".git" in file_path.parts:
                    continue
                if file_path.is_file():
                    arcname = file_path.relative_to(run_dir)
                    zip_file.write(file_path, arcname)

        zip_data = zip_path.read_bytes()

        # Cache project locally in /tmp/coder_cache/{project_slug} for instant 1-click GitHub publish
        cache_dir = Path("/tmp/coder_cache") / project_slug
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(run_dir, cache_dir, dirs_exist_ok=True)
        except Exception:
            pass

        # 7. PARALLEL CLOUD UPLOADS (GitHub + Hugging Face Workspace + Supabase)
        upload_tasks = []

        # Task A: GitHub push (if explicitly requested)
        if task.push_github:
            upload_tasks.append(
                _create_and_push_github(
                    run_dir,
                    project_name=project_slug,
                    is_private=task.github_private,
                    description=task.prompt[:200],
                )
            )
        else:
            upload_tasks.append(asyncio.sleep(0, result=None))

        # Task B: Hugging Face upload (Workspace folder + Archive ZIP)
        if task.upload_hf:
            upload_tasks.append(_upload_to_huggingface(run_dir, zip_data, zip_filename, project_slug))
        else:
            upload_tasks.append(asyncio.sleep(0, result={"hf_project_url": None, "hf_zip_url": None}))

        # Task C: Supabase Storage backup
        upload_tasks.append(_upload_to_supabase(zip_filename, zip_data, content_type="application/zip"))

        # Run all 3 concurrently in parallel
        github_url, hf_res, download_url = await asyncio.gather(*upload_tasks)
        hf_project_url = hf_res.get("hf_project_url") if isinstance(hf_res, dict) else None
        hf_zip_url = hf_res.get("hf_zip_url") if isinstance(hf_res, dict) else None

        # Cleanup zip file
        if zip_path.exists():
            zip_path.unlink()

        return JSONResponse(
            content={
                "status": "project",
                "project_name": project_slug,
                "zip_filename": zip_filename,
                "github_url": github_url,
                "hf_project_url": hf_project_url,
                "hf_url": hf_zip_url,
                "download_url": download_url,
                "files": created_files,
                "file_contents": extracted_files,
                "git_commit": git_commit_hash,
            }
        )

    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@router.post("/api/publish-github")
async def publish_to_github(req: PublishGithubRequest, request: Request):
    """Publish an existing generated project from local cache or Hugging Face to GitHub."""
    _verify_auth(req.auth_key, request)
    project_slug = _sanitize_project_name(req.project_name)
    cache_dir = Path("/tmp/coder_cache") / project_slug

    if not cache_dir.exists() or not any(cache_dir.iterdir()):
        matching = sorted(Path("/tmp").glob(f"coder_{project_slug}_*"), reverse=True)
        if matching:
            cache_dir = matching[0]
        else:
            raise HTTPException(status_code=404, detail="Файлы проекта не найдены для публикации в GitHub")

    # Initialize git if needed
    try:
        subprocess.run(["git", "init"], cwd=cache_dir, check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.name", "Claude Code Agent"], cwd=cache_dir, check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "claude-code@agent.local"], cwd=cache_dir, check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "add", "."], cwd=cache_dir, check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "commit", "-m", f"Release v1.0: {project_slug}"], cwd=cache_dir, check=True, capture_output=True, timeout=5)
    except Exception:
        pass

    github_url = await _create_and_push_github(
        cache_dir, project_name=project_slug, is_private=req.is_private, description=f"Production release of {project_slug} via Claude Code Studio"
    )
    if not github_url:
        raise HTTPException(status_code=500, detail="Ошибка публикации в GitHub (проверьте права токена)")

    return JSONResponse(content={"status": "published", "github_url": github_url, "project_name": project_slug})


@router.get("/api/list")
async def list_supabase_projects():
    """List project zip archives from storage."""
    url_base, key, bucket = _supabase_config()
    if not url_base or not key:
        return JSONResponse(content=[], status_code=200)

    url = f"{url_base}/storage/v1/object/list/{bucket}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "prefix": "",
        "limit": 30,
        "offset": 0,
        "sortBy": {"column": "created_at", "order": "desc"},
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                return JSONResponse(content=[], status_code=200)
            items = resp.json()

        results = []
        for item in items:
            name = item.get("name", "")
            if name.endswith(".zip"):
                size_bytes = item.get("metadata", {}).get("size", 0)
                size_kb = round(size_bytes / 1024, 1)
                created_at = item.get("created_at", "")[:19].replace("T", " ")
                public_url = f"{url_base}/storage/v1/object/public/{bucket}/{name}"
                results.append({
                    "name": name,
                    "size_kb": size_kb,
                    "created_at": created_at,
                    "url": public_url,
                })

        return JSONResponse(content=results)
    except Exception:
        return JSONResponse(content=[], status_code=200)
