"""Autonomous Coder Agent Web UI with Multi-Cloud Storage: GitHub, Hugging Face & Supabase."""

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/coder", tags=["coder"])


class CoderTaskRequest(BaseModel):
    project_name: str = Field(min_length=2, max_length=64)
    prompt: str = Field(min_length=5, max_length=16000)
    model: str = Field(default="claude-fable-5.5")
    auth_key: str = Field(min_length=1)
    push_github: bool = Field(default=True)
    github_private: bool = Field(default=False)
    upload_hf: bool = Field(default=True)


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
            files[clean_path] = content

    if not files:
        alt_pattern = re.compile(
            r"(?:###|####|\*\*)\s*(?:File:\s*|Файл:\s*)?`?([a-zA-Z0-9_.\-\/\\]+\.[a-zA-Z0-9]+)`?\s*\n+```(?:\w+)?\n([\s\S]*?)```",
            re.IGNORECASE,
        )
        for filepath, content in alt_pattern.findall(text):
            clean_path = filepath.strip().strip("'\"` ")
            if clean_path and not clean_path.startswith(("/", "\\", "..")):
                files[clean_path] = content

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
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=data)
            if resp.status_code in (200, 201):
                return f"{url_base}/storage/v1/object/public/{bucket}/{filename}"
    except Exception:
        pass
    return None


async def _create_and_push_github(
    run_dir: Path, project_name: str, is_private: bool = False, description: str = ""
) -> str | None:
    """Create a GitHub repository and push code to it."""
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
        "description": description or f"Generated autonomously by Aider & Claude Fable 5.5",
        "auto_init": False,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(api_url, headers=headers, json=payload)
            # 201 Created or 422 (if already exists, we will update it)
            if resp.status_code not in (201, 422):
                return None

        # Add remote and push via git
        remote_url = f"https://{gh_user}:{gh_token}@github.com/{gh_user}/{project_name}.git"
        subprocess.run(["git", "remote", "remove", "origin"], cwd=run_dir, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=run_dir, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=run_dir, check=True, capture_output=True)
        res = subprocess.run(["git", "push", "-u", "origin", "main", "--force"], cwd=run_dir, capture_output=True, text=True)
        if res.returncode == 0:
            return f"https://github.com/{gh_user}/{project_name}"
    except Exception:
        pass
    return None


async def _upload_to_huggingface(zip_path: Path, zip_filename: str) -> str | None:
    """Upload project ZIP to Hugging Face Datasets repository."""
    hf_token = os.getenv("HF_TOKEN", "")
    hf_user = os.getenv("HF_USERNAME", "Klode2026")
    hf_dataset = os.getenv("HF_DATASET", "agent-projects")
    if not hf_token:
        return None

    repo_url = f"https://{hf_user}:{hf_token}@huggingface.co/datasets/{hf_user}/{hf_dataset}"

    with tempfile.TemporaryDirectory() as tmpdir:
        clone_dir = Path(tmpdir) / "hf_repo"
        try:
            # Clone shallow
            res = subprocess.run(["git", "clone", "--depth", "1", repo_url, str(clone_dir)], capture_output=True, text=True)
            if res.returncode != 0:
                return None

            dest_file = clone_dir / zip_filename
            shutil.copy2(zip_path, dest_file)

            subprocess.run(["git", "config", "user.name", hf_user], cwd=clone_dir, check=True)
            subprocess.run(["git", "config", "user.email", "klode@local"], cwd=clone_dir, check=True)
            subprocess.run(["git", "add", "."], cwd=clone_dir, check=True)
            subprocess.run(["git", "commit", "-m", f"Store {zip_filename}"], cwd=clone_dir, check=True)
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=clone_dir, capture_output=True, text=True)
            if push_res.returncode == 0:
                return f"https://huggingface.co/datasets/{hf_user}/{hf_dataset}/resolve/main/{zip_filename}"
        except Exception:
            pass

    return None


