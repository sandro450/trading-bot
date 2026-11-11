import os
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
ROMA = ZoneInfo("Europe/Rome")
STATE = "signals.json"

SYMBOLS = {
    "ISP.MI": "Intesa Sanpaolo",
    "UCG.MI": "UniCredit",
    "BAMI.MI": "Banco BPM",
    "ENI.MI": "Eni",
    "ENEL.MI": "Enel",
    "SPM.MI": "Saipem",
    "LDO.MI": "Leonardo",
    "RACE.MI": "Ferrari",
    "STLAM.MI": "Stellantis",
    "BRE.MI": "Brembo",
    "^VIX": "VIX",
    "GC=F": "Oro",
    "CL=F": "Petrolio WTI",
    "BZ=F": "Petrolio Brent",
    "NG=F": "Gas naturale",
}

# ---- Parametri (modificali per avere piu' o meno segnali) ----
LOOKBACK = 8        # candele da 15 min da superare (8 = 2 ore)
VOL_MULT = 1.0      # volume minimo rispetto alla media (1.0 = media)
STOP_ATR = 4      # stop largo = 4 x ATR
TP_R = 1         # take profit = 1 volte il rischio
MAX_DAILY = 8       # massimo segnali al giorno
MAX_PER_RUN = 2     # massimo segnali per ogni controllo
COOLDOWN_H = 2      # ore di pausa sullo stesso titolo
START = (9, 30)     # primo controllo (ora italiana)
END = (17, 15)      # ultimo controllo
REPORT_FROM = (17, 40)


def fmt(x):
    return f"{x:.3f}" if x < 10 else f"{x:.2f}"


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=20,
    )


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"signals": [], "reported": ""}


def save_state(st, now):
    limit = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    st["signals"] = [s for s in st["signals"] if s["date"] >= limit]
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


def get_data(sym):
    df = yf.Ticker(sym).history(period="5d", interval="15m")
    if df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROMA)
    return df


def add_indicators(df):
    c, h, l = df["Close"], df["High"], df["Low"]
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["ema20"] = c.ewm(span=20).mean()
    d = c.diff()
    gain = d.clip(lower=0).rolling(14).mean()
    loss = (-d.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)
    df["volavg"] = df["Volume"].rolling(20).mean()
    return df


def check(sym, name, now):
    df = get_data(sym)
    if len(df) < 40:
        return None
    df = df[df.index + pd.Timedelta(minutes=15) <= now]
    df = add_indicators(df)
    last = df.iloc[-1]
    if last.name.date() != now.date() or pd.isna(last["atr"]):
        return None
    prev = df.iloc[-1 - LOOKBACK:-1]
    va = last["volavg"]
    vol_ok = pd.isna(va) or va == 0 or last["Volume"] >= VOL_MULT * va
    vol_x = (last["Volume"] / va) if (not pd.isna(va) and va > 0) else 0
    c = float(last["Close"])
    risk = STOP_ATR * float(last["atr"])
    side = None
    if vol_ok and c > prev["High"].max() and c > last["ema20"] and last["rsi"] < 80:
        side = "LONG"
        stop, tp = c - risk, c + TP_R * risk
        note = f"Rottura al rialzo dei massimi delle ultime {LOOKBACK // 4} ore, compratori in controllo"
    elif vol_ok and c < prev["Low"].min() and c < last["ema20"] and last["rsi"] > 20:
        side = "SHORT"
        stop, tp = c + risk, c - TP_R * risk
        note = f"Rottura al ribasso dei minimi delle ultime {LOOKBACK // 4} ore, venditori in controllo"
    if not side:
        return None
    if vol_x:
        note += f", volumi {vol_x:.1f}x la media"
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": last.name.isoformat(),
        "sym": sym,
        "name": name,
        "side": side,
        "entry": c,
        "stop": stop,
        "tp": tp,
        "note": note,
    }


