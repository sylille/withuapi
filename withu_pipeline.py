# -*- coding: utf-8 -*-
"""
WithU Talk — consolidated detection pipeline (run-in-one-go).

Fixes two production bugs seen with the comfort message "네 잘못이 아니야":
  BUG 1  Module A scores a supportive/negated message as verbal violence (0.9973),
         so the app highlights it red like an aggressor message.
  BUG 2  That single message spawns a NEW cyberbullying event inside a 1:1 comfort
         room and pushes an intervention alert to the victim child.

The pipeline adds two guard layers on top of your existing Module A / Module B:

  GUARD 1  Prosocial guard   -> suppresses false positives on supportive messages.
                                Provenance flag (app says "this is a comfort/defense
                                action") hard-overrides; content heuristics are a
                                conservative fallback that only *caps* the score and
                                flags for the annotation audit queue.
  GUARD 2  Event gate         -> a CB event is created only when attribution names a
                                real aggressor (!= victim) with enough directed
                                aggression. A defender sending support in a 1:1 room
                                never creates an event.

Standalone: `python withu_pipeline.py` runs a self-test that reproduces the bug and
proves the fix WITHOUT the real models, then (if corpus.xlsx is present) re-validates
role attribution on the 5 gold events. Stdlib only — safe for the pip-less server.

Wiring the real models: pass callables into `analyze_window(...)`:
    module_a(text) -> float cb_score in [0,1]
    module_b(text) -> float distress in [0,1]   (negative/victim-reaction prob)
"""
from __future__ import annotations
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Optional, Callable
import math, re

# ============================================================================
# CONFIG (server-tunable)
# ============================================================================
TOX_THRESHOLD       = 0.50   # cb_score at/above this = aggressive message
MIN_AGGR_MSGS       = 2      # aggressive msgs a speaker needs to be 가해자
SECONDARY_RATIO     = 0.34   # keep co-aggressor if count >= top_count * this
ADJACENCY_WINDOW    = 2      # +/- turns "adjacent" to an aggressive msg
PROSOCIAL_CAP       = 0.30   # content-suppressed messages capped to this cb_score
# victim signal weights
W_EXPLICIT, W_MENTION, W_DISTRESS, W_ADJACENCY = 6.0, 4.0, 2.5, 1.0

# ============================================================================
# GUARD 1 — prosocial / support detection
# ============================================================================
# Supportive patterns. Negation-aware exculpation ("네 잘못이 아니야") is the exact
# false positive we hit, so it is first-class here.
_SUPPORT_PATTERNS = [
    r"잘못\s*이?\s*아니",      # 네 잘못이 아니야 / 잘못 아니야
    r"잘못\s*이?\s*없",        # 네 잘못이 없어
    r"탓\s*(이|은)?\s*아니",   # 네 탓이 아니야
    r"때문\s*(이|은|아)?\s*아니",  # 너 때문이 아니야
    r"자책\s*하지",            # 자책하지 마
    r"괜찮아", r"괜찮을",
    r"힘\s*내", r"힘내",
    r"응원", r"네\s*편", r"내\s*편이", r"우리가\s*있", r"내가\s*있",
    r"곁에\s*있", r"함께\s*할", r"같이\s*있",
    r"걱정\s*(하지\s*)?마", r"혼자\s*(가)?\s*아니",
]
_SUPPORT_RE = [re.compile(p) for p in _SUPPORT_PATTERNS]

# Minimal profanity/slur veto: if an unambiguous attack token is present we do NOT
# suppress, even with a support-ish match (blocks camouflage like "병신 아니야?").
# Kept intentionally short; it only vetoes suppression, never adds detections.
_ATTACK_VETO = [
    "시발", "씨발", "ㅅㅂ", "병신", "ㅂㅅ", "새끼", "지랄", "꺼져",
    "죽어", "닥쳐", "존나", "개새", "애미", "좆", "역겨", "역겁",
]

def _is_prosocial(text: str) -> bool:
    if any(v in text for v in _ATTACK_VETO):
        return False
    return any(r.search(text) for r in _SUPPORT_RE)


@dataclass
class Guarded:
    cb_final: float
    cb_raw: float
    suppressed: bool
    reason: str          # 'defense_action' | 'prosocial_content' | 'none'
    audit: bool          # send to annotation audit queue?

