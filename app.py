"""WithU Talk — AI inference server (AI 추론 서버).

Run:
    pip install fastapi uvicorn torch transformers anthropic
    uvicorn withu_api.app:app --host 0.0.0.0 --port 8000

The WithU backend POSTs a message + recent context to /analyze and gets back a
cyberbullying judgment. The chatbot layer (OpenAI, per the design doc) is a SEPARATE
service; this server only returns the judgment.
"""
import os
from fastapi import FastAPI
from .schemas import AnalyzeRequest, AnalyzeResponse
from .ensemble import Ensemble
from . import models

app = FastAPI(title="WithU Talk AI 추론 서버", version="0.1.0")
_ensemble: Ensemble | None = None


def build_ensemble() -> Ensemble:
    """Load real modules. Override pieces via env for staged rollout."""
    message_scorer = models.load_message_scorer()
    context_scorer = models.load_context_scorer()

    bystander_fn = None
    if os.environ.get("ENABLE_BYSTANDER") == "1":
        # import your Phase-4 classify_bystander and wrap it
        from phase4_bystander import classify_bystander       # adjust import to your module
        bystander_fn = models.load_bystander_fn(classify_bystander)

    return Ensemble(
        message_scorer=message_scorer,
        context_scorer=context_scorer,
        exclusion_scorer=models.exclusion_stub,   # Module C stub until live logs exist
        bystander_fn=bystander_fn,
        suspect=float(os.environ.get("SUSPECT_THRESHOLD", 0.75)),
        confirm=float(os.environ.get("CONFIRM_THRESHOLD", 0.85)),
    )


@app.on_event("startup")
def _startup():
    global _ensemble
    _ensemble = build_ensemble()


@app.get("/health")
def health():
    return {"status": "ok", "ensemble_ready": _ensemble is not None}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    result = _ensemble.analyze(req.model_dump())
    return AnalyzeResponse(**result)
