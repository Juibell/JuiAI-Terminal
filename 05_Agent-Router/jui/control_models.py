"""Explicit provider transports; no ambient credentials, retries, or cloud fallback."""

import asyncio
import ipaddress
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from . import retrieval
from .config import load_settings
from .control_local_models import QWEN_CONTEXT, QWEN_ID, managed_model

os.environ.update(
    LITELLM_LOCAL_MODEL_COST_MAP="True",
    LITELLM_TELEMETRY_DISABLED="true",
    LITELLM_DISABLE_LATEST_RELEASE_CHECK="true",
    DO_NOT_TRACK="1",
)


def endpoint(value, protocol):
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ValueError(
            "Use an endpoint without embedded credentials, query or fragment"
        )
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.lower() == "localhost"
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError(
            "External endpoints require HTTPS; HTTP is allowed only on loopback"
        )
    if local and parsed.port in {load_settings()["port"], 12394, 18790, 8643, 4097}:
        raise ValueError(
            "An agent or control-center endpoint cannot be an upstream provider"
        )
    if protocol not in {"openai", "anthropic", "gemini", "brave", "tavily", "searxng"}:
        raise ValueError("Unsupported provider protocol")
    return value.rstrip("/"), local


def cloud_guard(profile, sensitive, policy, audio=False):
    if profile["provider"].get("local"):
        return
    if audio and not policy.get("allow_cloud_audio"):
        raise ValueError("Cloud audio is not authorized for this workspace")
    if sensitive and not policy.get("allow_cloud_context"):
        raise ValueError(
            "This workspace has not authorized sending files, memories or related history to external services"
        )


