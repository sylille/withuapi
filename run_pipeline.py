# -*- coding: utf-8 -*-
"""
Run withu_pipeline.py's decision logic with your REAL Module A.

This is the window-level pipeline (prosocial guard + role attribution + event gate).
It does NOT parse chat files — it takes a window of messages you hand it. Use it to
confirm the guard behaves with the real model (e.g. the comfort message stays
suppressed), then move on to check_chat_excel.py for whole exports.

Files needed in the same folder:
    withu_pipeline.py   (the pipeline)
    models.py           (loader)
    run_pipeline.py     (this file)

Edit MODULE_A_DIR / A_POSITIVE, then:  python run_pipeline.py
"""
from withu_models import load_classifier
from withu_pipeline import analyze_window

# ---- EDIT THESE ------------------------------------------------------------
MODULE_A_DIR = "../models/stage2_domain_final/checkpoint-264"
A_POSITIVE   = "1"
A_BASE_TOKEN = "beomi/KcELECTRA-base"    # confirm with: python inspect_model.py <dir>
# module_b (distress) stays off for now — see note in chat.
# ---------------------------------------------------------------------------

def show(title, result, window):
    print("=" * 68); print(title); print("=" * 68)
    for i, (r, g) in enumerate(zip(window, result.guarded)):
        red = "RED " if result.highlights[i] else "    "
        note = "" if g.reason == "none" else f"  <-guard:{g.reason} (raw {g.cb_raw:.3f})"
        print(f"  {red}[{r['speaker']}] {r['text'][:40]:40} cb={g.cb_final:.3f}{note}")
    a = result.attribution
    print(f"  -> create_event={result.create_event} ({result.reason})")
    print(f"  -> 가해자={a.aggressors}  피해자={a.victim!r} via {a.victim_reason}  conf={a.confidence}")
    print(f"  -> roles: {{ {', '.join(f'{s}:{sr.role}' for s, sr in a.per_speaker.items())} }}")
    print()

def main():
    module_a = load_classifier(MODULE_A_DIR, base_tokenizer=A_BASE_TOKEN, positive_label=A_POSITIVE)

    # Case 1: the reported false positive — comfort message in a 1:1 room.
    w1 = [{"speaker": "방어친구", "text": "네 잘못이 아니야.", "targets": ["피해아동"],
           "is_defense_action": True}]
    show("CASE 1 — comfort message (should NOT create event, NOT red)",
         analyze_window(w1, module_a, is_one_to_one=True), w1)

    # Case 2: same comfort message but the app forgot to set the defense flag.
    w2 = [{"speaker": "방어친구", "text": "네 잘못이 아니야."}]
    show("CASE 2 — comfort message, no provenance flag (content guard should cap it)",
         analyze_window(w2, module_a, is_one_to_one=True), w2)

    # Case 3: a real attack — should create an event and name aggressor/victim.
    w3 = [{"speaker": "가해자", "text": "너 돼지야?"},
          {"speaker": "가해자", "text": "역겁네 진짜 꺼져"},
          {"speaker": "피해자", "text": "왜 그래 진짜 그만해"},
          {"speaker": "친구", "text": "야 그만해라"}]
    show("CASE 3 — real attack (SHOULD create event)",
         analyze_window(w3, module_a), w3)

if __name__ == "__main__":
    main()