def prosocial_guard(text: str, cb_raw: float, *, is_defense_action: bool = False) -> Guarded:
    """Layer between Module A and the event/role logic."""
    # Provenance flag: the app itself generated/sent this as a comfort/defense action.
    if is_defense_action:
        return Guarded(0.0, cb_raw, True, "defense_action", audit=False)
    # Content fallback: only cap (don't zero) and always flag for audit.
    if cb_raw >= TOX_THRESHOLD and _is_prosocial(text):
        return Guarded(min(cb_raw, PROSOCIAL_CAP), cb_raw, True, "prosocial_content", audit=True)
    return Guarded(cb_raw, cb_raw, False, "none", audit=False)


# ============================================================================
# Role attribution (Module E) — speaker-level over a window
# ============================================================================
@dataclass
class Message:
    speaker: str
    text: str = ""
    cb_score: float = 0.0
    distress: Optional[float] = None
    targets: list[str] = field(default_factory=list)
    is_defense_action: bool = False

@dataclass
class SpeakerRole:
    speaker: str; role: str; aggression: float; victim_score: float; n_messages: int

@dataclass
class Attribution:
    aggressors: list[str]; victim: Optional[str]; victim_reason: str
    per_speaker: dict[str, SpeakerRole]; confidence: float

_TOK = re.compile(r"[가-힣A-Za-z0-9]+")
def _name_tokens(name: str) -> set[str]:
    toks = set(_TOK.findall(name)); toks.add(re.sub(r"\s+", "", name))
    return {t for t in toks if len(t) >= 2}

def attribute(messages: list[Message], *, roster: Optional[list[str]] = None) -> Attribution:
    speakers = list(dict.fromkeys(m.speaker for m in messages))
    n_by = Counter(m.speaker for m in messages)
    aggr_count = Counter()
    for m in messages:
        # a defense-action message never counts as aggression
        if m.cb_score >= TOX_THRESHOLD and not m.is_defense_action:
            aggr_count[m.speaker] += 1

    if not aggr_count:
        per = {s: SpeakerRole(s, "주변인", 0.0, 0.0, n_by[s]) for s in speakers}
        return Attribution([], None, "no_aggression", per, 0.0)

    top_s, top_c = aggr_count.most_common(1)[0]
    keep = max(MIN_AGGR_MSGS, math.ceil(top_c * SECONDARY_RATIO))
    aggressors = [s for s, c in aggr_count.items() if c >= keep] or [top_s]
    aggr_set = set(aggressors)

    hi = [i for i, m in enumerate(messages) if m.cb_score >= TOX_THRESHOLD and not m.is_defense_action]
    aggr_text = " ".join(messages[i].text for i in hi)
    v_explicit, v_mention, v_adj = Counter(), Counter(), Counter()
    v_distress = defaultdict(float)
    for i in hi:
        for t in messages[i].targets:
            if t not in aggr_set: v_explicit[t] += 1
    for s in speakers:
        if s in aggr_set: continue
        if any(tok in aggr_text for tok in _name_tokens(s)): v_mention[s] += 1
    for m in messages:
        if m.speaker not in aggr_set and m.distress is not None:
            v_distress[m.speaker] += max(0.0, m.distress)
    for i in hi:
        for j in range(max(0, i-ADJACENCY_WINDOW), min(len(messages), i+ADJACENCY_WINDOW+1)):
            if messages[j].speaker not in aggr_set: v_adj[messages[j].speaker] += 1
    max_adj = max(v_adj.values()) if v_adj else 1

    vscore = {}
    for s in speakers:
        if s in aggr_set: continue
        vscore[s] = (W_EXPLICIT*v_explicit[s] + W_MENTION*v_mention[s]
                     + W_DISTRESS*v_distress[s] + W_ADJACENCY*(v_adj[s]/max_adj))
    victim, reason = None, "no_signal"
    if vscore and max(vscore.values()) > 0:
        victim = max(vscore, key=vscore.get)
        reason = ("explicit_target" if v_explicit[victim] else
                  "name_mention" if v_mention[victim] else
                  "distress_signal" if v_distress[victim] else "turn_adjacency")

    per = {}
    for s in speakers:
        role = "가해자" if s in aggr_set else ("피해자" if s == victim else "주변인")
        per[s] = SpeakerRole(s, role, float(aggr_count[s]), float(vscore.get(s, 0.0)), n_by[s])
    if roster:
        for s in roster:
            per.setdefault(s, SpeakerRole(s, "비해당", 0.0, 0.0, 0))

    counts = sorted(aggr_count.values(), reverse=True)
    agg_sep = 1.0 if len(counts) == 1 else (counts[0]-counts[1])/counts[0]
    vs = sorted(vscore.values(), reverse=True) if vscore else [0]
    vic_sep = 0.0 if not victim or vs[0] == 0 else (1.0 if len(vs) == 1 else (vs[0]-vs[1])/vs[0])
    return Attribution(aggressors, victim, reason, per, round(0.5*agg_sep+0.5*vic_sep, 3))