class ModelEngine:
    def __init__(self, store, gpu_lock, prepare_model=None):
        self.store, self.gpu_lock = store, gpu_lock
        self.prepare_model = prepare_model

    @asynccontextmanager
    async def model_session(self, profile):
        if profile["provider"].get("local"):
            async with self.gpu_lock:
                if self.prepare_model:
                    await self.prepare_model(profile)
                yield
        else:
            yield

    async def completion(self, profile, messages, stream=None, tools=None, **extra):
        import litellm

        provider = profile["provider"]
        if provider["protocol"] not in {"openai", "anthropic", "gemini"}:
            raise ValueError("This connection does not provide text completions")
        # A conservative bound avoids silently dropping user messages or tool evidence.
        size = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, list):
                size += sum(
                    2048
                    if item.get("type") == "image_url"
                    else len(str(item.get("text", "")))
                    for item in content
                )
            else:
                size += len(str(content))
        context = profile.get("context", 8192)
        identity = managed_model(profile)
        if identity == QWEN_ID:
            context = min(context, QWEN_CONTEXT)
        if size + profile.get("max_tokens", 3072) > context:
            raise ValueError(
                "Conversation exceeds this model's context; start a new conversation or choose a larger context"
            )
        arguments = {
            "model": provider["protocol"] + "/" + profile["model_id"],
            "api_base": provider["base_url"],
            "api_key": self.store.key(provider) or "local-no-key",
            "messages": messages,
            "max_tokens": profile.get("max_tokens", 3072),
            "temperature": profile.get("temperature", 0.7),
            "timeout": profile.get("timeout", 240),
            "stream": profile.get("stream", False) if stream is None else stream,
            "num_retries": 0,
        }
        if tools:
            if "tools" not in profile.get("capabilities", []):
                raise ValueError("Model is not configured for tool calling")
            arguments["tools"] = tools
        if identity:
            arguments["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False}
            }
        arguments.update(
            {
                k: v
                for k, v in extra.items()
                if k in {"tool_choice", "response_format", "stop"}
            }
        )
        try:
            return await litellm.acompletion(**arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- third-party SDK errors must never expose credentials.
            raise RuntimeError(
                "Model request failed ("
                + type(error).__name__
                + "); no fallback or retry was performed"
            ) from None

    async def text(self, profile, messages, on_delta=None, response_format=None):
        async def perform():
            response = await self.completion(
                profile,
                messages,
                stream=bool(on_delta and profile.get("stream")),
                **({"response_format": response_format} if response_format else {}),
            )
            if not on_delta or not profile.get("stream"):
                text = response.choices[0].message.content or ""
                if on_delta and text:
                    on_delta(text)
                return text
            chunks = []
            try:
                async for chunk in response:
                    if chunk.choices and (value := chunk.choices[0].delta.content):
                        chunks.append(value)
                        on_delta(value)
            finally:
                close = getattr(response, "aclose", None)
                if close:
                    await close()
            return "".join(chunks)

        async with self.model_session(profile):
            try:
                async with asyncio.timeout(profile.get("timeout", 240)):
                    return await perform()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, ValueError):
                    raise
                raise RuntimeError(
                    "Model response failed ("
                    + type(error).__name__
                    + "); no retry was performed"
                ) from None

    async def probe(self, provider, generate_model=None):
        key = self.store.key(provider)
        headers = {"Authorization": "Bearer " + key} if key else {}
        protocol, base = provider["protocol"], provider["base_url"]
        if protocol.startswith("native-"):
            return {
                "connected": True,
                "models": [],
                "note": "Local voice configuration; use the explicit voice test to verify runtime",
            }
        if protocol == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if protocol == "gemini":
            headers = {"x-goog-api-key": key}
        async with httpx.AsyncClient(
            timeout=12, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.get(base + "/models", headers=headers)
            response.raise_for_status()
            body = response.json()
        models = [
            m.get("id", m.get("name", "").removeprefix("models/"))
            for m in body.get("data", body.get("models", []))
            if isinstance(m, dict)
        ]
        result = {"connected": True, "models": models[:500]}
        if generate_model:
            text = await self.text(
                generate_model, [{"role": "user", "content": "Reply with OK only."}]
            )
            result["generation"] = text[:100]
        return result


async def search(store, query, configuration):
    if not configuration.get("enabled"):
        raise ValueError("Search is disabled")
    provider = store.get("provider", configuration["provider_id"])
    if not provider.get("enabled"):
        raise ValueError("Search provider is disabled")
    protocol, base = provider["protocol"], provider["base_url"]
    key = store.key(provider)
    count = max(1, min(int(configuration.get("count", 5)), 10))
    async with httpx.AsyncClient(
        timeout=20, trust_env=False, follow_redirects=False
    ) as client:
        if protocol == "brave":
            response = await client.get(
                base + "/web/search",
                params={
                    "q": query,
                    "count": count,
                    "country": configuration.get("region", "us"),
                    "search_lang": configuration.get("language", "en"),
                },
                headers={"X-Subscription-Token": key},
            )
        elif protocol == "tavily":
            response = await client.post(
                base + "/search",
                headers={"Authorization": "Bearer " + key},
                json={
                    "query": query,
                    "max_results": count,
                    "include_raw_content": False,
                },
            )
        elif protocol == "searxng":
            response = await client.get(
                base + "/search",
                params={
                    "q": query,
                    "format": "json",
                    "language": configuration.get("language", "auto"),
                    "safesearch": 1,
                },
            )
        else:
            raise ValueError("Not a search connection")
        response.raise_for_status()
        body = response.json()
    rows = (
        body.get("web", {}).get("results", [])
        if protocol == "brave"
        else body.get("results", [])
    )
    candidates = [
        {
            "title": str(row.get("title", ""))[:300],
            "url": str(row.get("url", ""))[:2000],
            "summary": str(row.get("description", row.get("content", "")))[:2000],
        }
        for row in rows[: count * 2]
        if urlsplit(str(row.get("url", ""))).scheme in {"https", "http"}
    ]
    if len(candidates) > 1:
        scores = await asyncio.to_thread(
            retrieval.scores,
            query,
            [row["title"] + "\n" + row["summary"] for row in candidates],
        )
        if scores is not None:
            order = sorted(range(len(candidates)), key=scores.__getitem__, reverse=True)
            candidates = [candidates[index] for index in order]
    return candidates[:count]
