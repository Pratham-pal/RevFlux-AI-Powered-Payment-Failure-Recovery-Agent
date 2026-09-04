"""FastAPI app for the Payment Failure Recovery Agent demo site.

    python -m uvicorn web.app:app --reload      # local
    python -m uvicorn web.app:app --host 0.0.0.0 --port $PORT   # deploy

Routes:
    GET  /                     the single-page demo
    GET  /api/config           front-end bootstrap (scenarios, razorpay on/off, backend)
    POST /api/create-order     create a Razorpay test order (or a mock order)
    POST /api/recover          run one failed payment through the agent + naive baseline
    POST /webhook/razorpay     optional real webhook sink (demo uses the client callback)
    GET  /api/batch            the pre-computed 200-payment batch summary
    GET  /api/batch/audit      the batch audit trail (JSON lines -> list)
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv

load_dotenv()  # pick up a local .env before the agent modules read os.environ

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from agent.classifier import active_backend  # noqa: E402
from web import razorpay_client  # noqa: E402
from web.recovery_service import run_demo, scenario_list  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
RESULTS_DIR = os.path.join(ROOT, "results")

app = FastAPI(title="Payment Failure Recovery Agent")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateOrderReq(BaseModel):
    amount: float = Field(gt=0, le=10_000_000)
    scenario: str


class RecoverReq(BaseModel):
    scenario: str
    amount: float = Field(gt=0, le=10_000_000)
    retry_count_so_far: int = Field(default=0, ge=0, le=5)
    razorpay_error: dict | None = None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/config")
def config_bootstrap() -> dict:
    return {
        "razorpay_enabled": razorpay_client.is_enabled(),
        "classifier_backend": active_backend(),
        "scenarios": scenario_list(),
    }


# ---------------------------------------------------------------------------
# Payment + recovery
# ---------------------------------------------------------------------------

@app.post("/api/create-order")
def create_order(req: CreateOrderReq) -> dict:
    receipt = f"demo_{req.scenario}"
    if razorpay_client.is_enabled():
        try:
            return razorpay_client.create_order(req.amount, receipt)
        except Exception as exc:  # noqa: BLE001 - fall back to mock, don't 500 the demo
            return {"provider": "mock", "reason": f"razorpay error: {exc}",
                    "amount": int(round(req.amount * 100)), "currency": "INR"}
    return {"provider": "mock", "amount": int(round(req.amount * 100)), "currency": "INR"}


@app.post("/api/recover")
def recover(req: RecoverReq) -> dict:
    try:
        return run_demo(
            scenario_id=req.scenario,
            amount=req.amount,
            retry_count_so_far=req.retry_count_so_far,
            razorpay_error=req.razorpay_error,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/webhook/razorpay")
async def razorpay_webhook(payload: dict) -> dict:
    """Real webhook sink. The live demo uses the browser `payment.failed`
    callback instead, so this just acknowledges."""
    return {"received": True, "event": payload.get("event")}


# ---------------------------------------------------------------------------
# Batch (pre-computed by main.py)
# ---------------------------------------------------------------------------

@app.get("/api/batch")
def batch_summary() -> JSONResponse:
    path = os.path.join(RESULTS_DIR, "summary.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail="No batch results. Run `python main.py` first.")
    with open(path, encoding="utf-8") as fh:
        return JSONResponse(json.load(fh))


@app.get("/api/batch/audit")
def batch_audit(limit: int = 500) -> list[dict]:
    path = os.path.join(RESULTS_DIR, "audit_log.jsonl")
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail="No audit log. Run `python main.py` first.")
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rows.append(json.loads(line))
            if len(rows) >= limit:
                break
    return rows
