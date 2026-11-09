# Trading bot ușor 🤖📈

Un bot de trading crypto **gata de folosit**, cu **platformă web** (grafice,
tranzacții, profit, backtest, pornire/oprire) — totul dintr-un singur folder.
Este construit pe [freqtrade](https://www.freqtrade.io) și interfața lui web
FreqUI.

> ⚠️ **Important:** botul pornește în modul **DEMO** (bani virtuali, 1000 USDT).
> Tradingul cu bani reali implică riscul de a pierde bani. Nicio strategie nu
> garantează profit. Testează întâi în DEMO și cu backtest.

---

## 1. Ce îți trebuie (o singură dată)

1. Instalează **Docker Desktop**: <https://docs.docker.com/get-docker/>
2. Pornește Docker Desktop (trebuie să ruleze în fundal).
3. Descarcă acest proiect (butonul verde **Code → Download ZIP** pe GitHub) și
   dezarhivează-l.

Atât. Nu trebuie să instalezi Python sau altceva.

## 2. Pornire

### Windows

Intră în folderul `easy-start` și dă **dublu-click pe `START.bat`**.

Se deschide automat platforma în browser: <http://127.0.0.1:8080>

Pentru oprire: dublu-click pe **`STOP.bat`**.

### Mac / Linux

Deschide un terminal în folderul `easy-start` și scrie:

```bash
./bot.sh start
```

Apoi deschide în browser <http://127.0.0.1:8080>.

### Logarea în platformă

La prima pornire scriptul îți afișează **utilizatorul** (`admin`) și
**parola** generată automat. Dacă o uiți:

```bash
./bot.sh parola        # Windows: .\bot.ps1 parola
```

În pagina web apasă **Login**, lasă adresa `http://127.0.0.1:8080` și
introdu utilizatorul și parola.

## 3. De pe telefon 📱

Botul **nu se instalează pe telefon**: rulează pe calculator (sau pe un
server), iar telefonul doar îl controlează. Ai două variante:

### Varianta A — platforma web pe telefon (același Wi-Fi)

1. Pe calculator: dublu-click pe **`TELEFON.bat`** (Windows) sau
   `./bot.sh telefon` (Mac/Linux).
2. Scriptul îți arată o adresă de forma `http://192.168.x.x:8080`.
3. Pe telefon, conectat la **același Wi-Fi**, deschide adresa în browser și
   loghează-te cu `admin` și parola ta (la „API Url” pune aceeași adresă).

Dacă pe Windows nu se încarcă, permite Docker în Windows Firewall pentru
rețele private. Revii la „doar pe calculator” cu `local`.
**Nu** deschide portul 8080 în router.

### Varianta B — Telegram (de oriunde, și pe date mobile)

1. Pe calculator: `telegram` (Windows: `.\bot.ps1 telegram`).
2. Urmează pașii afișați: token de la [@BotFather](https://t.me/BotFather) și
   chat ID de la [@userinfobot](https://t.me/userinfobot).
3. Scrie `/help` botului tău în Telegram. Comenzi utile: `/status`,
   `/profit`, `/balance`, `/daily`, `/start`, `/stop`.

Primești și mesaj la fiecare cumpărare/vânzare. Calculatorul trebuie să fie
pornit; pentru 24/7 folosește un server (VPS) sau un mini-PC/Raspberry Pi.

## 4. Ce poți face în platforma web

| Secțiune | Ce face |
|----------|---------|
| **Trade** | Tranzacțiile deschise, profitul, butoane de Start/Stop, închidere manuală a unei poziții |
| **Dashboard** | Profit total, profit pe zile, câștiguri/pierderi |
| **Chart** | Grafice cu prețul, intrările și ieșirile botului, EMA și RSI |
| **Backtest** | Testezi strategia pe date din trecut, fără bani |
| **Logs** | Ce face botul în timp real |

## 5. Toate comenzile

Pe Windows folosește `.\bot.ps1 <comanda>` în PowerShell; pe Mac/Linux
`./bot.sh <comanda>`.

| Comandă | Ce face |
|---------|---------|
| `start` | Pornește botul și platforma web |
| `stop` | Oprește botul |
| `restart` | Repornește botul (după ce schimbi setări) |
| `status` | Arată modul (DEMO/REAL), exchange-ul și perechile |
| `loguri` | Arată ce face botul în timp real |
| `parola` | Arată datele de logare în platformă |
| `backtest` | Testează strategia pe ultimele 90 de zile |
| `chei` | Adaugă cheile API de la exchange (Binance, Kraken, …) |
| `real` | Trece pe bani **reali** (cere să scrii `DA`) |
| `demo` | Revine la bani virtuali |
| `update` | Actualizează botul la ultima versiune |
| `telefon` | Platforma web accesibilă de pe telefon (același Wi-Fi) |
| `local` | Platforma doar pe acest calculator (implicit) |
| `telegram` | Controlezi botul din Telegram, de oriunde |

## 6. Strategia inclusă: `StrategieUsoara`

O strategie simplă, care urmează trendul, pe grafic de 15 minute:

- **Cumpără** când media mobilă rapidă (EMA 9) trece peste cea lentă (EMA 21),
  doar dacă prețul e peste EMA 200 (trend de creștere) și RSI nu e prea mare.
- **Vinde** când EMA 9 coboară sub EMA 21 sau RSI trece de 75.
- **Protecție:** stop-loss la −5 %, trailing stop care blochează profitul după
  +2 %, take-profit automat (6 % / 3 % după 2 ore / 1 % după 6 ore).

Fișierul este în `user_data/strategies/StrategieUsoara.py` — numerele de sus
pot fi modificate ușor.

## 7. Setări simple

Setările sunt în `user_data/bot_settings.json` (creat la prima pornire).
Cele mai utile:

| Setare | Implicit | Ce înseamnă |
|--------|----------|-------------|
| `dry_run_wallet` | `1000` | Câți USDT virtuali ai în DEMO |
| `max_open_trades` | `3` | Câte tranzacții simultan |
| `pair_whitelist` | BTC, ETH, SOL, XRP, ADA | Ce monede tranzacționează |
| `exchange.name` | `binance` | Exchange-ul folosit |

După orice modificare: `restart`.

## 8. Trecerea pe bani reali (doar când ești sigur)

1. Pe exchange (ex. Binance) creează o **cheie API** cu drept de
   **trading spot**. **Nu** activa dreptul de retragere (withdraw).
   Dacă poți, restricționează cheia la IP-ul tău.
2. `chei` → introdu cheia și secretul (nu se afișează pe ecran).
3. `real` → scrie `DA` pentru confirmare.
4. Începe cu o sumă mică.

Revii oricând la bani virtuali cu `demo`.

## 9. Notificări pe telefon

Vezi secțiunea 3, varianta B (Telegram).

## 10. Securitate

- Implicit, platforma web este accesibilă **doar de pe calculatorul tău**
  (`127.0.0.1`); cu `telefon` devine accesibilă în rețeaua ta Wi-Fi, protejată
  de parolă.
- Parola, cheile API și baza de date nu sunt urcate pe GitHub
  (vezi `.gitignore`). Nu trimite nimănui fișierul `bot_settings.json`.

## 11. Probleme frecvente

| Problemă | Soluție |
|----------|---------|
| „Docker nu rulează” | Pornește Docker Desktop și așteaptă să fie verde |
| Pagina nu se deschide | Așteaptă ~30 secunde după `start`, apoi `loguri` |
| „Could not load markets” | Exchange-ul nu e accesibil din țara/rețeaua ta — schimbă `exchange.name` (ex. `kraken`, `bybit`, `okx`) |
| Pe telefon nu se încarcă | Telefonul trebuie să fie pe același Wi-Fi; rulează `telefon`, nu `start`; verifică firewall-ul; sau folosește Telegram |
| Pe Windows scriptul nu pornește | Folosește `START.bat`, care permite rularea scriptului |
