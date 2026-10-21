"""FastAPI route handlers."""

from collections.abc import Mapping
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from loguru import logger
from pydantic import ValidationError

from free_claude_code.application.errors import ApplicationError, InvalidRequestError
from free_claude_code.application.ports import ProviderResolver, RequestRuntimeLease
from free_claude_code.application.routing import ModelRouter, supports_native_messages
from free_claude_code.config.model_refs import parse_provider_type
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic import (
    MessagesRequest,
    NativeTokenCountRequest,
    TokenCountRequest,
    get_token_count,
)
from free_claude_code.core.anthropic.native import (
    NativeMessagesError,
    validate_messages_json,
)
from free_claude_code.core.anthropic.passthrough import NativeMessagesRequest
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.core.trace import trace_event

from .dependencies import (
    get_services,
    get_settings,
    require_anthropic_proxy_auth,
    require_proxy_auth,
    resolve_provider,
)
from .handlers import MessagesHandler, ResponsesHandler, TokenCountHandler
from .model_catalog import (
    ModelCatalogView,
    ModelsListResponse,
    build_models_list_response,
    build_muse_models_list_response,
)
from .ports import ApiServices
from .request_errors import ordinary_application_error_response
from .request_ids import get_request_id
from .response_streams import bind_response_lifetime

router = APIRouter()


def _provider_resolver(lease: RequestRuntimeLease) -> ProviderResolver:
    return lambda provider_type, api_key_number: resolve_provider(
        provider_type, api_key_number, lease=lease
    )


async def _create_messages_response(
    services: ApiServices,
    request_data: MessagesRequest | JsonObject,
    *,
    request_id: str,
    request_headers: Mapping[str, str] | None = None,
) -> object:
    lease: RequestRuntimeLease | None = None
    try:
        lease = await services.requests.acquire()
        router = ModelRouter(lease.settings)
        raw = (
            request_data.model_dump(mode="json", exclude_unset=True)
            if isinstance(request_data, MessagesRequest)
            else request_data
        )
        model = raw.get("model")
        resolved = (
            router.resolve(model) if isinstance(model, str) and model.strip() else None
        )
        native = resolved is not None and supports_native_messages(
            resolved.primary.provider_id
        )
        if native:
            try:
                native_request = NativeMessagesRequest(raw)
            except NativeMessagesError as error:
                raise InvalidRequestError(str(error)) from error
        else:
            if not isinstance(request_data, MessagesRequest):
                try:
                    request_data = MessagesRequest.model_validate(raw)
                except ValidationError as error:
                    raise RequestValidationError(
                        [
                            {**item, "loc": ("body", *item["loc"])}
                            for item in error.errors()
                        ],
                        body=raw,
                    ) from error
            await lease.wait_for_token_estimation()
        handler = MessagesHandler(
            lease.settings,
            web_tools=services.web_tools,
            provider_resolver=_provider_resolver(lease),
            token_counter=get_token_count,
            generation_id=lease.generation_id,
            request_headers=request_headers,
            model_info_lookup=lease.model_info,
        )
        if native:
            assert resolved is not None
            response = await handler.create_native(
                router.route_native_messages(native_request, resolved),
                request_id=request_id,
            )
        else:
            assert isinstance(request_data, MessagesRequest)
            response = await handler.create(request_data, request_id=request_id)
    except ApplicationError as exc:
        if lease is not None:
            await lease.release()
        return ordinary_application_error_response(
            exc,
            wire_api="messages",
            request_id=request_id,
        )
    except BaseException:
        if lease is not None:
            await lease.release()
        raise
    assert lease is not None
    return await bind_response_lifetime(response, lease.release)


async def _create_responses_response(
    services: ApiServices,
    request_data: OpenAIResponsesRequest,
    *,
    request_id: str,
    request_headers: Mapping[str, str] | None = None,
) -> object:
    lease: RequestRuntimeLease | None = None
    try:
        lease = await services.requests.acquire()
        await lease.wait_for_token_estimation()
        handler = ResponsesHandler(
            lease.settings,
            provider_resolver=_provider_resolver(lease),
            generation_id=lease.generation_id,
            request_headers=request_headers,
            model_info_lookup=lease.model_info,
        )
        response = await handler.create(request_data, request_id=request_id)
    except ApplicationError as exc:
        if lease is not None:
            await lease.release()
        return ordinary_application_error_response(
            exc,
            wire_api="responses",
            request_id=request_id,
        )
    except BaseException:
        if lease is not None:
            await lease.release()
        raise
    assert lease is not None
    return await bind_response_lifetime(response, lease.release)


def _probe_response(allow: str) -> Response:
    return Response(status_code=204, headers={"Allow": allow})


