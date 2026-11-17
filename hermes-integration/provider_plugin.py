"""
Hermes model-provider plugin for the gemini-web-proxy.

Tells Hermes how to drive this OpenAI-compatible proxy and, crucially, how to
forward the Hermes session identity (session_id / cache_scope_id) into the
request so the proxy can KEEP ONE Gemini chat per Hermes session and resume it
on later turns. Without this the raw session id never reaches the proxy.

Install: place this file (or symlink) at
    $HERMES_HOME/plugins/model-providers/gemini-web-proxy/__init__.py
AND in EVERY Hermes profile that uses the proxy:
    $HERMES_HOME/profiles/<profile>/plugins/model-providers/gemini-web-proxy/__init__.py
A profile without its own copy silently falls back to the generic provider:
no session_id reaches the proxy, so every agent step opens a NEW Gemini chat
with the full prompt (seen live: ~2 min per step, 15 chats for one task).
and register the provider in Hermes config:

    custom_providers:
      - name: gemini-web-proxy
        base_url: http://127.0.0.1:9658/v1
        api_key: <PROXY_API_KEY if required>

Reasoning level: this profile shadows the generic custom-provider profile, whose
hook is the only place a custom provider emits the reasoning effort. So
build_api_kwargs_extras also sends top-level ``reasoning_effort`` (low / medium /
high) when the session has one (/reasoning <level>, the Desktop effort dial,
agent.reasoning_effort). Unset -> the field is omitted (proxy default,
GEMINI_WEB_EFFORT). See docs/MODELS.md.
"""
from providers import register_provider
from providers.base import ProviderProfile

# the proxy understands low / medium / high only
_EFFORT_TO_WIRE = {
    "none": "low", "minimal": "low", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high", "ultra": "high",
}


def _wire_effort(reasoning_config):
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled", True) is False:
        return "low"  # Gemini cannot switch thinking off; the lowest level is the closest
    effort = str(reasoning_config.get("effort") or "").strip().lower()
    return _EFFORT_TO_WIRE.get(effort)


class GeminiWebProxyProfile(ProviderProfile):
    def build_extra_body(self, *, session_id=None, **context):
        body = {}
        if session_id:
            body["session_id"] = session_id
        cache_scope = context.get("cache_scope_id") or session_id
        if cache_scope:
            body["cache_scope_id"] = cache_scope
        return body

    def build_api_kwargs_extras(self, *, session_id=None, cache_scope_id=None, **context):
        body = {}
        sid = cache_scope_id or session_id
        if sid:
            body["session_id"] = sid
        top_level = {}
        effort = _wire_effort(context.get("reasoning_config"))
        if effort:
            top_level["reasoning_effort"] = effort
        return body, top_level


gemini_web_proxy = GeminiWebProxyProfile(
    name="gemini-web-proxy",
    display_name="Gemini Web Proxy",  # provider row label in the picker
    aliases=("gemini-web",),
    env_vars=("GEMINI_WEB_PROXY_API_KEY",),
    base_url="http://127.0.0.1:9658/v1",
)

register_provider(gemini_web_proxy)