def scan(st, now):
    today = now.strftime("%Y-%m-%d")
    todays = [s for s in st["signals"] if s["date"] == today]
    sent = 0
    for sym, name in SYMBOLS.items():
        if len(todays) >= MAX_DAILY or sent >= MAX_PER_RUN:
            break
        recent = [
            s for s in st["signals"]
            if s["sym"] == sym
            and datetime.fromisoformat(s["time"]) > now - timedelta(hours=COOLDOWN_H)
        ]
        if recent:
            continue
        try:
            sig = check(sym, name, now)
        except Exception as e:
            print(sym, "errore:", e)
            continue
        if not sig:
            continue
        st["signals"].append(sig)
        todays.append(sig)
        sent += 1
        icon = "🟢" if sig["side"] == "LONG" else "🔴"
        send(
            f"{icon} {sig['name']} - {sig['side']}\n"
            f"{sig['note']}.\n\n"
            f"Entrata: {fmt(sig['entry'])}\n"
            f"Stop loss: {fmt(sig['stop'])}\n"
            f"Take profit: {fmt(sig['tp'])}"
        )


def simulate(s, now):
    df = get_data(s["sym"])
    start = datetime.fromisoformat(s["time"]) + timedelta(minutes=15)
    end = now.replace(hour=17, minute=30, second=0, microsecond=0)
    d = df[(df.index >= start) & (df.index <= end)]
    long_ = s["side"] == "LONG"
    exit_p, why = None, "chiusura"
    for _, r in d.iterrows():
        if long_:
            if r["Low"] <= s["stop"]:
                exit_p, why = s["stop"], "STOP"
                break
            if r["High"] >= s["tp"]:
                exit_p, why = s["tp"], "TP"
                break
        else:
            if r["High"] >= s["stop"]:
                exit_p, why = s["stop"], "STOP"
                break
            if r["Low"] <= s["tp"]:
                exit_p, why = s["tp"], "TP"
                break
    if exit_p is None:
        if d.empty:
            return None
        exit_p = float(d.iloc[-1]["Close"])
    pct = (exit_p - s["entry"]) / s["entry"] * 100 * (1 if long_ else -1)
    risk_pct = abs(s["entry"] - s["stop"]) / s["entry"] * 100
    return why, pct, pct / risk_pct


def report(st, now):
    today = now.strftime("%Y-%m-%d")
    todays = [s for s in st["signals"] if s["date"] == today]
    st["reported"] = today
    if not todays:
        send("📊 Resoconto di oggi: nessun segnale inviato.")
        return
    lines, tot, rtot, wins, n = [], 0.0, 0.0, 0, 0
    for s in todays:
        try:
            res = simulate(s, now)
        except Exception as e:
            print(s["sym"], "errore:", e)
            res = None
        hhmm = datetime.fromisoformat(s["time"]).strftime("%H:%M")
        if not res:
            lines.append(f"⚪ {s['name']} {s['side']} {hhmm}: dati non disponibili")
            continue
        why, pct, r = res
        n += 1
        tot += pct
        rtot += r
        wins += pct > 0
        icon = "✅" if pct > 0 else "❌"
        lines.append(f"{icon} {s['name']} {s['side']} {hhmm} -> {why} {pct:+.2f}% ({r:+.1f}R)")
    msg = "📊 Resoconto di oggi\n\n" + "\n".join(lines)
    if n:
        msg += (
            f"\n\nSegnali: {n} | Vincenti: {wins}\n"
            f"Totale: {tot:+.2f}% ({rtot:+.1f}R)\n"
            "Simulazione con posizione uguale su ogni segnale, chiusura a fine giornata."
        )
    send(msg)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    now = datetime.now(ROMA)
    st = load_state()
    if mode == "test":
        send("✅ Bot collegato correttamente.")
    elif now.weekday() < 5:
        hm = (now.hour, now.minute)
        if START <= hm <= END:
            scan(st, now)
        elif hm >= REPORT_FROM and st.get("reported") != now.strftime("%Y-%m-%d"):
            report(st, now)
    save_state(st, now)


if __name__ == "__main__":
    main()
