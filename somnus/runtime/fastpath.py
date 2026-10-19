"""Fast routes: utterances answered without the LLM.

The vendor assistants' speed advantage over an LLM agent is mostly this:
"打开微信" never reaches a language model - an on-device router maps it
straight to the platform launch call in milliseconds. This module gives
Somnus the same first layer. Anything it cannot route still goes to the
LLM, which keeps the long tail covered.

Two layers, tried in order:

1. **记忆** - an utterance the LLM has already solved is replayed from
   ``~/.somnus/fastpath.json`` (the first action of the recorded plan, e.g.
   a deep link or launch). A GUI move that depends on live screen state is
   not cached: only plans that start with a system-level action are.
2. **规则** - deterministic intents (打开/launch an app by label) resolved
   against the installed-app list.

Usage::

    from somnus.runtime.fastpath import try_route
    result = try_route("打开微信", session)
    if result is None:
        ... LLM ...
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Deep links for the pages people actually ask for. Routing through these
#: skips whole UI-navigation conversations; the URIs are the apps' own
#: public schemes. Unknown apps fall through to plain launch.
DEEPLINKS: dict[str, str] = {
    "美团外卖": "imeituan://www.meituan.com/food?latlng=0,0",
    "淘宝": "taobao://home",
    "天猫": "tmall://page.tmall.appcenter",
    "京东": "openapp.jdmobile://virtual?params=%7B%22category%22%3A%22jump%22%2C%22des%22%3A%22homepage%22%7D",
    "支付宝": "alipay://platformapi/startapp?appId=20000056",
    "闲鱼": "fleamarket://index",
    "小红书": "xhsdiscover://",
    "携程": "ctrip://wireless",
    "网易云音乐": "orpheus://",
    "哔哩哔哩": "bilibili://root/home",
    "微博": "sinaweibo://splash",
}

_LAUNCH_RE = re.compile(
    r"^(?:帮我\s*)?(?:打开|启动|开启|open|launch)\s*[一下]?\s*(?P<target>\S+?)\s*[吧。.]?$",
    re.IGNORECASE,
)

_CACHE_PATH = Path.home() / ".somnus" / "fastpath.json"


def _load_cache() -> dict[str, str]:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember(utterance: str, first_action: str) -> None:
    """Record the first action of a solved plan, for instant replay.

    Only system-level first actions are cached: an ``APP launch/open`` or a
    ``SWITCH`` is about *reaching* a screen and does not depend on the tree
    that was live when it was learned. A ``CALL`` is state-dependent and
    would mis-fire from stale context.
    """
    head = first_action.split("(", 1)[0].strip().upper()
    if not (head.startswith("APP ") or head.startswith("SWITCH ")
            or head.startswith("SYSTEM ")):
        return
    cache = _load_cache()
    cache[utterance.strip()] = first_action
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def try_route(utterance: str, session) -> str | None:
    """Route ``utterance`` without the LLM; None means "needs the LLM".

    Returns the SUI-Text the caller would have shown for the action.
    """
    text = (utterance or "").strip()
    if not text:
        return None

    # 1. Replay a remembered plan (exact utterance match).
    cached = _load_cache().get(text)
    if cached:
        result = session.dispatch(cached)
        if not result.startswith("ERR"):
            return f"{result}\n~ fastpath: replayed {cached!r}"
        # The remembered action failed (app uninstalled, scheme dead):
        # fall through to the rules and the LLM, and let the next success
        # overwrite the memory.

    # 2. Deterministic "open <app>" intent.
    m = _LAUNCH_RE.match(text)
    if not m:
        return None
    target = m.group("target")

    # A deep link beats a bare launch: it lands one page deeper.
    link = DEEPLINKS.get(target)
    if link:
        result = session.dispatch(f"APP open {link}")
        if not result.startswith("ERR"):
            return f"{result}\n~ fastpath: deeplink {target}"
        # Scheme not registered on this phone (app missing): try launch.

    lister = getattr(session.provider, "list_apps", None)
    if lister is None:
        return None
    want = target.casefold()
    hit = next(
        (
            a for a in lister()
            if (a.get("label") or "").casefold() == want
            or a.get("package") == target
        ),
        None,
    )
    if hit is None:
        # Substring fallback only when unambiguous.
        hits = [
            a for a in lister()
            if want in (a.get("label") or "").casefold()
        ]
        if len(hits) == 1:
            hit = hits[0]
        else:
            return None  # ambiguous or unknown: the LLM can ask/decide
    return session.dispatch(f'APP launch "{hit.get("label")}"') + (
        "\n~ fastpath: launch"
    )
