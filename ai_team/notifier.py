"""
Telegram notifications for the paper-trading system (optional).

  python -m ai_team.notifier setup     # once: asks for the bot token, finds your chat, sends a test
  python -m ai_team.notifier report    # send the weekly report now
  python -m ai_team.notifier run       # started by the supervisor: trades + weekly report

The bot token is a credential. It is stored only on this computer, in
~/.ai-trading-desk/telegram.json (outside the repository, so updating the bot keeps it),
and is never logged, printed or sent anywhere except api.telegram.org. The notifier only
reads the gateway's public decisions and portfolio; it cannot submit or change anything.
Messages are in Romanian because they are for the person running the bot.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import readiness
from .fsutil import atomic_write_text
from .gateway_client import GatewayClient
from .history import CandleStore
from .llm_review import week_summary, write_review
from .state import DEFAULT_STATE_DIR, StateStore
from .track_record import SignalLedger
from .tuning import Tuner


log = logging.getLogger("ai_team.notifier")

CONFIG_DIR = Path(os.environ.get("AI_TEAM_CONFIG_DIR") or Path.home() / ".ai-trading-desk")
API = "https://api.telegram.org"
POLL_S = 30
REPORT_EVERY = timedelta(days=7)
MAX_SEEN = 500
PROBLEM_GRACE_S = 300  # a problem must last 5 minutes before it is reported (restarts are quick)
RUNNER_SILENT_S = 600
MARKET_STALE_S = 300
DRAWDOWN_WARN = -0.08
DAILY_HOUR = 9


class TelegramError(RuntimeError):
    """Telegram rejected a call or could not be reached (message never contains the token)."""


def config_file() -> Path:
    return CONFIG_DIR / "telegram.json"


def load_config(path: Path | None = None) -> dict[str, str] | None:
    try:
        cfg = json.loads((path or config_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(cfg, dict) and cfg.get("token") and cfg.get("chat_id"):
        return {"token": str(cfg["token"]), "chat_id": str(cfg["chat_id"])}
    return None


def save_config(token: str, chat_id: str, path: Path | None = None) -> Path:
    path = path or config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"token": token, "chat_id": chat_id}))
    try:
        path.chmod(0o600)  # owner only, where the OS supports it
    except OSError:
        pass
    return path


class Telegram:
    def __init__(self, token: str, chat_id: str = "", opener: Any = None, timeout: float = 20.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text

    def call(self, method: str, **params: Any) -> Any:
        url = f"{API}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except (OSError, ValueError) as e:
            raise TelegramError(self._scrub(f"Telegram {method} failed: {type(e).__name__}: {e}")) from None
        if not body.get("ok"):
            raise TelegramError(self._scrub(f"Telegram {method} refused: {body.get('description')}"))
        return body.get("result")

    def send(self, text: str) -> None:
        self.call("sendMessage", chat_id=self.chat_id, text=text[:4000], disable_web_page_preview="true")

    def find_chat_id(self) -> str | None:
        """The chat of the most recent message sent to the bot (the user's /start)."""
        for update in reversed(self.call("getUpdates") or []):
            msg = update.get("message") or update.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
        return None


# ---------------- messages ----------------
def _money(v: float | None) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _signed(v: float) -> str:
    return f"{v:+,.2f}"


def describe_decision(d: dict[str, Any], positions: dict[str, Any]) -> str | None:
    """A message for an executed paper trade, or None for anything else."""
    fill = d.get("fill")
    if not d.get("approved") or not fill:
        return None
    pair = d.get("pair") or fill.get("pair")
    if fill.get("side") == "buy":
        pos = positions.get(pair) or {}
        stake = fill["price"] * fill["amount"]
        lines = [
            f"🟢 CUMPĂRAT (pe hârtie) {pair}",
            f"Preț: {_money(fill['price'])} · Sumă: {_money(stake)} USDT",
        ]
        if pos.get("stop_loss") or pos.get("take_profit"):
            lines.append(
                f"Stop-loss: {_money(pos.get('stop_loss'))} · Take-profit: {_money(pos.get('take_profit'))}"
            )
        if pos.get("strategy"):
            lines.append(f"Strategie: {pos['strategy']}")
        return "\n".join(lines)
    if fill.get("side") == "sell":
        pnl = float(fill.get("pnl") or 0.0)
        reason = "; ".join(d.get("reasons") or [])
        if "take profit" in reason:
            why = "take-profit atins"
        elif "stop loss" in reason:
            why = "stop-loss atins"
        else:
            why = "ieșire"
        icon = "✅" if pnl > 0 else "🔻"
        return (
            f"{icon} VÂNDUT (pe hârtie) {pair}\n"
            f"Preț: {_money(fill['price'])} · Profit: {_signed(pnl)} USDT\n"
            f"Motiv: {why}"
        )
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def weekly_report(
    portfolio: dict[str, Any],
    track_record: dict[str, dict[str, Any]],
    now: datetime | None = None,
    starting_equity: float | None = None,
    ready: dict[str, Any] | None = None,
) -> str:
    now = now or datetime.now(UTC)
    since = now - REPORT_EVERY
    equity = portfolio.get("equity")
    history = [(t, h["equity"]) for h in portfolio.get("history") or [] if (t := _parse(h.get("ts")))]
    before = [e for t, e in history if t <= since]
    week_start = before[-1] if before else (history[0][1] if history else None)
    closed = [r for r in portfolio.get("realised") or [] if (t := _parse(r.get("ts"))) and t > since]
    wins = sum(1 for r in closed if r["pnl"] > 0)
    lines = ["📊 RAPORT SĂPTĂMÂNAL (pe hârtie, bani virtuali)"]
    if equity is not None:
        lines.append(f"Valoare cont: {_money(equity)} USDT")
        if week_start:
            change = (equity / week_start - 1) * 100
            lines.append(f"Săptămâna asta: {_signed(equity - week_start)} USDT ({change:+.2f}%)")
        if starting_equity:
            lines.append(f"De la început: {(equity / starting_equity - 1) * 100:+.2f}%")
    if closed:
        pnl = sum(r["pnl"] for r in closed)
        lines.append(f"Tranzacții închise: {len(closed)} ({wins} pe plus) · Profit: {_signed(pnl)} USDT")
        by_strategy: dict[str, float] = {}
        for r in closed:
            by_strategy[r.get("strategy") or "?"] = by_strategy.get(r.get("strategy") or "?", 0.0) + r["pnl"]
        lines += [f"  · {s}: {_signed(v)} USDT" for s, v in sorted(by_strategy.items(), key=lambda x: -x[1])]
    else:
        lines.append("Tranzacții închise: niciuna săptămâna asta")
    unrealised = portfolio.get("unrealised") or {}
    if unrealised:
        lines.append(f"Poziții deschise: {len(unrealised)}")
        lines += [f"  · {p}: {_signed(u['pnl'])} USDT" for p, u in sorted(unrealised.items())]
    ranked = sorted(track_record.items(), key=lambda x: (-x[1]["weight"], -x[1]["signals"]))
    scored = [(a, s) for a, s in ranked if s["signals"] >= 3]
    if scored:
        lines.append("Agenții cu cele mai bune semnale:")
        lines += [
            f"  · {a}: {s['hit_rate'] * 100:.0f}% corecte din {s['signals']} (vot x{s['weight']:.2f})"
            for a, s in scored[:3]
        ]
    if ready:
        missing = [c["name"] for c in ready["criteria"] if c["ok"] is not True]
        lines.append(f"Pregătire pentru bani reali: {ready['met']}/{ready['total']} criterii îndeplinite")
        if missing:
            lines.append("  Lipsesc: " + "; ".join(missing))
    return "\n".join(lines)


# ---------------- the running notifier ----------------
class Notifier:
    def __init__(
        self,
        gateway: GatewayClient,
        state_dir: Path,
        telegram: Telegram | None = None,
        review_writer: Any = None,
    ) -> None:
        self.gateway = gateway
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / "notifier.json"
        self.telegram = telegram
        self.review_writer = review_writer or write_review
        self.daily_hour = DAILY_HOUR

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict[str, Any]) -> None:
        atomic_write_text(self.state_file, json.dumps(st))

    def tick(self, now: datetime | None = None) -> int:
        """Send messages for new paper trades and the weekly report when due; returns messages sent."""
        if self.telegram is None:
            return 0
        now = now or datetime.now(UTC)
        st = self._state()
        try:
            decisions = self.gateway.decisions()
        except (OSError, ValueError):
            decisions = None  # gateway down: reported by check_health below
        key = lambda d: f"{d.get('ts')}|{d.get('proposal_id')}"  # noqa: E731
        sent = 0
        if "seen" not in st:
            if decisions is None:
                return 0
            # First run: announce, and do not replay trades that happened before setup.
            st = {"seen": [key(d) for d in decisions][-MAX_SEEN:], "last_report": now.isoformat()}
            self.telegram.send(
                "✅ Notificările botului sunt pornite.\n"
                "Primești un mesaj la fiecare tranzacție pe hârtie și un raport în fiecare săptămână."
            )
            self._save(st)
            return 1
        seen = set(st["seen"])
        positions = (self.gateway.portfolio() or {}).get("positions") or {}
        try:
            sent += self.check_health(st, now)
            sent += self.daily_status(st, now)
            for d in decisions or []:
                k = key(d)
                if k in seen:
                    continue
                text = describe_decision(d, positions)
                if text:
                    self.telegram.send(text)  # may raise: then this decision is retried next time
                    sent += 1
                seen.add(k)
                st["seen"].append(k)
            last = _parse(st.get("last_report")) or now
            if now - last >= REPORT_EVERY:
                self.send_report(now)
                st["last_report"] = now.isoformat()
                sent += 1
        finally:
            st["seen"] = st["seen"][-MAX_SEEN:]
            self._save(st)
        return sent

    # ---------------- problems and the daily "all good" ----------------
    def problems(self, now: datetime) -> dict[str, str]:
        """Current problems, keyed so each one is reported once and cleared once."""
        found: dict[str, str] = {}
        if not self.gateway.health().get("ok"):
            found["gateway"] = "Controlul de risc (gateway) nu răspunde."
        runner = _read_json(self.state_dir / "runner.json")
        beat = _parse((runner or {}).get("heartbeat"))
        if runner is None or beat is None:
            found["runner"] = "Partea care rulează agenții (runner) nu a pornit."
        elif (now - beat).total_seconds() > RUNNER_SILENT_S:
            found["runner"] = f"Runner-ul nu mai răspunde de {(now - beat).total_seconds() / 60:.0f} minute."
        else:
            last = (runner or {}).get("last_cycle") or {}
            if last and not last.get("ok"):
                found["cycle"] = f"Ultimul ciclu al agenților a eșuat: {str(last.get('error'))[:200]}"
        market = _read_json(self.state_dir / "market.json")
        mts = _parse((market or {}).get("ts"))
        if (
            market is None
            or mts is None
            or not market.get("ok")
            or (now - mts).total_seconds() > MARKET_STALE_S
        ):
            found["market"] = "Prețurile de la Kraken nu mai vin (internet sau Kraken indisponibil)."
        pf = self.gateway.portfolio() or {}
        dd = pf.get("drawdown_pct")
        if dd is not None and dd <= DRAWDOWN_WARN:
            found["drawdown"] = (
                f"Contul de test a scăzut {dd * 100:.1f}% de la maxim (limita de siguranță: 10%)."
            )
        return found

    def check_health(self, st: dict[str, Any], now: datetime) -> int:
        """Alert when a problem has lasted PROBLEM_GRACE_S; say so once it is gone."""
        current = self.problems(now)
        active = st.setdefault("problems", {})
        sent = 0
        for k, text in current.items():
            p = active.setdefault(k, {"since": now.isoformat(), "sent": False})
            p["text"] = text
            since = _parse(p["since"]) or now
            if not p["sent"] and (now - since).total_seconds() >= PROBLEM_GRACE_S:
                self.telegram.send(
                    f"⚠️ PROBLEMĂ: {text}\n"
                    "Botul încearcă singur să se repare. Dacă durează, scrie-i lui Claude."
                )
                p["sent"] = True
                sent += 1
        for k in [k for k in active if k not in current]:
            if active[k].get("sent"):
                self.telegram.send(f"✅ Rezolvat: {active[k].get('text', k)}")
                sent += 1
            del active[k]
        return sent

    def daily_status(self, st: dict[str, Any], now: datetime) -> int:
        """One short "all good" message a day, from DAILY_HOUR local time."""
        local = now.astimezone()
        today = local.date().isoformat()
        if local.hour < self.daily_hour or st.get("last_daily") == today:
            return 0
        st["last_daily"] = today
        pf = self.gateway.portfolio() or {}
        agents = _read_json(self.state_dir / "agents.json") or {}
        online = sum(1 for a in agents.values() if a.get("online"))
        problems = [p.get("text") for p in (st.get("problems") or {}).values() if p.get("sent")]
        head = "✅ Botul merge." if not problems else "⚠️ Botul merge, dar sunt probleme active:"
        lines = [head]
        lines += [f"  · {p}" for p in problems]
        if pf.get("equity") is not None:
            open_n = len(pf.get("positions") or {})
            lines.append(f"Cont (pe hârtie): {_money(pf['equity'])} USDT · poziții deschise: {open_n}")
        lines.append(f"Agenți online: {online}/{len(agents) or 50}")
        lines.append("Dacă într-o zi nu primești acest mesaj, verifică dacă e pornit calculatorul.")
        self.telegram.send("\n".join(lines))
        return 1

    def send_report(self, now: datetime | None = None) -> None:
        if self.telegram is None:
            raise TelegramError("Telegram is not configured")
        pf = self.gateway.portfolio() or {}
        try:
            start = float(self.gateway.limits().get("starting_equity"))
        except (OSError, TypeError, ValueError):
            start = None
        scores = SignalLedger(self.state_dir / "signal_ledger.json").scores()
        try:
            runner = json.loads((self.state_dir / "runner.json").read_text())
        except (OSError, ValueError):
            runner = None
        reviews = {
            d["pair"]: d
            for d in StateStore(self.state_dir).decisions(400)
            if d.get("type") == "strategy_review"
        }
        ready = readiness.evaluate(
            pf, start, runner, reviews, CandleStore(self.state_dir / "history").load("BTC/USDT", "4h")
        )
        self.telegram.send(weekly_report(pf, scores, now, start, ready))
        # Optional second message: Claude's written review (only with an Anthropic key).
        summary = week_summary(pf, ready, scores, reviews, Tuner(self.state_dir).load())
        text = self.review_writer(summary)
        if text:
            self.telegram.send(
                "🧠 Analiza săptămânii (Claude, doar propuneri - nu se aplică nimic automat):\n" + text
            )

    def run_forever(self) -> None:
        configured_logged = None
        while True:
            cfg = load_config()
            if cfg is None:
                if configured_logged is not False:
                    log.info("Telegram not configured (%s); waiting", config_file())
                    configured_logged = False
                self.telegram = None
                time.sleep(60)
                continue
            if configured_logged is not True:
                log.info("Telegram configured; sending trade notifications")
                configured_logged = True
            self.telegram = Telegram(cfg["token"], cfg["chat_id"])
            try:
                self.tick()
            except (TelegramError, OSError, ValueError) as e:  # gateway down or Telegram unreachable
                log.warning("notifier: %s", e)
            time.sleep(POLL_S)


