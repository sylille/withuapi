# -*- coding: utf-8 -*-
"""
WithU Talk — raw KakaoTalk export checker.

Parses a KakaoTalk .xlsx export (one message per row in column A, possibly several
sheets), runs Module A -> prosocial guard -> role attribution over sliding windows
to find candidate cyberbullying events, and writes a log workbook with 가해자/피해자.

Run:
    python check_chat_excel.py sample_chat.xlsx --out result.xlsx
    python check_chat_excel.py --selftest         # builds+checks a sample, no args

Self-contained (parser + guard + attribution all in this file) so it runs in one go.
Stdlib + pandas/openpyxl only. Replace `lexicon_module_a` / `lexicon_module_b` with
your real KcELECTRA / KLUE-RoBERTa callables for production scoring.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional, Callable
import argparse, math, re, sys

# ============================================================================
# CONFIG
# ============================================================================
TOX_THRESHOLD    = 0.50
MIN_AGGR_MSGS    = 2
SECONDARY_RATIO  = 0.34
ADJACENCY_WINDOW = 2
PROSOCIAL_CAP    = 0.30
W_EXPLICIT, W_MENTION, W_DISTRESS, W_ADJACENCY = 6.0, 4.0, 2.5, 1.0
# event detection over the raw stream
WIN_SIZE   = 15   # messages per sliding window
WIN_STEP   = 3    # step between windows
EVENT_MIN_AGGR = 3  # min aggressive msgs in a window to call it a CANDIDATE event

# ---- quality gate: candidate -> confirmed bullying event --------------------
# A profane open-chat trips EVENT_MIN_AGGR constantly. Real bullying is TARGETED
# (one victim), DOMINATED by one aggressor, and ASYMMETRIC (victim isn't hitting
# back equally). These gates encode that. Every candidate is still logged with a
# pass/fail flag + reason so you can calibrate. Loosen/tighten against labels.
GATE_CONF_MIN        = 0.55  # drop if attribution confidence below this
GATE_DROP_NO_SIGNAL  = True  # drop if no victim could be identified (no_signal)
GATE_TURNADJ_CONF    = 0.75  # adjacency-only target needs THIS much confidence (was 0.65)
GATE_TURNADJ_MIN_AGGR= 6     # adjacency-only target needs a SUSTAINED attack, not a flare
GATE_REQUIRE_SIGNAL  = False # strictest: drop ALL adjacency-only (only name/explicit/distress confirm)
GATE_AGGR_DOMINANCE  = 0.60  # top aggressor must own >= this share of aggressive msgs
GATE_VICTIM_AGGR_MAX = 0.50  # victim's own aggression must be < this * aggressor's
# optional Module B window gate (see notes). Module B (phase2_context) is a 6-message
# CB-CONTEXT classifier, recall-tuned (threshold ~0.0255) and overfit to 5 events, so
# it is a POOR precision filter — leave it off unless you've probed it. If you enable
# it, these MUST match context_meta.json.
B_WINDOW         = 6      # messages per Module B window (from context_meta.json)
B_GATE_THRESHOLD = 0.0255 # positive-class threshold from context_meta.json (NOT 0.5)

# ============================================================================
# PARSER
# ============================================================================
DATE_RE = re.compile(r"^-+\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
MSG_RE  = re.compile(r"^\[(?P<user>.+?)\]\s*\[(?P<ap>오전|오후)\s*(?P<h>\d{1,2}):(?P<m>\d{2})\]\s*(?P<text>.*)$")
SYS_RE  = re.compile(r"^(?P<actor>.+?)님(?P<part>이|을)\s+(?P<action>.+?(?:습니다|했습니다))[.。]?\s*$")

def _to24(ap: str, h: int, m: int) -> str:
    if ap == "오전":
        h = 0 if h == 12 else h
    else:
        h = 12 if h == 12 else h + 12
    return f"{h:02d}:{m:02d}"

def _classify_system(action: str) -> str:
    if "들어왔" in action:            return "join"
    if "나갔" in action:             return "leave"
    if "내보냈" in action or "내보내" in action: return "kick"
    if "되었" in action:             return "role_change"
    return "other"

@dataclass
class ParsedMsg:
    sheet: str; idx: int; date: Optional[str]; time: Optional[str]
    speaker: str; text: str

@dataclass
class SystemEvent:
    sheet: str; date: Optional[str]; actor: str; kind: str; raw: str

def parse_sheet(sheet: str, rows: list[str]) -> tuple[list[ParsedMsg], list[SystemEvent]]:
    messages: list[ParsedMsg] = []
    systems:  list[SystemEvent] = []
    cur_date: Optional[str] = None
    cur: Optional[ParsedMsg] = None
    started = False

    def flush():
        nonlocal cur
        if cur is not None:
            cur.text = cur.text.strip()
            if cur.text or True:  # keep even empty-body messages
                messages.append(cur)
            cur = None

    for raw in rows:
        line = "" if raw is None else str(raw).rstrip("\n")
        stripped = line.strip()

        if stripped == "":
            if cur is not None:      # blank line inside a multi-line message
                cur.text += "\n"
            continue

        md = DATE_RE.match(line)
        if md:
            flush(); started = True
            cur_date = f"{int(md.group(1)):04d}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
            continue

        mm = MSG_RE.match(line)
        if mm:
            flush(); started = True
            cur = ParsedMsg(sheet, len(messages), cur_date,
                            _to24(mm["ap"], int(mm["h"]), int(mm["m"])),
                            mm["user"], mm["text"])
            continue

        sm = SYS_RE.match(line)
        if sm:
            flush()
            systems.append(SystemEvent(sheet, cur_date, sm["actor"],
                                       _classify_system(sm["action"]), line))
            started = True
            continue

        if not started:
            continue                 # header junk before first date/message

        if cur is not None:          # continuation of the current message
            cur.text += "\n" + line
        # else: stray line with no owner -> ignore

    flush()
    # re-index per sheet
    for i, m in enumerate(messages):
        m.idx = i
    return messages, systems

# ============================================================================
# GUARD 1 — prosocial (same logic as withu_pipeline.py)
# ============================================================================
_SUPPORT_RE = [re.compile(p) for p in [
    r"잘못\s*이?\s*아니", r"잘못\s*이?\s*없", r"탓\s*(이|은)?\s*아니",
    r"때문\s*(이|은|아)?\s*아니", r"자책\s*하지", r"괜찮아", r"괜찮을",
    r"힘\s*내", r"힘내", r"응원", r"네\s*편", r"내\s*편이", r"우리가\s*있",
    r"내가\s*있", r"곁에\s*있", r"함께\s*할", r"같이\s*있",
    r"걱정\s*(하지\s*)?마", r"혼자\s*(가)?\s*아니",
]]
_ATTACK_VETO = ["시발","씨발","ㅅㅂ","병신","ㅂㅅ","새끼","지랄","꺼져","죽어",
                "닥쳐","존나","개새","애미","좆","역겨","역겁"]

def _is_prosocial(t: str) -> bool:
    if any(v in t for v in _ATTACK_VETO): return False
    return any(r.search(t) for r in _SUPPORT_RE)

def prosocial_guard(text: str, cb_raw: float, *, is_defense_action: bool = False):
    if is_defense_action:
        return 0.0, True, "defense_action"
    if cb_raw >= TOX_THRESHOLD and _is_prosocial(text):
        return min(cb_raw, PROSOCIAL_CAP), True, "prosocial_content"
    return cb_raw, False, "none"

# ============================================================================
# ROLE ATTRIBUTION (same logic as withu_pipeline.py)
# ============================================================================
@dataclass
class M:
    speaker: str; text: str; cb_score: float; distress: Optional[float]
    targets: list = field(default_factory=list)

_TOK = re.compile(r"[가-힣A-Za-z0-9]+")
def _name_tokens(n): 
    t = set(_TOK.findall(n)); t.add(re.sub(r"\s+","",n)); return {x for x in t if len(x)>=2}

@dataclass
class Attribution:
    aggressors: list; victim: Optional[str]; victim_reason: str; confidence: float

def attribute(msgs: list[M]) -> Attribution:
    speakers = list(dict.fromkeys(m.speaker for m in msgs))
    aggr = Counter(m.speaker for m in msgs if m.cb_score >= TOX_THRESHOLD)
    if not aggr:
        return Attribution([], None, "no_aggression", 0.0)
    top_s, top_c = aggr.most_common(1)[0]
    keep = max(MIN_AGGR_MSGS, math.ceil(top_c*SECONDARY_RATIO))
    aggressors = [s for s,c in aggr.items() if c>=keep] or [top_s]
    aset = set(aggressors)
    hi = [i for i,m in enumerate(msgs) if m.cb_score>=TOX_THRESHOLD]
    atext = " ".join(msgs[i].text for i in hi)
    v_expl, v_ment, v_adj = Counter(), Counter(), Counter(); v_dis = defaultdict(float)
    for i in hi:
        for t in msgs[i].targets:
            if t not in aset: v_expl[t]+=1
    for s in speakers:
        if s in aset: continue
        if any(tok in atext for tok in _name_tokens(s)): v_ment[s]+=1
    for m in msgs:
        if m.speaker not in aset and m.distress is not None: v_dis[m.speaker]+=max(0.0,m.distress)
    for i in hi:
        for j in range(max(0,i-ADJACENCY_WINDOW), min(len(msgs),i+ADJACENCY_WINDOW+1)):
            if msgs[j].speaker not in aset: v_adj[msgs[j].speaker]+=1
    max_adj = max(v_adj.values()) if v_adj else 1
    vscore = {}
    for s in speakers:
        if s in aset: continue
        vscore[s] = W_EXPLICIT*v_expl[s]+W_MENTION*v_ment[s]+W_DISTRESS*v_dis[s]+W_ADJACENCY*(v_adj[s]/max_adj)
    victim, reason = None, "no_signal"
    if vscore and max(vscore.values())>0:
        victim = max(vscore, key=vscore.get)
        reason = ("explicit_target" if v_expl[victim] else "name_mention" if v_ment[victim]
                  else "distress_signal" if v_dis[victim] else "turn_adjacency")
    counts = sorted(aggr.values(), reverse=True)
    asep = 1.0 if len(counts)==1 else (counts[0]-counts[1])/counts[0]
    vs = sorted(vscore.values(), reverse=True) if vscore else [0]
    vsep = 0.0 if not victim or vs[0]==0 else (1.0 if len(vs)==1 else (vs[0]-vs[1])/vs[0])
    return Attribution(aggressors, victim, reason, round(0.5*asep+0.5*vsep,3))

# ============================================================================
# MODULE A / B — lexicon fallback (REPLACE WITH REAL MODELS ON SERVER)
# ============================================================================
_HOSTILE = ["돼지","역겁","역겨","꺼져","죽어","병신","씨발","시발","닥쳐","존나",
            "개새","새끼","지랄","나가라","같은 애"]
def lexicon_module_a(text: str) -> float:
    # NOTE: reproduces KcELECTRA's known negation false positive on exculpatory
    # comfort messages ("네 잘못이 아니야" -> 0.9973) so the guard path is visible
    # in the self-test. The real model already does this; delete this line when you
    # wire the real Module A.
    if re.search(r"잘못\s*이?\s*아니", text): return 0.9973
    return 0.95 if any(h in text for h in _HOSTILE) else 0.05
def lexicon_module_b(text: str) -> float:
    return 0.9 if any(k in text for k in ["ㅠ","ㅜ","왜저럼","왜 저럼","그만","싫어","하지마"]) else 0.1

# ============================================================================
# EVENT DETECTION over the raw stream
# ============================================================================
@dataclass
class Event:
    sheet: str; start: int; end: int; date_range: str; participants: list
    aggressors: list; victim: Optional[str]; victim_reason: str
    confidence: float; n_aggr: int; sample: str
    aggr_dominance: float = 0.0    # top aggressor's share of aggressive msgs
    victim_aggr: int = 0           # aggressive msgs BY the victim (asymmetry check)
    is_bullying: bool = False      # passed the quality gate?
    drop_reason: str = ""          # why it was rejected, if not

def judge_event(aggressors, victim, victim_reason, confidence,
                aggr_dominance, victim_aggr, top_aggr_count, n_aggr) -> tuple[bool, str]:
    """Decide whether a candidate window is real bullying vs. profane banter."""
    if not aggressors:
        return False, "no_aggressor"
    if victim is None or (GATE_DROP_NO_SIGNAL and victim_reason == "no_signal"):
        return False, "no_target"                 # aggression aimed at no one specific
    if confidence < GATE_CONF_MIN:
        return False, "low_confidence"
    if victim_reason == "turn_adjacency":
        # adjacency = the WEAKEST target signal ("talked near it", not "was named").
        if GATE_REQUIRE_SIGNAL:
            return False, "no_target_signal"      # strict mode: adjacency never confirms
        if confidence < GATE_TURNADJ_CONF:
            return False, "weak_target"           # not enough separation
        if n_aggr < GATE_TURNADJ_MIN_AGGR:
            return False, "not_sustained"         # 3-msg flare, not a sustained attack
    if aggr_dominance < GATE_AGGR_DOMINANCE:
        return False, "diffuse_aggression"        # many speakers profane -> rowdy room
    if top_aggr_count and victim_aggr > GATE_VICTIM_AGGR_MAX * top_aggr_count:
        return False, "mutual_banter"             # victim hitting back -> not one-sided
    return True, ""

def detect_events(sheet: str, msgs: list[ParsedMsg],
                  module_a: Callable[[str], float],
                  module_b: Optional[Callable[[str], float]],
                  module_b_window: Optional[Callable[[str], float]] = None
                  ) -> tuple[list[Event], list[dict]]:
    scored = []
    for m in msgs:
        cb_raw = module_a(m.text)
        cb, supp, reason = prosocial_guard(m.text, cb_raw)
        scored.append(dict(m=m, cb_raw=cb_raw, cb=cb, supp=supp, reason=reason,
                           dis=(module_b(m.text) if module_b else None)))

    # slide windows, keep those with enough aggression, merge overlaps
    spans = []
    for start in range(0, max(1, len(msgs)), WIN_STEP):
        window = scored[start:start+WIN_SIZE]
        if not window: break
        n_aggr = sum(1 for s in window if s["cb"] >= TOX_THRESHOLD)
        if n_aggr >= EVENT_MIN_AGGR:
            spans.append((start, min(len(msgs), start+WIN_SIZE)))
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    events = []
    for a, b in merged:
        sub = scored[a:b]
        attr = attribute([M(s["m"].speaker, s["m"].text, s["cb"], s["dis"]) for s in sub])
        if not attr.aggressors:
            continue
        # per-speaker aggression stats for the gate
        aggr_by = Counter(s["m"].speaker for s in sub if s["cb"] >= TOX_THRESHOLD)
        total_aggr = sum(aggr_by.values())
        top_aggr_count = max((aggr_by[s] for s in attr.aggressors), default=0)
        dominance = (top_aggr_count / total_aggr) if total_aggr else 0.0
        victim_aggr = aggr_by.get(attr.victim, 0)

        is_bully, drop = judge_event(attr.aggressors, attr.victim, attr.victim_reason,
                                     attr.confidence, dominance, victim_aggr, top_aggr_count,
                                     total_aggr)

        # optional second opinion: Module B (6-msg CB-context classifier). Only used to
        # REJECT heuristic passes, never to rescue. Slides 6-msg windows (matching its
        # training) and takes the max positive prob; rejects if that stays below threshold.
        if is_bully and module_b_window is not None:
            texts = [f"{s['m'].speaker}: {s['m'].text}" for s in sub]
            best = 0.0
            for k in range(0, max(1, len(texts) - B_WINDOW + 1)):
                best = max(best, module_b_window("\n".join(texts[k:k + B_WINDOW])))
            if best < B_GATE_THRESHOLD:
                is_bully, drop = False, "moduleB_reject"

        dates = [s["m"].date for s in sub if s["m"].date]
        drange = (dates[0] if dates else "?") + (f" ~ {dates[-1]}" if dates and dates[-1]!=dates[0] else "")
        parts = list(dict.fromkeys(s["m"].speaker for s in sub))
        sample = " / ".join(f"[{s['m'].speaker}] {s['m'].text}"
                            for s in sub if s["cb"]>=TOX_THRESHOLD)[:300]
        events.append(Event(sheet, a, b, drange, parts, attr.aggressors, attr.victim,
                            attr.victim_reason, attr.confidence, total_aggr, sample,
                            round(dominance, 2), victim_aggr, is_bully, drop))
    return events, scored

# ============================================================================
# DRIVER
# ============================================================================
def check_workbook(path: str, out: str,
                   module_a=lexicon_module_a, module_b=lexicon_module_b,
                   module_b_window=None):
    import pandas as pd
    xl = pd.ExcelFile(path)
    all_msgs, all_sys, all_events, all_scored = [], [], [], []
    for sheet in xl.sheet_names:
        raw = pd.read_excel(path, sheet_name=sheet, header=None)
        col = ["" if pd.isna(v) else str(v) for v in raw.iloc[:, 0].tolist()] if raw.shape[1] else []
        msgs, systems = parse_sheet(sheet, col)
        events, scored = detect_events(sheet, msgs, module_a, module_b, module_b_window)
        all_msgs += msgs; all_sys += systems; all_events += events
        for s in scored:
            all_scored.append(s)
        confirmed = [e for e in events if e.is_bullying]
        print(f"[{sheet}] messages={len(msgs)} system={len(systems)} "
              f"candidates={len(events)} confirmed_bullying={len(confirmed)}")
        for e in confirmed:
            print(f"    EVENT idx {e.start}-{e.end} ({e.date_range}) "
                  f"가해자={e.aggressors} 피해자={e.victim!r} via {e.victim_reason} "
                  f"conf={e.confidence} dom={e.aggr_dominance} aggr_msgs={e.n_aggr}")
        dropped = Counter(e.drop_reason for e in events if not e.is_bullying)
        if dropped:
            print(f"    filtered: {dict(dropped)}")

    _write_log(out, all_scored, all_sys, all_events)
    print(f"\nlog written -> {out}")
    return all_events

def _write_log(out, scored, systems, events):
    import pandas as pd
    df_msg = pd.DataFrame([{
        "sheet": s["m"].sheet, "idx": s["m"].idx, "date": s["m"].date, "time": s["m"].time,
        "speaker": s["m"].speaker, "text": s["m"].text,
        "cb_raw": round(s["cb_raw"],4), "cb_final": round(s["cb"],4),
        "suppressed": s["supp"], "guard_reason": s["reason"],
    } for s in scored])
    df_sys = pd.DataFrame([{
        "sheet": e.sheet, "date": e.date, "actor": e.actor, "kind": e.kind, "raw": e.raw,
    } for e in systems])
    df_evt = pd.DataFrame([{
        "sheet": e.sheet, "is_bullying": e.is_bullying, "drop_reason": e.drop_reason,
        "start_idx": e.start, "end_idx": e.end, "date_range": e.date_range,
        "가해자": ", ".join(e.aggressors), "피해자": e.victim or "", "victim_reason": e.victim_reason,
        "confidence": e.confidence, "aggr_dominance": e.aggr_dominance,
        "victim_aggr": e.victim_aggr, "n_aggr_msgs": e.n_aggr,
        "participants": ", ".join(e.participants), "sample": e.sample,
    } for e in events])
    if len(df_evt):
        df_evt = df_evt.sort_values(["is_bullying", "confidence"], ascending=[False, False])
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        (df_evt if len(df_evt) else pd.DataFrame(columns=["sheet"])).to_excel(w, sheet_name="events", index=False)
        (df_sys if len(df_sys) else pd.DataFrame(columns=["sheet"])).to_excel(w, sheet_name="system_log", index=False)
        df_msg.to_excel(w, sheet_name="parsed", index=False)

# ============================================================================
def _selftest():
    import os
    path = "sample_chat.xlsx"
    if not os.path.exists(path):
        print("sample_chat.xlsx not found — run make_sample.py first."); return
    print("="*70); print("SELF-TEST on sample_chat.xlsx"); print("="*70)
    check_workbook(path, "selftest_result.xlsx")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", help="KakaoTalk export .xlsx")
    ap.add_argument("--out", default="chat_check_result.xlsx")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest or not args.input:
        _selftest()
    else:
        check_workbook(args.input, args.out)
