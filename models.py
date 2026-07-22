"""Load the trained modules and expose them as the callables the Ensemble expects.

torch/transformers are imported lazily inside the loaders so this module (and the
schemas/ensemble) can be imported and unit-tested without a GPU.
"""
import json, re
from pathlib import Path
from functools import lru_cache

PHASE1_DIR = Path("./models/stage2_domain_final")   # message toxicity
PHASE2_DIR = Path("./models/phase2_context")         # window context


# ---------- Phase 1: message toxicity scorer ----------
def load_message_scorer(model_dir=PHASE1_DIR):
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    tok = AutoTokenizer.from_pretrained(str(model_dir))
    mdl = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    mdl = mdl.to(dev).eval()

    @torch.no_grad()
    def score(text: str) -> float:
        enc = tok(text or "", return_tensors="pt", truncation=True, max_length=128).to(dev)
        return float(torch.softmax(mdl(**enc).logits, dim=-1)[0, 1])
    return score


# ---------- Phase 2: window context scorer ----------
def _render_window(texts, speakers=None):
    if speakers is None:
        speakers = ["?"] * len(texts)
    seen, letters, parts = {}, "ABCDEFGH", []
    for s, t in zip(speakers, texts):
        s = str(s)
        if s not in seen: seen[s] = letters[len(seen) % len(letters)]
        parts.append(f"{seen[s]}: {t}")
    return " [TURN] ".join(parts)

def load_context_scorer(model_dir=PHASE2_DIR):
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    tok = AutoTokenizer.from_pretrained(str(model_dir))
    mdl = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    mdl = mdl.to(dev).eval()

    @torch.no_grad()
    def score(window_texts) -> float:
        text = _render_window(window_texts)
        enc = tok(text, return_tensors="pt", truncation=True, max_length=256).to(dev)
        return float(torch.softmax(mdl(**enc).logits, dim=-1)[0, 1])
    return score


# ---------- Phase 4: bystander (LLM few-shot) ----------
def load_bystander_fn(classify_bystander):
    """Wrap your Phase-4 classify_bystander(context, speaker, text) into the ensemble contract."""
    def fn(context, speaker, text, logs=None):
        # 방관 log rule first (if metadata present)
        if logs:
            from_logs = logs.get("bystanding")
            if from_logs:
                return {"behavior": "방관", "reason": "log rule", "source": "logs"}
        lab, why = classify_bystander(context, speaker, text)
        return {"behavior": lab, "reason": why, "source": "llm"}
    return fn


# ---------- Phase 3: exclusion (배제) — STUB ----------
def exclusion_stub(logs: dict) -> float:
    """Placeholder for Module C. Wire this to real social-network / participation metrics
    from the live messenger (isolation index, response-rate asymmetry, read-no-response).
    Until then it can read a precomputed score if the app supplies one, else returns 0.
    """
    if not logs:
        return 0.0
    return float(logs.get("exclusion_score", 0.0))