def setup(input_fn=None, out=print, telegram_factory=Telegram, secret_fn=None) -> int:
    # The token is read without echo, so it never appears on screen or in a screenshot.
    if input_fn is None:
        input_fn, secret_fn = input, secret_fn or getpass.getpass
    secret_fn = secret_fn or input_fn
    out("Configurare notificari Telegram (o singura data).")
    out("1. In Telegram cauta @BotFather, scrie /newbot si urmeaza pasii.")
    out("2. BotFather iti da un token, de forma 123456789:ABC...")
    out("   Tokenul NU se vede pe ecran cand il lipesti (click dreapta) - e normal.")
    token = secret_fn("Lipeste tokenul aici si apasa Enter: ").strip()
    if ":" not in token:
        out("Tokenul nu pare corect (trebuie sa contina ':'). Incearca din nou.")
        return 1
    tg = telegram_factory(token)
    try:
        me = tg.call("getMe")
    except TelegramError as e:
        out(f"Tokenul nu a fost acceptat de Telegram: {e}")
        return 1
    out(f"3. Deschide in Telegram botul @{me.get('username')} si scrie-i /start")
    input_fn("   Dupa ce ai trimis /start, apasa Enter aici...")
    chat_id = tg.find_chat_id()
    if not chat_id:
        out("Nu am gasit mesajul tau. Trimite /start botului si ruleaza din nou.")
        return 1
    tg.chat_id = chat_id
    tg.send("✅ Gata! Botul de trading iti va trimite aici tranzactiile (pe hartie) si raportul saptamanal.")
    path = save_config(token, chat_id)
    out(f"Gata. Setarile sunt salvate doar pe acest calculator: {path}")
    out("Ai primit un mesaj de test in Telegram. Notificarile pornesc in maxim un minut.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_team.notifier")
    ap.add_argument("command", choices=["run", "setup", "report"])
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows consoles may lack some characters
    if a.command == "setup":
        return setup()
    notifier = Notifier(GatewayClient(a.gateway), a.state_dir)
    if a.command == "report":
        cfg = load_config()
        if cfg is None:
            print("Telegram nu este configurat. Ruleaza intai: python -m ai_team.notifier setup")
            return 1
        notifier.telegram = Telegram(cfg["token"], cfg["chat_id"])
        notifier.send_report()
        print("Raportul a fost trimis.")
        return 0
    notifier.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
