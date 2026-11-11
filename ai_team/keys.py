"""
Save the optional API keys on this computer (never in the repository).

  python -m ai_team.keys setup     # asks for an Anthropic key and a FRED key; Enter skips either
  python -m ai_team.keys status    # shows which keys are configured (never the keys themselves)

* Anthropic (console.anthropic.com): Claude scores news headlines for the sentiment analyst.
  Paid per use; at most one request an hour.
* FRED (fredaccount.stlouisfed.org, free): US rates, inflation, yields and the dollar for
  the macro-economic analyst.

Keys are checked before they are saved and stored in ~/.ai-trading-desk/ (see
ai_team.local_config). Prompts are in Romanian because the person running the bot reads them.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from . import local_config
from .llm_news import DEFAULT_MODEL


def check_anthropic(key: str) -> str | None:
    """None if the key works, else a short reason. Listing models is free."""
    import anthropic

    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=30).models.list(limit=1)
    except anthropic.AuthenticationError:
        return "cheia nu este valida"
    except anthropic.APIError as e:
        return f"{type(e).__name__}"
    return None


def check_fred(key: str) -> str | None:
    url = (
        "https://api.stlouisfed.org/fred/series/observations?file_type=json&limit=1"
        f"&series_id=FEDFUNDS&api_key={urllib.parse.quote(key)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - fixed https FRED endpoint
            ok = "observations" in json.loads(r.read())
    except (OSError, ValueError):
        return "cheia nu este valida sau FRED nu raspunde"
    return None if ok else "raspuns neasteptat de la FRED"


def setup(
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    checks: dict[str, Callable[[str], str | None]] | None = None,
) -> int:
    checks = checks or {"anthropic": check_anthropic, "fred": check_fred}
    out("Chei optionale pentru bot. Apasa doar Enter ca sa sari peste una.")
    out("")
    out("1) Claude citeste stirile (cheie de la console.anthropic.com, cost mic pe utilizare).")
    key = input_fn("   Cheia Anthropic (incepe cu sk-ant-): ").strip()
    if key:
        problem = checks["anthropic"](key)
        if problem:
            out(f"   Nu am salvat cheia Anthropic: {problem}.")
        else:
            out(f"   Model implicit: {DEFAULT_MODEL}. Pentru cost mai mic poti scrie claude-haiku-5-5.")
            model = input_fn("   Model (Enter = implicit): ").strip() or DEFAULT_MODEL
            local_config.save("anthropic", {"api_key": key, "model": model})
            out(f"   Salvat ({model}).")
    out("")
    out("2) Date economice SUA de la FRED (cheie gratuita de la fredaccount.stlouisfed.org).")
    key = input_fn("   Cheia FRED (32 de caractere): ").strip()
    if key:
        problem = checks["fred"](key)
        if problem:
            out(f"   Nu am salvat cheia FRED: {problem}.")
        else:
            local_config.save("fred", {"api_key": key})
            out("   Salvat.")
    out("")
    out(f"Cheile sunt doar pe acest calculator, in {local_config.config_dir()}.")
    out("Botul le foloseste de la urmatorul ciclu (maxim 5 minute), fara repornire.")
    return 0


def status() -> dict[str, Any]:
    a = local_config.load("anthropic") or {}
    return {
        "anthropic": bool(a.get("api_key")),
        "anthropic_model": a.get("model") if a.get("api_key") else None,
        "fred": bool((local_config.load("fred") or {}).get("api_key")),
        "telegram": local_config.load("telegram") is not None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_team.keys")
    ap.add_argument("command", choices=["setup", "status"])
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if a.command == "setup":
        return setup()
    print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
