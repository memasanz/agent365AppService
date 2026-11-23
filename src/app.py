import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from dotenv import load_dotenv

from src.observability import configure_observability, project_endpoint

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
telemetry = configure_observability()

# Telemetry must be configured before importing framework/model modules.
from agent_framework import Agent  # noqa: E402
from agent_framework.foundry import FoundryChatClient  # noqa: E402
from agent_framework.observability import disable_instrumentation  # noqa: E402
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, StringConstraints  # noqa: E402

disable_instrumentation()
logger = logging.getLogger(__name__)
Message = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=8000)
]
Conversation = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Za-z0-9._:-]{1,128}$"),
]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Message
    conversation_id: Conversation | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str
    trace_id: str | None = None


@asynccontextmanager
async def lifespan(app):
    endpoint = project_endpoint(os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""))
    model = os.environ.get("FOUNDRY_MODEL", "").strip()
    if not model:
        raise ValueError("Set FOUNDRY_MODEL to your Responses-compatible deployment name.")
    # Explicitly system-assigned: never use AZURE_CLIENT_ID for hosted model access.
    credential = (
        ManagedIdentityCredential()
        if os.environ.get("WEBSITE_INSTANCE_ID")
        else AzureCliCredential()
    )
    client = FoundryChatClient(
        project_endpoint=endpoint, model=model, credential=credential, timeout=60
    )
    app.state.agent = Agent(
        client=client,
        name="AppServiceLearningAgent",
        instructions=(
            "You are a helpful conversational assistant. Give clear, concise, accurate answers."
        ),
    )
    app.state.model, app.state.endpoint = model, endpoint
    try:
        yield
    finally:
        await client.close()
        await credential.close()
        if telemetry:
            await asyncio.to_thread(telemetry.shutdown)


app = FastAPI(
    title="Agent 365 App Service starter", lifespan=lifespan, docs_url=None, redoc_url=None
)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, _error):
    # FastAPI's default validation response echoes rejected inputs.
    return JSONResponse(status_code=422, content={"detail": "Invalid message or conversation_id."})


@app.middleware("http")
async def limit_body(request, call_next):
    if request.url.path == "/chat":
        length = request.headers.get("content-length")
        if length and (not length.isdecimal() or int(length) > 40000):
            return JSONResponse(status_code=413, content={"detail": "Request too large."})
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 40000:
                return JSONResponse(status_code=413, content={"detail": "Request too large."})
        request._body = bytes(body)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    conversation_id = body.conversation_id or str(uuid4())
    try:
        if telemetry:
            response, trace_id = await telemetry.run(
                request.app.state.agent.run,
                body.message,
                conversation_id,
                request.client.host if request.client else "0.0.0.0",
                request.app.state.model,
                request.app.state.endpoint,
            )
        else:
            response = await request.app.state.agent.run(body.message)
            trace_id = None
        return ChatResponse(reply=response.text, conversation_id=conversation_id, trace_id=trace_id)
    except Exception:
        logger.error(
            "Model request failed; conversation=%s. Check model RBAC and connectivity.",
            conversation_id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "detail": "Model request failed. Check service diagnostics.",
                "conversation_id": conversation_id,
            },
        )
