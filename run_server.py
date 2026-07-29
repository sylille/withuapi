# -*- coding: utf-8 -*-
"""
The one script to run on the server. Edit the paths, then:  python run_server.py chat.xlsx

Files that must sit in the same folder:
    check_chat_excel.py   (parser + pipeline)
    models.py             (loaders)
    run_server.py         (this file)
"""
import sys
from withu_models import load_classifier
from check_chat_excel import check_workbook

# ---- EDIT THESE ------------------------------------------------------------
A_BASE_TOKEN   = "beomi/KcELECTRA-base"   # only used if tokenizer files aren't in the dir
MODULE_A_DIR = "../models/stage2_domain_final/checkpoint-264"
A_POSITIVE   = "1"                     # confirm with inspect_model.py (index or label)

# Module B is the window CB-context model, NOT a per-message distress signal, so leave
# distress OFF to start. Victim is then found via name-mention + turn-adjacency (~4/5 on
# the corpus). Only wire a distress scorer here if you have a per-message negative-
# sentiment model — see note in the message.
MODULE_B_DIR   = None
# ---------------------------------------------------------------------------

def main(input_xlsx, out_xlsx="chat_check_result.xlsx"):
    module_a = load_classifier(MODULE_A_DIR, base_tokenizer=A_BASE_TOKEN, positive_label=A_POSITIVE)
    module_b = None
    if MODULE_B_DIR:
        module_b = load_classifier(MODULE_B_DIR, positive_label="1")
    check_workbook(input_xlsx, out_xlsx, module_a=module_a, module_b=module_b)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python run_server.py your_chat.xlsx [out.xlsx]"); sys.exit(1)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "chat_check_result.xlsx")