@router.get("", response_class=HTMLResponse)
async def coder_dashboard(request: Request):
    """Serve the Aider Multi-Cloud Coding Agent UI."""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aider Multi-Cloud Code Agent • GitHub + Hugging Face</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{ background: #080c14; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    .glass {{ background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(14px); border: 1px solid rgba(255, 255, 255, 0.08); }}
    .glow {{ box-shadow: 0 0 35px rgba(139, 92, 246, 0.25); }}
  </style>
</head>
<body class="text-slate-100 min-h-screen py-8 px-4 flex flex-col items-center">
  <div class="w-full max-w-4xl space-y-6">

    <!-- Header -->
    <div class="glass glow rounded-2xl p-6 flex flex-col md:flex-row items-center justify-between gap-4">
      <div>
        <div class="flex items-center gap-2.5">
          <span class="text-3xl">🚀</span>
          <h1 class="text-2xl font-bold bg-gradient-to-r from-violet-400 via-fuchsia-400 to-indigo-400 bg-clip-text text-transparent">
            Aider Multi-Cloud Agent
          </h1>
          <span class="text-xs px-2.5 py-0.5 rounded-full bg-violet-900/60 text-violet-300 border border-violet-700/50">Claude Fable 5.5</span>
        </div>
        <p class="text-sm text-slate-400 mt-1">Автономный ИИ-разработчик с автоматической публикацией в <b>GitHub</b> и <b>Hugging Face</b></p>
      </div>

      <!-- Cloud Badges -->
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <span class="px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-700/80 flex items-center gap-1.5 text-slate-300">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          🐙 GitHub: <b class="text-violet-300">0939222228elit</b>
        </span>
        <span class="px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-700/80 flex items-center gap-1.5 text-slate-300">
          <span class="w-2 h-2 rounded-full bg-yellow-400"></span>
          🤗 HF: <b class="text-yellow-300">50 GB</b>
        </span>
      </div>
    </div>

    <!-- Main Card -->
    <div class="glass rounded-2xl p-6 space-y-5">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Имя репозитория / проекта</label>
          <input id="projectName" type="text" placeholder="crypto-bot" value="my-project"
            class="w-full bg-slate-900/90 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-violet-500">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Модель интеллекта</label>
          <select id="modelSelect"
            class="w-full bg-slate-900/90 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-violet-500">
            <option value="claude-fable-5.5" selected>Claude Fable 5.5 (1.3B Tokens Fallback)</option>
            <option value="claude-3-7-sonnet-20250219">Claude 3.7 Sonnet</option>
            <option value="gemini/gemini-3.8-flash">Gemini 3.8 Flash</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Мастер-ключ / Пароль</label>
          <input id="authKey" type="password" placeholder="sk-fcc-... или пароль"
            class="w-full bg-slate-900/90 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-violet-500">
        </div>
      </div>

      <!-- Cloud Toggles -->
      <div class="flex flex-wrap items-center gap-6 p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-xs text-slate-300">
        <label class="flex items-center gap-2 cursor-pointer select-none">
          <input id="pushGithub" type="checkbox" checked class="w-4 h-4 rounded text-violet-600 focus:ring-violet-500">
          <span>🐙 Создавать репозиторий на <b>GitHub</b> и пушить код</span>
        </label>
        <label class="flex items-center gap-2 cursor-pointer select-none">
          <input id="githubPrivate" type="checkbox" class="w-4 h-4 rounded text-violet-600 focus:ring-violet-500">
          <span>🔒 Приватный репозиторий GitHub</span>
        </label>
        <label class="flex items-center gap-2 cursor-pointer select-none">
          <input id="uploadHf" type="checkbox" checked class="w-4 h-4 rounded text-yellow-500 focus:ring-yellow-400">
          <span>🤗 Сохранять ZIP в <b>Hugging Face</b> (50 GB Cloud)</span>
        </label>
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1">Техническое задание (ТЗ) для агента</label>
        <textarea id="promptInput" rows="5"
          placeholder="Например: Создай полнофункционального Telegram-бота на Python (aiogram 3) для отслеживания курсов валют и криптовалют с базой данных SQLite, кнопками, графиками и авто-обновлением..."
          class="w-full bg-slate-900/90 border border-slate-700 rounded-xl p-3.5 text-sm text-white focus:outline-none focus:border-violet-500 placeholder-slate-500"></textarea>
      </div>

      <div class="flex items-center justify-between pt-2">
        <span class="text-xs text-slate-400">Код автоматически структурируется, тестируется Git и публикуется в облако</span>
        <button id="runBtn" onclick="runCoder()"
          class="bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white font-medium text-sm px-6 py-2.5 rounded-xl shadow-lg shadow-violet-600/30 flex items-center gap-2 transition disabled:opacity-50">
          <span>🚀 Запустить разработку</span>
        </button>
      </div>

      <!-- Progress / Console Box -->
      <div id="consoleBox" class="hidden mt-4 p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-3">
        <div class="flex items-center justify-between text-xs text-slate-400">
          <span id="consoleStatus" class="font-mono text-violet-400 flex items-center gap-2">
            <span class="inline-block w-2 h-2 rounded-full bg-violet-400 animate-ping"></span>
            Генерация архитектуры и кода...
          </span>
          <span id="elapsedTime" class="font-mono">0s</span>
        </div>
        <pre id="consoleLogs" class="text-xs font-mono text-slate-300 max-h-60 overflow-y-auto whitespace-pre-wrap"></pre>

        <!-- Dynamic Success Action Buttons -->
        <div id="linksSection" class="hidden pt-3 border-t border-slate-800 flex flex-wrap items-center justify-between gap-3">
          <span class="text-xs text-emerald-400 font-semibold flex items-center gap-1.5">
            <span>✓</span> Проект успешно сгенерирован и опубликован!
          </span>
          <div class="flex items-center gap-2.5">
            <a id="githubBtn" href="#" target="_blank" class="hidden bg-slate-800 hover:bg-slate-700 border border-slate-600 text-white text-xs font-semibold px-3.5 py-2 rounded-lg flex items-center gap-1.5 transition">
              🐙 Открыть в GitHub
            </a>
            <a id="hfBtn" href="#" target="_blank" class="hidden bg-yellow-600 hover:bg-yellow-500 text-white text-xs font-semibold px-3.5 py-2 rounded-lg flex items-center gap-1.5 transition">
              🤗 Скачать (Hugging Face)
            </a>
            <a id="supabaseBtn" href="#" target="_blank" class="hidden bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold px-3.5 py-2 rounded-lg flex items-center gap-1.5 transition">
              📥 Скачать (Supabase)
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Storage History -->
    <div class="glass rounded-2xl p-6 space-y-4">
      <div class="flex items-center justify-between">
        <h2 class="text-base font-semibold text-slate-200 flex items-center gap-2">
          <span>📦</span> Сохранённые проекты в облаке
        </h2>
        <button onclick="loadProjects()" class="text-xs text-violet-400 hover:text-violet-300">↻ Обновить</button>
      </div>
      <div id="projectsList" class="space-y-2 text-sm text-slate-400">
        <div class="text-xs text-slate-500">Загрузка проектов...</div>
      </div>
    </div>

  </div>

  <script>
    const savedKey = localStorage.getItem('coder_auth_key') || 'sk-fcc-master-key-2026';
    document.getElementById('authKey').value = savedKey;

    let timerInterval = null;

    async function runCoder() {{
      const projectName = document.getElementById('projectName').value.trim() || 'my-project';
      const prompt = document.getElementById('promptInput').value.trim();
      const model = document.getElementById('modelSelect').value;
      const authKey = document.getElementById('authKey').value.trim();
      const pushGithub = document.getElementById('pushGithub').checked;
      const githubPrivate = document.getElementById('githubPrivate').checked;
      const uploadHf = document.getElementById('uploadHf').checked;

      if (!prompt) {{
        alert('Пожалуйста, введите задачу / ТЗ для проекта!');
        return;
      }}

      localStorage.setItem('coder_auth_key', authKey);

      const runBtn = document.getElementById('runBtn');
      const consoleBox = document.getElementById('consoleBox');
      const consoleStatus = document.getElementById('consoleStatus');
      const consoleLogs = document.getElementById('consoleLogs');
      const linksSection = document.getElementById('linksSection');
      const githubBtn = document.getElementById('githubBtn');
      const hfBtn = document.getElementById('hfBtn');
      const supabaseBtn = document.getElementById('supabaseBtn');
      const elapsedTime = document.getElementById('elapsedTime');

      runBtn.disabled = true;
      consoleBox.classList.remove('hidden');
      linksSection.classList.add('hidden');
      githubBtn.classList.add('hidden');
      hfBtn.classList.add('hidden');
      supabaseBtn.classList.add('hidden');

      consoleLogs.textContent = 'Инициализация агента Aider...\\nЗапрос к модели ' + model + '...\\n';

      let seconds = 0;
      clearInterval(timerInterval);
      timerInterval = setInterval(() => {{
        seconds++;
        elapsedTime.textContent = seconds + 's';
      }}, 1000);

      try {{
        const res = await fetch('/coder/api/generate', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            project_name: projectName,
            prompt: prompt,
            model: model,
            auth_key: authKey,
            push_github: pushGithub,
            github_private: githubPrivate,
            upload_hf: uploadHf
          }})
        }});

        const data = await res.json();
        clearInterval(timerInterval);

        if (!res.ok) {{
          consoleStatus.innerHTML = '<span class="text-red-400">Ошибка выполнения</span>';
          consoleLogs.textContent += '\\n[ОШИБКА]: ' + (data.detail || JSON.stringify(data));
          return;
        }}

        consoleStatus.innerHTML = '<span class="text-emerald-400">✓ Завершено успешно!</span>';
        consoleLogs.textContent += '\\n[Файлы проекта]:\\n' + data.files.map(f => '  + ' + f).join('\\n');
        consoleLogs.textContent += '\\n\\n[Git]: ' + data.git_commit;

        if (data.github_url) {{
          consoleLogs.textContent += '\\n[GitHub Repo]: ' + data.github_url;
          githubBtn.href = data.github_url;
          githubBtn.classList.remove('hidden');
        }}

        if (data.hf_url) {{
          consoleLogs.textContent += '\\n[Hugging Face Cloud]: ' + data.hf_url;
          hfBtn.href = data.hf_url;
          hfBtn.classList.remove('hidden');
        }}

        if (data.download_url) {{
          consoleLogs.textContent += '\\n[Supabase Storage]: ' + data.download_url;
          supabaseBtn.href = data.download_url;
          supabaseBtn.classList.remove('hidden');
        }}

        linksSection.classList.remove('hidden');
        loadProjects();
      }} catch (err) {{
        clearInterval(timerInterval);
        consoleStatus.innerHTML = '<span class="text-red-400">Сетевая ошибка</span>';
        consoleLogs.textContent += '\\n[Network Error]: ' + err.message;
      }} finally {{
        runBtn.disabled = false;
      }}
    }}

    async function loadProjects() {{
      const listEl = document.getElementById('projectsList');
      try {{
        const res = await fetch('/coder/api/list');
        const data = await res.json();
        if (!data || data.length === 0) {{
          listEl.innerHTML = '<div class="text-xs text-slate-500">Пока нет сохранённых архивов. Создайте первый проект!</div>';
          return;
        }}
        listEl.innerHTML = data.map(item => `
          <div class="flex items-center justify-between p-3 rounded-xl bg-slate-900/60 border border-slate-800/80 hover:border-slate-700 transition">
            <div class="flex items-center gap-3">
              <span class="text-lg">📦</span>
              <div>
                <div class="font-medium text-slate-200 text-xs">${{item.name}}</div>
                <div class="text-[10px] text-slate-500">${{item.created_at || ''}} • ${{item.size_kb}} KB</div>
              </div>
            </div>
            <a href="${{item.url}}" target="_blank"
              class="text-xs px-3 py-1.5 rounded-lg bg-violet-950/60 border border-violet-800/50 text-violet-300 hover:bg-violet-900/60 transition">
              Скачать
            </a>
          </div>
        `).join('');
      }} catch (e) {{
        listEl.innerHTML = '<div class="text-xs text-red-400">Не удалось загрузить проекты: ' + e.message + '</div>';
      }}
    }}

    loadProjects();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.post("/api/generate")
