"""Ensemble: combine module scores → cb_score, apply the .75 / .85 gates.

The Ensemble takes *callables*, not model objects, so it can be unit-tested with mocks
and so real models load lazily in app startup. Module contracts:

  message_scorer(text: str)                      -> float 0..1   (Phase 1)
  context_scorer(window_texts: list[str])        -> float 0..1   (Phase 2)
  exclusion_scorer(logs: dict)                    -> float 0..1   (Phase 3, optional/stub)
  bystander_fn(context, speaker, text, logs)      -> dict{'behavior',...} (Phase 4, optional)
"""
from typing import Callable, List, Optional, Dict
from .check_chat_excel import prosocial_guard, evaluate_window


class Ensemble:
    def __init__(self,
                 message_scorer: Callable[[str], float],
                 context_scorer: Callable[[List[str]], float],
                 exclusion_scorer: Optional[Callable[[dict], float]] = None,
                 bystander_fn: Optional[Callable] = None,
                 weights: Optional[Dict[str, float]] = None,
                 combine_mode: str = "max_of",
                 suspect: float = 0.75,
                 confirm: float = 0.85):
        self.message_scorer = message_scorer
        self.context_scorer = context_scorer
        self.exclusion_scorer = exclusion_scorer
        self.bystander_fn = bystander_fn
        # weights over AVAILABLE modules; renormalized per request (missing modules dropped)
        self.weights = weights or {"message": 0.5, "context": 0.4, "exclusion": 0.1}
        # how to fuse module scores. TUNE THIS on labeled full conversations:
        #   "weighted" : weighted average (smooth, but dilutes a single strong signal)
        #   "noisy_or" : 1-Π(1-s)   (fires if ANY module is high; can over-trigger)
        #   "max_of"   : max(weighted_avg, strongest single module)  ← safety-leaning default
        self.combine_mode = combine_mode
        self.suspect = suspect
        self.confirm = confirm

    def _combine(self, scores: Dict[str, Optional[float]]) -> float:
        present = {k: v for k, v in scores.items() if v is not None and k in self.weights}
        if not present:
            return 0.0
        wsum = sum(self.weights[k] for k in present)
        weighted = sum(self.weights[k] * present[k] for k in present) / wsum
        if self.combine_mode == "weighted":
            return weighted
        if self.combine_mode == "noisy_or":
            prod = 1.0
            for v in present.values():
                prod *= (1.0 - v)
            return 1.0 - prod
        # "max_of": don't let a strong single module (e.g. clear exclusion, or one
        # unambiguously toxic message) be washed out by calm signals elsewhere.
        return max(weighted, max(present.values()))

    def analyze(self, req: dict) -> dict:
        ctx: List[dict] = req.get("context", [])
        new = req["new_message"]
        is_def = bool(new.get("is_defense_action", False))              # NEW
        window_texts = [m["text"] for m in ctx] + [new["text"]]

        m_score = float(self.message_scorer(new["text"]))
        c_score = float(self.context_scorer(window_texts))
        e_score = None
        if self.exclusion_scorer is not None and req.get("logs"):
            e_score = float(self.exclusion_scorer(req["logs"]))
        scores = {"message": m_score, "context": c_score, "exclusion": e_score}
        cb_score = self._combine(scores)

        # NEW: prosocial guard on the new message → fixes the comfort-message FP (Bug 1)
        cb_score, suppressed, guard_reason = prosocial_guard(
            new["text"], cb_score, is_defense_action=is_def)

        types = []
        if not suppressed and max(m_score, c_score) >= self.suspect: types.append("언어적 폭력")
        if not suppressed and e_score is not None and e_score >= self.suspect: types.append("배제")
        cb_type = "·".join(types) if types else "비해당"

        # NEW: per-message attribution over the window → 가해자/피해자 + targeting-aware verdict
        items = []
        for m in ctx + [new]:
            a = float(self.message_scorer(m["text"]))
            cb_m, _, _ = prosocial_guard(m["text"], a,
                                         is_defense_action=bool(m.get("is_defense_action", False)))
            items.append({"speaker": m["participant_code"], "text": m["text"],
                          "cb": cb_m, "dis": None})
        verdict = evaluate_window(items)
        attr = verdict["attr"]

        # CHANGED: full intervention now requires a high score AND a real target (fixes Bug 2)
        if cb_score >= self.confirm and verdict["is_bullying"]:
            level, need = "confirm", True
        elif cb_score >= self.suspect:
            level, need = "suspect", False       # aggressor-facing pre-send warning, unchanged
        else:
            level, need = "none", False

        bystander = None
        if level != "none" and self.bystander_fn is not None:
            ctx_turns = [(m["participant_code"], m["text"]) for m in ctx]
            try:
                bystander = self.bystander_fn(ctx_turns, new["participant_code"],
                                              new["text"], req.get("logs")).get("behavior")
            except Exception:
                bystander = None

        evidence = (f"msg={m_score:.2f} ctx={c_score:.2f}"
                    + (f" excl={e_score:.2f}" if e_score is not None else "")
                    + f" → cb={cb_score:.2f} [{level}]")

        return {"room_id": req.get("room_id", ""),
                "cb_score": round(cb_score, 4), "cb_type": cb_type,
                "intervention_level": level, "intervention_needed": need,
                "attribution": {                                          # NEW block
                    "is_bullying": verdict["is_bullying"],
                    "aggressors": attr.aggressors,        # 가해자
                    "victim": attr.victim,                # 피해자
                    "victim_reason": attr.victim_reason,
                    "confidence": attr.confidence,
                    "drop_reason": verdict["drop_reason"]},
                "suppressed": suppressed, "guard_reason": guard_reason,   # NEW
                "bystander_behavior": bystander,
                "module_scores": {"message": round(m_score, 4), "context": round(c_score, 4),
                                  "exclusion": (round(e_score, 4) if e_score is not None else None)},
                "evidence": evidence}
