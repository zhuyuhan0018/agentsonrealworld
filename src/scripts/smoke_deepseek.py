#!/usr/bin/env python3
"""Minimal DeepSeek call using project .env (DEEPSEEK_API_KEY). Run from repo root:
   .venv/bin/python src/scripts/smoke_deepseek.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

load_dotenv(ROOT / ".env")

from src.models import factory  # noqa: E402


def main() -> None:
    llm = factory.create("deepseek", "deepseek-chat", {})
    prompt = "用一句话回答：1+1 等于几？只输出算式结果，不要解释。"
    reply = llm.invoke([HumanMessage(content=prompt)])
    text = (reply.content or "").strip()

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": "deepseek-chat",
        "prompt": prompt,
        "reply": text,
    }
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / "deepseek_smoke.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nWrote: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
