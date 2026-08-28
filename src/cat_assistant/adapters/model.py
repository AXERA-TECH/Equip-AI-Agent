from __future__ import annotations

import json
import asyncio
import re
from dataclasses import dataclass
from collections.abc import Sequence
from urllib.request import Request, urlopen

from cat_assistant.domain.models import ChatMessage, ModelReply, ToolCall, ToolSpec


class DemoRuleBasedModel:
    """Offline development double. Replace with a vLLM adapter in deployment."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        tool_names = {tool.name for tool in tools}
        tool_messages = [message for message in messages if message.role == "tool"]
        evidence_messages = [
            message
            for message in messages
            if message.role == "system"
            and message.content.startswith("Collected knowledge evidence")
        ]
        # Knowledge evidence already gathered upstream and injected into context:
        # synthesize from it directly, regardless of whether search tools are
        # offered. The terminal diagnostic.agent runs without tools, so gating this
        # on a search tool being present would drop the citation on that path.
        if evidence_messages:
            evidence = evidence_messages[-1].content.split("\n", 1)[-1]
            try:
                hits = json.loads(evidence)
            except json.JSONDecodeError:
                hits = []
            if hits:
                first = hits[0]
                return ModelReply(content=f"{first['content']} 来源：{first['source']}。")
            return ModelReply(content="本机资料中没有找到与该问题匹配的已验证内容。")
        if not tool_messages and "search_manual" in tool_names:
            question = next(
                message.content for message in reversed(messages) if message.role == "user"
            )
            return ModelReply(
                tool_calls=(ToolCall("search_manual", {"query": question}),)
            )

        if tool_messages:
            payload = json.loads(tool_messages[-1].content)
            if payload.get("is_error"):
                return ModelReply(content="本地资料查询失败，无法给出可靠诊断。")
            hits = json.loads(payload["content"])
            if hits:
                first = hits[0]
                return ModelReply(
                    content=f"{first['content']} 来源：{first['source']}。"
                )
        return ModelReply(content="本机没有找到与该问题匹配的已验证资料。")


@dataclass(frozen=True, slots=True)
class OpenAICompatibleModel:
    """Minimal stdlib adapter for vLLM/Ollama/OpenAI-compatible chat APIs."""

    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout_seconds: int = 30
    disable_thinking: bool = False
    stop: tuple[str, ...] = ()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        payload_messages = [_message_payload(message) for message in messages]
        if self.disable_thinking:
            _apply_no_think(payload_messages)
        payload = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # Bound generation at the turn boundary. Edge runtimes routinely fail to
        # register ``<|im_end|>`` as a stop token, so the model runs past its turn
        # and starts emitting the next (role-tagged) ChatML turn. Sending ``stop``
        # halts that runaway even when the server forgot to.
        if self.stop:
            payload["stop"] = list(self.stop)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self._endpoint(), data=body, headers=headers, method="POST")
        response = await asyncio.to_thread(self._request, request)
        try:
            choice = response["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("model response does not contain choices[0].message") from exc
        calls = []
        for call in message.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            name, arguments = _tool_call_fields(call)
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("model tool arguments must be a JSON object")
            calls.append(
                ToolCall(
                    name=_normalize_tool_name(name),
                    arguments=arguments,
                    call_id=str(call.get("id") or ""),
                )
            )
        # Edge runtimes (AXera AXLLM, llama.cpp, a vLLM with no tool-call parser)
        # often return Qwen/Hermes <tool_call> blocks as plain text in
        # ``content`` instead of the structured ``tool_calls`` field, and leave
        # the <think> reasoning inline. Recover the calls from the text when the
        # structured field is empty, and always strip <think> so it reaches
        # neither the operator nor the diagnostic agent's context. Both the
        # structured and text paths run through _tool_call_fields, so the several
        # name/argument shapes edge templates emit (notably AXLLM's Qwen3.5 build,
        # which puts the tool name under ``function`` as a bare string) all parse.
        content_text = _content_text(message.get("content"))
        content_text = _strip_role_leakage(content_text)
        recovered, content_text = _recover_tool_calls(content_text)
        if not calls:
            calls = recovered
        finish_reason = choice.get("finish_reason")
        return ModelReply(
            content=content_text,
            tool_calls=tuple(calls),
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            usage=_usage_view(response.get("usage")),
        )

    def _endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def _request(self, request: Request) -> dict[str, object]:
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("model response must be a JSON object")
        return decoded


def _message_payload(message: ChatMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict)
        )
    return ""


_THINK_PAIR_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_ROLE_LEAK_RE = re.compile(r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>")
_TOOL_NAME_WS_RE = re.compile(r"\s+")


def _strip_role_leakage(text: str) -> str:
    """Drop ChatML control tokens the server failed to stop the generation on.

    A correctly served turn never contains ``<|im_start|>``/``<|im_end|>`` in its
    text; their presence means the edge runtime did not treat ``<|im_end|>`` as a
    stop token (or did not add the assistant generation prompt), so the model ran
    past its own turn and began emitting the next, role-tagged turn (e.g. a fake
    ``<|im_start|>user`` echoing the question back). Keep only the text before the
    first control token, so the leaked turn reaches neither the operator nor the
    diagnostic agent's context — and the tool-call recovery below sees only the
    model's own turn, not a tool call hallucinated inside the fake turn.
    """
    match = _ROLE_LEAK_RE.search(text)
    if match is not None:
        return text[: match.start()]
    return text


def _strip_think(text: str) -> str:
    """Remove Qwen3 <think> reasoning: whole pairs, plus a dangling unclosed
    block left by a truncated (finish_reason="length") generation."""
    text = _THINK_PAIR_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text


def _normalize_tool_name(name: object) -> str:
    """Strip whitespace an edge detokenizer injected into an opaque tool name.

    OpenAI function names match ``^[a-zA-Z0-9_-]+$`` and never contain
    whitespace, but a GPTQ-quantized edge model (observed: AXLLM Qwen3.5-4B)
    copying our ``cap_<name>_<hash>`` string splits the non-linguistic hash
    suffix into fragile subword tokens and detokenizes them with a spurious
    space — e.g. ``cap_telemetry_summarize_b a63631c`` for
    ``cap_telemetry_summarize_ba63631c``. One mangled name is enough to get the
    whole plan rejected as ``unknown_tool``. Removing all internal whitespace
    restores the intended name so it maps back to a capability. Returns "" for a
    non-string so the caller drops the call rather than crashing.
    """
    if not isinstance(name, str):
        return ""
    return _TOOL_NAME_WS_RE.sub("", name)


def _tool_call_fields(payload: dict[str, object]) -> tuple[object, object]:
    """Pull ``(name, arguments)`` out of one tool-call object, tolerating the
    several shapes edge runtimes emit for a Qwen/Hermes call.

    * ``{"name": "...", "arguments": {...}}`` — the Hermes standard and what a
      structured OpenAI ``tool_calls[i].function`` also reduces to.
    * ``{"function": {"name": "...", "arguments": ...}}`` — the OpenAI nested
      shape, both as a real ``tool_calls`` entry and when it leaks into text.
    * ``{"function": "...", "arguments": {...}}`` — AXera AXLLM's Qwen3.5 chat
      template puts the tool *name* under ``function`` as a bare string (observed
      on the 4B build). Without handling this the block parses as JSON but yields
      ``name=None``, so every call is dropped and the whole LLM plan is silently
      discarded in favour of the rule planner.

    An explicit top-level ``name``/``arguments`` always wins; ``function`` only
    fills in what is missing, so a well-formed Hermes block is never disturbed.
    """
    name = payload.get("name")
    arguments = payload.get("arguments", {})
    function = payload.get("function")
    if isinstance(function, str):
        if not (isinstance(name, str) and name):
            name = function
    elif isinstance(function, dict):
        if not (isinstance(name, str) and name):
            name = function.get("name")
        if "arguments" not in payload and "arguments" in function:
            arguments = function.get("arguments", {})
    return name, arguments


def _recover_tool_calls(content: str) -> tuple[list[ToolCall], str]:
    """Extract Hermes-style <tool_call> blocks a server left in the text.

    Returns the recovered calls plus the content with the tool-call blocks and
    any <think> reasoning removed. Malformed or truncated blocks are skipped
    rather than raised on, so a half-written tag degrades to "no calls" (and the
    planner's empty-selection fallback) instead of crashing the turn. The tag
    body is delimited by the closing tag, not by brace matching, so nested JSON
    in ``arguments`` survives intact. Name/argument extraction is delegated to
    _tool_call_fields so the AXLLM ``{"function": "<name>", ...}`` shape parses.
    """
    calls: list[ToolCall] = []
    for match in _TOOL_CALL_RE.finditer(content):
        try:
            payload = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        name, arguments = _tool_call_fields(payload)
        name = _normalize_tool_name(name)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                continue
        if name and isinstance(arguments, dict):
            calls.append(ToolCall(name=name, arguments=arguments))
    cleaned = _strip_think(_TOOL_CALL_RE.sub("", content)).strip()
    return calls, cleaned


def _apply_no_think(payload_messages: list[dict[str, object]]) -> None:
    """Inject Qwen3's ``/no_think`` soft switch so the model skips reasoning.

    Appended to the first system message (or a new one) rather than passed as an
    API parameter, so it works on edge servers that ignore
    ``chat_template_kwargs``; a no-op on models that don't recognise the token.
    """
    for message in payload_messages:
        if message.get("role") == "system":
            content = str(message.get("content") or "")
            if "/no_think" not in content:
                message["content"] = f"{content}\n/no_think".strip()
            return
    payload_messages.insert(0, {"role": "system", "content": "/no_think"})


def _usage_view(usage: object) -> dict[str, int] | None:
    """Normalize an OpenAI-style ``usage`` block to a small int-only dict.

    Keeps prompt/completion/total counts and, for reasoning models, the nested
    ``completion_tokens_details.reasoning_tokens`` — the figure that reveals
    whether a reasoning model burned its whole budget thinking before it could
    emit tool calls. Returns None when the server omits usage. ``bool`` is
    rejected explicitly since it is an ``int`` subclass and never a token count.
    """
    if not isinstance(usage, dict):
        return None
    view: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            view[key] = value
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
        if isinstance(reasoning, int) and not isinstance(reasoning, bool):
            view["reasoning_tokens"] = reasoning
    return view or None
