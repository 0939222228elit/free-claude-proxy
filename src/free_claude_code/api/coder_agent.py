"""Autonomous Coder Agent Web UI & Supabase Storage Integration."""

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
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/coder", tags=["coder"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "projects")
AUTH_SECRET = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
DB_PASS = os.getenv("SUPABASE_DB_PASSWORD", "")


class CoderTaskRequest(BaseModel):
    project_name: str = Field(min_length=2, max_length=64)
    prompt: str = Field(min_length=5, max_length=16000)
    model: str = Field(default="claude-fable-5.5")
    auth_key: str = Field(min_length=1)


def _verify_auth(auth_key: str) -> None:
    valid_keys = {k for k in (AUTH_SECRET, DB_PASS, "sk-fcc-master-key-2026") if k}
    if not valid_keys or auth_key not in valid_keys:
        raise HTTPException(status_code=401, detail="Неверный ключ доступа (API Key или пароль)")


def _sanitize_project_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", name.strip())
    return cleaned or "project"


def _extract_files_from_markdown(text: str) -> dict[str, str]:
    """Extract code files embedded in markdown with file headers/comments."""
    files: dict[str, str] = {}
    pattern = re.compile(r"```(?:\w+)?\s*(?:#|//|<!--|--)?\s*(?:file:\s*|filename:\s*)?([a-zA-Z0-9_.\-\/\\]+)\n([\s\S]*?)```", re.IGNORECASE)
    matches = pattern.findall(text)
    for filepath, content in matches:
        clean_path = filepath.strip().strip("'\"<> ")
        if clean_path and not clean_path.startswith(("/", "\\", "..")):
            files[clean_path] = content

    # Fallback pattern: ### File: name or ### `filename`
    if not files:
        alt_pattern = re.compile(r"(?:###|####|\*\*)\s*(?:File:\s*|Файл:\s*)?`?([a-zA-Z0-9_.\-\/\\]+\.[a-zA-Z0-9]+)`?\s*\n+```(?:\w+)?\n([\s\S]*?)```", re.IGNORECASE)
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


async def _upload_to_supabase(filename: str, data: bytes, content_type: str = "application/zip") -> str:
    """Upload a file to Supabase Storage and return public download URL."""
    url_base, key, bucket = _supabase_config()
    if not url_base or not key:
        raise HTTPException(status_code=500, detail="SUPABASE_URL или SUPABASE_KEY не настроены")

    url = f"{url_base}/storage/v1/object/{bucket}/{filename}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, content=data)
        if resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=500,
                detail=f"Ошибка загрузки в Supabase Storage ({resp.status_code}): {resp.text}",
            )

    return f"{url_base}/storage/v1/object/public/{bucket}/{filename}"


