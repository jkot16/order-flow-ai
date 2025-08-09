import os
import re
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL", "gpt-4o-mini")
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "./credentials.json")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_WS = (os.getenv("SHEET_WORKSHEET_NAME", "") or "").strip()

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required.")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

EMAIL_RE_EXTRACT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_RE_STRICT = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
COMMON_DOMAINS = [
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "wp.pl", "onet.pl", "o2.pl", "interia.pl"
]


def extract_email(t: str) -> Optional[str]:
    m = EMAIL_RE_EXTRACT.search(t or "")
    return m.group(0) if m else None


def valid_email(s: str) -> bool:
    return bool(EMAIL_RE_STRICT.match((s or "").strip()))


def mask_email(s: str) -> str:
    if "@" not in s:
        return s
    n, d = s.split("@", 1)
    return f"{n[0] + '***'}@{d}" if len(n) > 1 else n + "***@" + d


def _lev(a: str, b: str) -> int:
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return dp[n]


def domain_suggestion(e: str) -> Optional[str]:
    if "@" not in e:
        return None
    l, dom = e.split("@", 1)
    best, dist = min(
        ((c, _lev(dom, c)) for c in COMMON_DOMAINS),
        key=lambda x: x[1]
    )
    return f"{l}@{best}" if dist <= 2 and best != dom else None


def extract_order_id(t: str) -> Optional[str]:
    rid = re.search(r"\b(\d{3,})\b", t or "")
    rid = rid.group(1) if rid else None

    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Extract the order id number. Return ONLY the number or NONE."},
                {"role": "user", "content": t or ""}
            ],
            temperature=0,
            max_tokens=10
        )
        guess = (r.choices[0].message.content or "").strip()
        if guess.isdigit():
            return guess
    except:
        pass

    return rid


def is_delayed(s: Optional[str]) -> bool:
    s = (s or "").lower()
    return "delayed" in s or "opóźn" in s


def _authorize_sheets():
    if not (os.path.exists(SERVICE_JSON) and SHEET_ID):
        raise RuntimeError("Missing credentials.")
    creds = Credentials.from_service_account_file(
        SERVICE_JSON,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return gspread.authorize(creds)


def load_orders_df() -> pd.DataFrame:
    ws = _authorize_sheets().open_by_key(SHEET_ID)
    ws = ws.worksheet(SHEET_WS) if SHEET_WS else ws.sheet1
    df = pd.DataFrame.from_records(ws.get_all_records())
    df.columns = [c.strip() for c in df.columns]

    lower_map = {c.lower(): c for c in df.columns}
    for c in ["orderid", "email", "customer", "status", "eta"]:
        if c == "email":
            df[lower_map[c]] = df[lower_map[c]].astype(str).str.strip().str.lower()
        else:
            df[lower_map[c]] = df[lower_map[c]].astype(str).str.strip()

    pretty = {lower_map[k]: k.capitalize() for k in lower_map}
    return df.rename(columns=pretty)


def find_order(df: pd.DataFrame, oid: str, email: str) -> Optional[Dict[str, Any]]:
    m = (df["Orderid"].values == str(oid)) & (df["Email"].values == email.strip().lower())
    return df.loc[m].iloc[0].to_dict() if np.any(m) else None


def compose_reply_llm(order: Dict[str, Any], q: str = None) -> str:
    delayed_flag = int(is_delayed(order.get("Status")))

    if q:
        prompt = (
            f"You are a customer support assistant. Answer the user's question "
            f"based on this order info: {order}. Question: {q}. Be accurate, 2-4 sentences."
        )
    else:
        prompt = (
            f"You are a customer support assistant. Reply in English in 2–4 short sentences. "
            f"Data: {order}, delayed_flag: {delayed_flag}. "
            f"Rules: - No apologies unless delayed_flag=1 "
            f"- End with: 'Kind regards, The Customer Care Team.'"
        )

    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=180
    )
    return (r.choices[0].message.content or "").strip()


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/ask")
def ask():
    p = request.get_json(silent=True) or {}
    q = (p.get("question") or "").strip()
    oid = (p.get("order_id") or "").strip() or extract_order_id(q)
    email = (p.get("email") or extract_email(q) or "").strip()

    if not oid and not email:
        return jsonify(
            reply="Please provide your order number (e.g., 0000) and the e-mail used to place the order."
        ), 200
    if not oid:
        return jsonify(reply="Please provide your order number (e.g., 1003).")
    if email and not valid_email(email):
        return jsonify(reply="That e-mail doesn’t look valid. Please use name@example.com.")
    if not email:
        return jsonify(reply="For verification, please also provide the e-mail used to place the order.")

    try:
        df = load_orders_df()
    except Exception as e:
        return jsonify(error=f"Data loading error: {e}"), 500

    if not np.any(df["Orderid"].values == str(oid)):
        return jsonify(reply=f"I couldn’t find order #{oid}. Please verify the number.")

    order = find_order(df, oid, email)
    if not order:
        hint = f" Did you mean **{domain_suggestion(email)}**?" if domain_suggestion(email) else ""
        return jsonify(reply=f"We found order #{oid}, but the e-mail doesn’t match our records.{hint}")

    try:
        reply = compose_reply_llm(order, q if any(w in q.lower() for w in ["when", "where"]) else None)
    except:
        reply = f"Order #{order.get('Orderid')}: status {order.get('Status')}, ETA {order.get('Eta')}."

    return jsonify(reply=reply)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