@router.post("/v1/messages")
async def create_message(
    request: Request,
    request_data: Annotated[dict[str, Any], Body()],
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_anthropic_proxy_auth),
):
    """Create a message (JSON by default; stream=true returns Anthropic SSE)."""
    return await _create_messages_response(
        services,
        cast(JsonObject, request_data),
        request_id=get_request_id(request),
        request_headers=request.headers,
    )


@router.api_route("/v1/messages", methods=["HEAD", "OPTIONS"])
async def probe_messages(_auth=Depends(require_anthropic_proxy_auth)):
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/responses")
async def create_response(
    request: Request,
    request_data: OpenAIResponsesRequest,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Create an OpenAI Responses-compatible response through this proxy."""
    return await _create_responses_response(
        services,
        request_data,
        request_id=get_request_id(request),
        request_headers=request.headers,
    )


@router.api_route("/v1/responses", methods=["HEAD", "OPTIONS"])
async def probe_responses(_auth=Depends(require_proxy_auth)):
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    request_data: Annotated[dict[str, Any], Body()],
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_anthropic_proxy_auth),
):
    """Count tokens for a request."""
    lease = await services.requests.acquire()
    try:
        model_router = ModelRouter(lease.settings)
        model = request_data.get("model")
        if isinstance(model, str) and not model.strip():
            raise InvalidRequestError("Messages model must not be empty.")
        resolved = (
            model_router.resolve(model)
            if isinstance(model, str) and model.strip()
            else None
        )
        try:
            if resolved is not None and supports_native_messages(
                resolved.primary.provider_id
            ):
                validate_messages_json(request_data)
                counted = NativeTokenCountRequest.model_validate(request_data)
            else:
                counted = TokenCountRequest.model_validate(request_data)
        except NativeMessagesError as error:
            raise InvalidRequestError(str(error)) from error
        except ValidationError as error:
            raise RequestValidationError(
                [{**item, "loc": ("body", *item["loc"])} for item in error.errors()],
                body=request_data,
            ) from error
        await lease.wait_for_token_estimation()
        handler = TokenCountHandler(
            lease.settings, model_router=model_router, token_counter=get_token_count
        )
        return handler.count(
            counted, request_id=get_request_id(request), resolved=resolved
        )
    finally:
        await lease.release()


@router.api_route("/v1/messages/count_tokens", methods=["HEAD", "OPTIONS"])
async def probe_count_tokens(_auth=Depends(require_anthropic_proxy_auth)):
    return _probe_response("POST, HEAD, OPTIONS")


@router.get("/")
async def root(
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    return {
        "status": "ok",
        "provider": parse_provider_type(settings.model),
        "model": settings.model,
    }


@router.api_route("/", methods=["HEAD", "OPTIONS"])
async def probe_root():
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/health")
async def health():
    return {"status": "healthy"}


@router.api_route("/health", methods=["HEAD", "OPTIONS"])
async def probe_health():
    return _probe_response("GET, HEAD, OPTIONS")


@router.get(
    "/v1/models",
    response_model=ModelsListResponse,
    response_model_exclude_none=True,
)
async def list_models(
    view: ModelCatalogView | None = None,
    x_fcc_model_view: ModelCatalogView | None = Header(default=None),
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_anthropic_proxy_auth),
):
    """List the model ids this proxy advertises to compatible clients."""
    trace_event(stage="ingress", event="free_claude_code.api.models.list", source="api")
    snapshot = await services.requests.wait_for_catalog()
    return build_models_list_response(
        snapshot.settings,
        snapshot,
        view=view or x_fcc_model_view or ModelCatalogView.CLAUDE,
    )


@router.get(
    "/muse-code/models",
    response_model=ModelsListResponse,
    response_model_exclude_none=True,
)
async def list_muse_models(
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """List the direct Responses models expected by Muse Code."""
    trace_event(stage="ingress", event="free_claude_code.api.models.list", source="api")
    snapshot = await services.requests.wait_for_catalog()
    return build_muse_models_list_response(snapshot.settings, snapshot)


@router.post("/stop")
async def stop_cli(
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Stop all CLI sessions and pending tasks."""
    result = await services.tasks.stop_all()
    if result is None:
        raise HTTPException(status_code=503, detail="Messaging system not initialized")
    if result.source is not None:
        logger.info("STOP_CLI: source={} cancelled_count=N/A", result.source)
        return {"status": "stopped", "source": result.source}

    count = result.cancelled_count or 0
    trace_event(
        stage="ingress",
        event="free_claude_code.api.cli.stop_via_messaging_workflow",
        source="api",
        cancelled_nodes=count,
    )
    logger.info("STOP_CLI: source=messaging_workflow cancelled_count={}", count)
    return {"status": "stopped", "cancelled_count": count}


DOWNLOAD_HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Скачать файл подключения Claude Opus 5.5 для бота</title>
  <link rel="icon" href="/coder/icon.svg" type="image/svg+xml">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background-color: #12100f; color: #f5f2eb; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
    .terracotta-btn { background-color: #d97757; color: white; transition: all 0.2s; }
    .terracotta-btn:hover { background-color: #c46445; transform: translateY(-1px); }
  </style>
</head>
<body class="min-h-screen flex flex-col items-center justify-center p-4">
  <div class="max-w-xl w-full bg-[#1a1715] border border-[#2e2824] rounded-2xl p-6 sm:p-8 shadow-2xl">
    <div class="flex items-center gap-3 mb-6">
      <div class="w-12 h-12 rounded-xl overflow-hidden border border-[#3e342d] bg-[#221c18] flex items-center justify-center shadow-lg">
        <img src="/coder/icon.svg" alt="Claude" class="w-full h-full object-cover">
      </div>
      <div>
        <h1 class="text-xl font-bold text-[#f5f2eb]">Файл подключения для бота</h1>
        <p class="text-xs text-[#a69e92]">Claude Opus 5.5 • Proxy Kit</p>
      </div>
    </div>

    <div class="space-y-4 mb-6">
      <div class="bg-[#12100f] border border-[#26211d] rounded-xl p-4 text-xs font-mono space-y-2">
        <div class="flex justify-between"><span class="text-[#888]">API Key:</span><span class="text-emerald-400 font-bold">sk-fcc-master-key-2026</span></div>
        <div class="flex justify-between"><span class="text-[#888]">Model:</span><span class="text-[#d97757] font-bold">claude-opus-5.5</span></div>
        <div class="flex justify-between"><span class="text-[#888]">Base URL:</span><span class="text-[#c7bfb3] truncate max-w-[260px]">https://free-claude-code-proxy-kwbo.onrender.com</span></div>
      </div>

      <!-- Main Download Button -->
      <a href="/download/claude_bot_connect.py" download="claude_bot_connect.py"
        class="terracotta-btn w-full py-3.5 px-4 rounded-xl font-bold text-sm flex items-center justify-center gap-2 shadow-lg shadow-[#d97757]/20">
        <span>⬇️</span> Скачать claude_bot_connect.py (10 KB)
      </a>

      <!-- Secondary Zip Download -->
      <a href="/download/claude_bot_kit.zip" download="claude_bot_kit.zip"
        class="w-full py-2.5 px-4 rounded-xl font-medium text-xs bg-[#241f1c] hover:bg-[#322b27] border border-[#382f2a] text-[#ded6cb] flex items-center justify-center gap-2 transition">
        <span>📦</span> Скачать всё архивом (.zip: .py + .env + config.json)
      </a>
    </div>

    <div class="pt-4 border-t border-[#26211d] flex items-center justify-between text-xs text-[#8a8277]">
      <a href="/coder" class="hover:text-[#d97757] transition">← В Claude Studio</a>
      <a href="https://raw.githubusercontent.com/0939222228elit/free-claude-proxy/main/claude_bot_connect.py" target="_blank" class="hover:text-[#d97757] transition">GitHub Raw ↗</a>
    </div>
  </div>
</body>
</html>"""


@router.api_route("/download", methods=["GET", "HEAD"])
async def download_page():
    return Response(content=DOWNLOAD_HTML_PAGE, media_type="text/html; charset=utf-8")


@router.api_route("/download/claude_bot_connect.py", methods=["GET", "HEAD"])
@router.api_route("/claude_bot_connect.py", methods=["GET", "HEAD"])
async def download_bot_connect():
    """Download the self-contained chatbot connection kit for Claude Opus 5.5."""
    import pathlib
    script_path = pathlib.Path(__file__).resolve().parents[3] / "claude_bot_connect.py"
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="Connection file not found")
    content = script_path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="text/x-python; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="claude_bot_connect.py"',
            "Cache-Control": "no-cache",
        },
    )


@router.api_route("/download/claude_bot_kit.zip", methods=["GET", "HEAD"])
@router.api_route("/claude_bot_kit.zip", methods=["GET", "HEAD"])
async def download_bot_zip():
    """Download the full zip kit containing python client, env and config."""
    import pathlib
    zip_path = pathlib.Path(__file__).resolve().parents[3] / "claude_bot_kit.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Zip kit not found")
    content = zip_path.read_bytes()
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="claude_bot_kit.zip"',
            "Cache-Control": "no-cache",
        },
    )


