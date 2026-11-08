"""
Small helper used by bot.sh / bot.ps1 to manage user_data/bot_settings.json.

It runs inside the freqtrade Docker image, so the user does not need Python
installed locally. Only the standard library is used.
"""

import getpass
import json
import secrets
import sys
from pathlib import Path


USER_DATA = Path("/freqtrade/user_data")
TEMPLATE = Path("/easy-tools/bot_settings.template.json")
SETTINGS = USER_DATA / "bot_settings.json"


def load() -> dict:
    with SETTINGS.open(encoding="utf-8") as f:
        return json.load(f)


def save(cfg: dict) -> None:
    SETTINGS.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    SETTINGS.chmod(0o600)


def init() -> None:
    if SETTINGS.exists():
        print("Setarile exista deja (user_data/bot_settings.json).")
        return
    text = TEMPLATE.read_text(encoding="utf-8")
    password = secrets.token_urlsafe(12)
    text = (
        text.replace("__JWT_SECRET__", secrets.token_hex(32))
        .replace("__WS_TOKEN__", secrets.token_urlsafe(24))
        .replace("__UI_PASSWORD__", password)
    )
    SETTINGS.write_text(text, encoding="utf-8")
    SETTINGS.chmod(0o600)
    for sub in ("logs", "data", "backtest_results"):
        (USER_DATA / sub).mkdir(exist_ok=True)
    print("Setari create. Datele tale de logare in platforma web:")
    print("   utilizator: admin")
    print(f"   parola:     {password}")


def show_login() -> None:
    api = load()["api_server"]
    print(f"   utilizator: {api['username']}")
    print(f"   parola:     {api['password']}")


def set_keys() -> None:
    cfg = load()
    print(f"Exchange curent: {cfg['exchange']['name']}")
    name = input("Exchange (Enter = pastreaza): ").strip().lower()
    if name:
        cfg["exchange"]["name"] = name
    key = getpass.getpass("API key (nu se afiseaza): ").strip()
    secret = getpass.getpass("API secret (nu se afiseaza): ").strip()
    if not key or not secret:
        print("Cheile nu au fost schimbate (camp gol).")
        return
    cfg["exchange"]["key"] = key
    cfg["exchange"]["secret"] = secret
    save(cfg)
    print("Chei salvate. Botul ramane in modul DEMO pana rulezi comanda 'real'.")


def set_mode(live: bool) -> None:
    cfg = load()
    if live:
        if not cfg["exchange"].get("key") or not cfg["exchange"].get("secret"):
            print("Mai intai adauga cheile API cu comanda 'chei'.")
            sys.exit(1)
        print("ATENTIE: in modul REAL botul tranzactioneaza cu banii tai reali.")
        print("Poti pierde bani. Testeaza intai in modul DEMO si cu backtest.")
        if input("Scrie DA ca sa confirmi: ").strip() != "DA":
            print("Anulat. Botul ramane in modul DEMO.")
            return
    cfg["dry_run"] = not live
    save(cfg)
    print("Mod setat: " + ("REAL" if live else "DEMO (bani virtuali)"))


def status() -> None:
    cfg = load()
    print("Mod:       " + ("DEMO (bani virtuali)" if cfg["dry_run"] else "REAL"))
    print(f"Exchange:  {cfg['exchange']['name']}")
    print("Perechi:   " + ", ".join(cfg["exchange"]["pair_whitelist"]))
    print(f"Max trades deschise: {cfg['max_open_trades']}")


COMMANDS = {
    "init": init,
    "login": show_login,
    "keys": set_keys,
    "live": lambda: set_mode(True),
    "demo": lambda: set_mode(False),
    "status": status,
}


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print("Utilizare: settings.py " + "|".join(COMMANDS))
        sys.exit(2)
    COMMANDS[sys.argv[1]]()
