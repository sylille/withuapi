# -*- coding: utf-8 -*-
"""
WithU Talk — real-model loaders.

Turns your fine-tuned checkpoints into the plain callables the pipeline expects:
    module_a(text) -> float   # cb_score, P(verbal violence)   [Module A / KcELECTRA]
    module_b(text) -> float   # distress, P(negative reaction)  [optional]

Usage on the server:
    from models import load_classifier
    module_a = load_classifier(MODULE_A_DIR, base_tokenizer="beomi/KcELECTRA-base",
                               positive_label="1")   # confirm with inspect_model.py
    # then:  check_workbook("chat.xlsx", "out.xlsx", module_a=module_a, module_b=None)

Requires torch + transformers (already installed where you trained).
"""
from __future__ import annotations
from functools import lru_cache
from typing import Optional, Callable


def resolve_positive_index(id2label: dict, positive_label=None) -> int:
    """Pick which output index means 'violence/hate/positive'. Order is NOT guaranteed."""
    labels = {int(k): str(v) for k, v in id2label.items()}
    n = len(labels)
    if positive_label is not None:
        for i, v in labels.items():
            if v == str(positive_label):
                return i
        try:
            pi = int(positive_label)
            if pi in labels:
                return pi
        except (ValueError, TypeError):
            pass
    kws = ["hate", "violence", "toxic", "offensive", "abusive", "bully", "harm",
           "혐오", "폭력", "공격", "유해", "악성", "비속", "욕설"]
    for i, v in labels.items():
        if any(k in v.lower() for k in kws):
            return i
    return 1 if n == 2 else n - 1   # binary -> index 1 by convention


def load_classifier(
    model_dir: str,
    *, base_tokenizer: Optional[str] = None,
    positive_label=None, max_length: int = 256, device: Optional[str] = None,
    batch_size: int = 32,
) -> Callable[[str], float]:
    """Load a fine-tuned sequence classifier and return a text->probability callable."""
    import torch
    import torch.nn.functional as F
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")

    # tokenizer: prefer the one saved in the checkpoint; fall back to the base model
    try:
        tok = AutoTokenizer.from_pretrained(model_dir)
    except Exception:
        if not base_tokenizer:
            raise RuntimeError(
                f"No tokenizer files in {model_dir}. Either copy the tokenizer files "
                f"(tokenizer_config.json, vocab.txt, special_tokens_map.json) into that "
                f"folder, or pass base_tokenizer='beomi/KcELECTRA-base'."
            )
        tok = AutoTokenizer.from_pretrained(base_tokenizer)

    model = AutoModelForSequenceClassification.from_pretrained(model_dir).to(device).eval()
    pos = resolve_positive_index(model.config.id2label, positive_label)
    print(f"[load_classifier] {model_dir}")
    print(f"    device={device}  id2label={dict(model.config.id2label)}  positive_index={pos}")

    @torch.no_grad()
    def _score_batch(texts: list[str]):
        enc = tok(list(texts), return_tensors="pt", truncation=True,
                  max_length=max_length, padding=True).to(device)
        probs = F.softmax(model(**enc).logits, dim=-1)
        return probs[:, pos].tolist()

    @lru_cache(maxsize=200_000)
    def score(text: str) -> float:
        t = (text or "").strip()
        if not t:
            return 0.0
        return float(_score_batch([t])[0])

    # expose a batch scorer for speed on large files (optional)
    score.batch = _score_batch          # type: ignore[attr-defined]
    score.positive_index = pos          # type: ignore[attr-defined]
    return score