# ============================================================================
# GUARD 2 — event-creation gate
# ============================================================================
@dataclass
class WindowResult:
    create_event: bool
    reason: str
    attribution: Attribution
    guarded: list[Guarded]
    highlights: dict[int, bool]   # message index -> highlight red?

def analyze_window(
    raw_messages: list[dict],
    module_a: Callable[[str], float],
    module_b: Optional[Callable[[str], float]] = None,
    *, is_one_to_one: bool = False,
) -> WindowResult:
    """
    raw_messages: [{speaker, text, targets?, is_defense_action?}, ...]
    Returns event decision + attribution + per-message highlight flags.
    """
    guarded, msgs = [], []
    for r in raw_messages:
        cb_raw = module_a(r["text"])
        g = prosocial_guard(r["text"], cb_raw, is_defense_action=r.get("is_defense_action", False))
        guarded.append(g)
        msgs.append(Message(
            speaker=r["speaker"], text=r["text"], cb_score=g.cb_final,
            distress=(module_b(r["text"]) if module_b else None),
            targets=r.get("targets", []), is_defense_action=r.get("is_defense_action", False),
        ))

    attr = attribute(msgs)
    # highlight only genuinely aggressive (post-guard) messages
    highlights = {i: (m.cb_score >= TOX_THRESHOLD and not m.is_defense_action)
                  for i, m in enumerate(msgs)}

    # event gate: need a real aggressor distinct from victim, with directed aggression
    n_aggr_msgs = sum(1 for m in msgs if m.cb_score >= TOX_THRESHOLD and not m.is_defense_action)
    if not attr.aggressors:
        create, reason = False, "no_aggressor_after_guard"
    elif attr.victim and set(attr.aggressors) == {attr.victim}:
        create, reason = False, "aggressor_equals_victim"
    elif is_one_to_one and n_aggr_msgs < MIN_AGGR_MSGS:
        create, reason = False, "one_to_one_insufficient_aggression"
    else:
        create, reason = True, "aggressor_confirmed"
    return WindowResult(create, reason, attr, guarded, highlights)


# ============================================================================
# SELF-TEST — proves both bugs are fixed, no real models needed
# ============================================================================
def _demo_module_a(text: str) -> float:
    """Stub that reproduces Module A's behavior INCLUDING the negation false positive."""
    if re.search(r"잘못\s*이?\s*아니", text):   # the bug: supportive msg scored as violence
        return 0.9973
    hostile = ["돼지", "역겁", "역겨", "꺼져", "죽어", "병신", "씨발", "시발", "닥쳐"]
    return 0.95 if any(h in text for h in hostile) else 0.05

def _demo_module_b(text: str) -> float:
    return 0.9 if any(k in text for k in ["왜저럼", "왜 저럼", "ᅲ", "ㅠ", "그만", "싫어"]) else 0.1