@router.get("", response_class=HTMLResponse)
async def coder_dashboard(request: Request):
    """Serve the Aider & Claude Fable 5.5 Autonomous Coding Agent UI."""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aider Code Agent • Claude Fable 5.5 + Supabase</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{ background: #0b0f19; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    .glass {{ background: rgba(17, 24, 39, 0.75); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }}
    .glow {{ box-shadow: 0 0 30px rgba(124, 58, 237, 0.2); }}
  </style>
</head>
<body class="text-slate-100 min-h-screen py-8 px-4 flex flex-col items-center">
  <div class="w-full max-w-4xl space-y-6">

    <!-- Header -->
    <div class="glass glow rounded-2xl p-6 flex flex-col md:flex-row items-center justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="text-2xl">⚡</span>
          <h1 class="text-2xl font-bold bg-gradient-to-r from-purple-400 via-pink-400 to-indigo-400 bg-clip-text text-transparent">
            Aider Cloud Agent
          </h1>
          <span class="text-xs px-2.5 py-0.5 rounded-full bg-purple-900/60 text-purple-300 border border-purple-700/50">v5.5</span>
        </div>
        <p class="text-sm text-slate-400 mt-1">Автономный AI-программист на базе <b>Claude Fable 5.5</b> с сохранением в <b>Supabase Cloud</b></p>
      </div>
      <div class="flex items-center gap-2 text-xs">
        <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
        <span class="text-slate-300">Supabase: <code class="text-purple-300">hrxmvitapcwfubcdaomu</code></span>
      </div>
    </div>

    <!-- Main Card -->
    <div class="glass rounded-2xl p-6 space-y-5">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Имя проекта / репозитория</label>
          <input id="projectName" type="text" placeholder="crypto-bot" value="my-project"
            class="w-full bg-slate-900/90 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-purple-500">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Модель ИИ</label>
          <select id="modelSelect"
            class="w-full bg-slate-900/90 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-purple-500">
            <option value="claude-fable-5.5" selected>Claude Fable 5.5 (Рекомендуется)</option>
            <option value="claude-3-7-sonnet-20250219">Claude 3.7 Sonnet</option>
            <option value="claude-opus-4-20250514">Claude Opus 4</option>
            <option value="gemini/gemini-3.8-flash">Gemini 3.8 Flash</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Мастер-ключ / Пароль</label>
          <input id="authKey" type="password" placeholder="sk-fcc-... или пароль"
            class="w-full bg-slate-900/90 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-purple-500">
        </div>
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1">ТЗ / Задача для агента-программиста</label>
        <textarea id="promptInput" rows="5"
          placeholder="Например: Создай полнофункционального Telegram-бота на Python (aiogram 3) для отслеживания цен криптовалют с базой данных SQLite, красивыми кнопками и авто-обновлением каждые 5 минут..."
          class="w-full bg-slate-900/90 border border-slate-700 rounded-xl p-3.5 text-sm text-white focus:outline-none focus:border-purple-500 placeholder-slate-500"></textarea>
      </div>

      <div class="flex items-center justify-between pt-2">
        <span class="text-xs text-slate-400">Каждый проект архивируется в Git и мгновенно загружается в Supabase Storage</span>
        <button id="runBtn" onclick="runCoder()"
          class="bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white font-medium text-sm px-6 py-2.5 rounded-xl shadow-lg shadow-purple-600/30 flex items-center gap-2 transition disabled:opacity-50">
          <span>🚀 Запустить разработку</span>
        </button>
      </div>

      <!-- Progress / Console Box -->
      <div id="consoleBox" class="hidden mt-4 p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
        <div class="flex items-center justify-between text-xs text-slate-400">
          <span id="consoleStatus" class="font-mono text-purple-400 flex items-center gap-2">
            <span class="inline-block w-2 h-2 rounded-full bg-purple-400 animate-ping"></span>
            Генерация архитектуры и кода...
          </span>
          <span id="elapsedTime" class="font-mono">0s</span>
        </div>
        <pre id="consoleLogs" class="text-xs font-mono text-slate-300 max-h-60 overflow-y-auto whitespace-pre-wrap"></pre>
        <div id="downloadSection" class="hidden pt-3 border-t border-slate-800 flex items-center justify-between">
          <span class="text-xs text-emerald-400 font-semibold">✓ Проект успешно собран и загружен в Supabase!</span>
          <a id="downloadBtn" href="#" target="_blank"
            class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold px-4 py-2 rounded-lg shadow-md shadow-emerald-600/30 flex items-center gap-1.5 transition">
            📥 Скачать ZIP-архив
          </a>
        </div>
      </div>
    </div>

    <!-- Projects List from Supabase Storage -->
    <div class="glass rounded-2xl p-6 space-y-4">
      <div class="flex items-center justify-between">
        <h2 class="text-base font-semibold text-slate-200 flex items-center gap-2">
          <span>📂</span> Проекты в Supabase Storage (<code class="text-xs text-purple-400 font-mono">projects/</code>)
        </h2>
        <button onclick="loadProjects()" class="text-xs text-purple-400 hover:text-purple-300">↻ Обновить</button>
      </div>
      <div id="projectsList" class="space-y-2 text-sm text-slate-400">
        <div class="text-xs text-slate-500">Загрузка списка проектов...</div>
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

      if (!prompt) {{
        alert('Пожалуйста, введите задачу/ТЗ для проекта!');
        return;
      }}

      localStorage.setItem('coder_auth_key', authKey);

      const runBtn = document.getElementById('runBtn');
      const consoleBox = document.getElementById('consoleBox');
      const consoleStatus = document.getElementById('consoleStatus');
      const consoleLogs = document.getElementById('consoleLogs');
      const downloadSection = document.getElementById('downloadSection');
      const downloadBtn = document.getElementById('downloadBtn');
      const elapsedTime = document.getElementById('elapsedTime');

      runBtn.disabled = true;
      consoleBox.classList.remove('hidden');
      downloadSection.classList.add('hidden');
      consoleLogs.textContent = 'Инициализация агента Aider...\\nОбращение к модели ' + model + '...\\n';

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
            auth_key: authKey
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
        consoleLogs.textContent += '\\n[Supabase URL]: ' + data.download_url;

        downloadBtn.href = data.download_url;
        downloadSection.classList.remove('hidden');

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
          listEl.innerHTML = '<div class="text-xs text-slate-500">В хранилище пока нет проектов. Создайте первый!</div>';
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
              class="text-xs px-3 py-1.5 rounded-lg bg-purple-950/60 border border-purple-800/50 text-purple-300 hover:bg-purple-900/60 transition">
              Скачать
            </a>
          </div>
        `).join('');
      }} catch (e) {{
        listEl.innerHTML = '<div class="text-xs text-red-400">Не удалось загрузить список проектов: ' + e.message + '</div>';
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
    """Run autonomous project generation, git commit, zip and upload to Supabase."""
    _verify_auth(task.auth_key)

    project_slug = _sanitize_project_name(task.project_name)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_dir = Path("/tmp") / f"coder_{project_slug}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Ask our local LLM proxy (Claude Fable 5.5) to produce the project code
        proxy_url = f"http://127.0.0.1:{os.getenv('PORT', '8082')}/v1/messages"
        headers = {
            "x-api-key": AUTH_SECRET,
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
            # Fallback if markdown parser didn't find headers: save main.py and README.md
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
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in run_dir.rglob("*"):
                if ".git" in file_path.parts:
                    continue
                if file_path.is_file():
                    arcname = file_path.relative_to(run_dir)
                    zip_file.write(file_path, arcname)

        zip_data = zip_buffer.getvalue()

        # 6. Upload ZIP to Supabase Storage
        download_url = await _upload_to_supabase(zip_filename, zip_data, content_type="application/zip")

        return JSONResponse(
            content={
                "status": "success",
                "project_name": project_slug,
                "zip_filename": zip_filename,
                "download_url": download_url,
                "files": created_files,
                "git_commit": git_commit_hash,
            }
        )

    finally:
        # Cleanup temporary files
        shutil.rmtree(run_dir, ignore_errors=True)


@router.get("/api/list")
async def list_supabase_projects():
    """List all project zip archives from Supabase Storage."""
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