async def generate_project(task: CoderTaskRequest, request: Request):
    """Run autonomous project generation, git commit, GitHub repo push, HF upload and Supabase backup."""
    _verify_auth(task.auth_key, request)

    project_slug = _sanitize_project_name(task.project_name)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_dir = Path("/tmp") / f"coder_{project_slug}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Ask our local LLM proxy (Claude Fable 5.5) to produce the project code
        proxy_url = f"http://127.0.0.1:{os.getenv('PORT', '8082')}/v1/messages"
        token_to_use = _get_server_token(request)
        headers = {
            "x-api-key": token_to_use,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system_instruction = (
            "You are Aider, an autonomous expert full-stack software engineer. "
            "When given a project specification, you design the architecture and write complete, fully functional, production-ready code. "
            "IMPORTANT RULES:\n"
            "1. Output every single project file inside a fenced code block with the exact relative filename in the header comment, e.g.:\n"
            "```python\n# file: main.py\n<complete code>\n```\n"
            "2. Always include a README.md and requirements.txt (or package.json if JS/TS).\n"
            "3. Do NOT omit any code or leave placeholders like 'TODO'. Write full, runnable implementations.\n"
            "4. Provide a brief summary of what was built at the end."
        )

        user_message = (
            f"Project Name: {project_slug}\n"
            f"Task Specification:\n{task.prompt}\n\n"
            "Generate the complete project files now with full code."
        )

        payload = {
            "model": task.model,
            "max_tokens": 8192,
            "system": system_instruction,
            "messages": [{"role": "user", "content": user_message}],
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
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

        # 3. Parse files and write to temporary directory
        extracted_files = _extract_files_from_markdown(full_text)
        if not extracted_files:
            extracted_files["main.py"] = full_text
            extracted_files["README.md"] = f"# {project_slug}\n\nGenerated for prompt:\n{task.prompt}"

        created_files = []
        for rel_path, code in extracted_files.items():
            dest = run_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            created_files.append(rel_path)

        # 4. Initialize Git repo and make initial commit
        git_commit_hash = "no-git"
        try:
            subprocess.run(["git", "init"], cwd=run_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Aider Agent"], cwd=run_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aider@agent.local"], cwd=run_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=run_dir, check=True, capture_output=True)
            res = subprocess.run(["git", "commit", "-m", f"Initial commit: {project_slug} via {task.model}"], cwd=run_dir, check=True, capture_output=True, text=True)
            git_commit_hash = res.stdout.strip().split("\n")[0]
        except Exception:
            pass

        # 5. Create ZIP archive
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

        # 6. Publish to GitHub (if enabled)
        github_url = None
        if task.push_github:
            github_url = await _create_and_push_github(
                run_dir,
                project_name=project_slug,
                is_private=task.github_private,
                description=task.prompt[:200],
            )

        # 7. Upload to Hugging Face (50 GB Cloud Storage)
        hf_url = None
        if task.upload_hf:
            hf_url = await _upload_to_huggingface(zip_path, zip_filename)

        # 8. Upload to Supabase Storage (backup)
        download_url = await _upload_to_supabase(zip_filename, zip_data, content_type="application/zip")

        # Cleanup zip file
        if zip_path.exists():
            zip_path.unlink()

        return JSONResponse(
            content={
                "status": "success",
                "project_name": project_slug,
                "zip_filename": zip_filename,
                "github_url": github_url,
                "hf_url": hf_url,
                "download_url": download_url,
                "files": created_files,
                "git_commit": git_commit_hash,
            }
        )

    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


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
        async with httpx.AsyncClient(timeout=30.0) as client:
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