def _self_test():
    print("="*70); print("SELF-TEST 1 — comfort message in a 1:1 room (the reported bug)"); print("="*70)
    window = [{"speaker": "방어친구", "text": "네 잘못이 아니야.", "targets": ["피해아동"],
               "is_defense_action": True}]
    raw_a = _demo_module_a(window[0]["text"])
    print(f"Module A raw score for '네 잘못이 아니야.' : {raw_a}   <- BUG 1 (0.9973)")
    
    res = analyze_window(window, _demo_module_a, _demo_module_b, is_one_to_one=True)
    g = res.guarded[0]
    print(f"After prosocial guard          : cb_final={g.cb_final}  reason={g.reason}")
    print(f"Highlighted red?               : {res.highlights[0]}   (expected False -> BUG 1 fixed)")
    print(f"Create CB event?               : {res.create_event}  ({res.reason})  (expected False -> BUG 2 fixed)")
    print(f"Sender role                    : {res.attribution.per_speaker['방어친구'].role}  (expected 주변인, NOT 가해자)")
    assert g.cb_final == 0.0 and not res.highlights[0] and not res.create_event
    assert res.attribution.per_speaker['방어친구'].role != "가해자"
    print("PASS\n")

    print("="*70); print("SELF-TEST 2 — content fallback when app sends NO provenance flag"); print("="*70)
    window = [{"speaker": "방어친구", "text": "네 잘못이 아니야."}]   # no is_defense_action
    res = analyze_window(window, _demo_module_a, _demo_module_b, is_one_to_one=True)
    g = res.guarded[0]
    print(f"cb_raw={g.cb_raw} -> cb_final={g.cb_final}  reason={g.reason}  audit={g.audit}")
    print(f"Highlighted? {res.highlights[0]}   Create event? {res.create_event}")
    assert g.cb_final <= PROSOCIAL_CAP and not res.create_event and g.audit
    print("PASS  (capped + queued for annotation audit)\n")

    print("="*70); print("SELF-TEST 3 — real attack still fires (no over-suppression)"); print("="*70)
    window = [{"speaker": "가해자", "text": "너 돼지야?"},
              {"speaker": "가해자", "text": "역겁네 진짜"},
              {"speaker": "피해자", "text": "왜저럼 ㅠㅠ"}]
    res = analyze_window(window, _demo_module_a, _demo_module_b)
    print(f"aggressors={res.attribution.aggressors} victim={res.attribution.victim!r} "
          f"create_event={res.create_event}")
    assert res.create_event and "가해자" in res.attribution.aggressors
    print("PASS\n")

    print("="*70); print("SELF-TEST 4 — camouflage attempt is NOT suppressed"); print("="*70)
    txt = "너 진짜 병신 아니야?"   # support-shaped negation + slur
    g = prosocial_guard(txt, _demo_module_a(txt))
    print(f"'{txt}' -> suppressed={g.suppressed} (expected False; attack veto)")
    assert not g.suppressed
    print("PASS\n")

def _validate_corpus():
    try:
        import pandas as pd
    except Exception:
        print("(pandas not available — skipping corpus validation)"); return
    import os
    path = next((p for p in ["Downloads/excel_wangtta.xlsx"] if os.path.exists(p)), None)
    if not path:
        print("(corpus file not found — skipping corpus validation)"); return
    print("="*70); print(f"CORPUS VALIDATION — role attribution on 5 gold events ({path})"); print("="*70)
    df = pd.read_excel(path, sheet_name="sheet1")
    df = df[df["행유형"] == "message"].reset_index(drop=True)
    a_ok = v_ok = 0
    for ev in ["WCB001", "WCB002", "WCB003", "WCB004", "WCB005"]:
        e = df[df["사건ID"] == ev]
        gold_a = set(e[e["참여역할"] == "가해자"]["발화자"])
        gold_v = set(e["피해대상"].dropna())
        msgs = [Message(speaker=str(r["발화자"]), text=str(r["내용"]),
                        cb_score=0.9 if r["메시지기능"] == "가해발화" else 0.05,
                        distress=1.0 if r["메시지기능"] == "피해반응" else 0.0)
                for _, r in e.iterrows()]
        res = attribute(msgs)
        ah = bool(set(res.aggressors) & gold_a); vh = res.victim in gold_v
        a_ok += ah; v_ok += vh
        print(f"  {ev}: aggr={res.aggressors} [{'OK' if ah else 'MISS'}]  "
              f"victim={res.victim!r} via {res.victim_reason} [{'OK' if vh else 'MISS'}]")
    print(f"  aggressor {a_ok}/5   victim {v_ok}/5")
    print("  (gold 가해발화 used as cb_score stand-in, 피해반응 as distress stand-in; n=5, directional)")

if __name__ == "__main__":
    _self_test()
    _validate_corpus()
    print("\nALL CHECKS DONE.")
