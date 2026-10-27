from __future__ import annotations
from typing import Any

DEFAULT_THRESHOLDS={"min_duration_seconds":0.15,"max_duration_seconds":90.0,"review_wer":0.5,"review_cer":0.4,"silence_fraction_review":0.8,"repetition_ratio_review":0.5,"truncation_ratio_review":0.5}

def classify(*, artifact_exists: bool, readable: bool, outcome: str|None=None, vad: dict|None=None,
             duration: float|None=None, wer: float|None=None, cer: float|None=None,
             transcript: str|None=None, reference_text: str|None=None, thresholds: dict|None=None, vad_unavailable: bool=False) -> dict:
    t={**DEFAULT_THRESHOLDS,**(thresholds or {})}; reasons=[]; status="PASS"
    if outcome in {"error","exception","failed","failure"}:
        status="FAIL"; reasons.append(f"Generation outcome reported as {outcome}.")
    if not artifact_exists:
        status="FAIL"; reasons.append("Attempted generation has no output artifact.")
    elif not readable:
        status="FAIL"; reasons.append("Audio is unreadable, corrupt, or contains invalid samples.")
    if artifact_exists and readable:
        if duration is not None and duration <= t["min_duration_seconds"]:
            status="FAIL"; reasons.append(f"Confirmed empty or silent output duration ({duration:.3f}s).")
        elif duration is not None and (duration<t["min_duration_seconds"]*4 or duration>t["max_duration_seconds"]):
            if status!="FAIL": status="REVIEW"
            reasons.append(f"Unusual audio duration ({duration:.2f}s).")
        if vad and vad.get("speech_segments",0)==0:
            status="FAIL"; reasons.append("Silero VAD detected no speech segments.")
        elif vad and vad.get("silence_fraction",0)>=t["silence_fraction_review"]:
            if status!="FAIL": status="REVIEW"
            reasons.append("Unusually high non-speech fraction.")
        elif vad_unavailable and status=="PASS":
            status="REVIEW"; reasons.append("Silero VAD result unavailable; speech presence was not confirmed.")
    if wer is not None and wer>=t["review_wer"]:
        if status=="PASS": status="REVIEW"
        reasons.append(f"WER {wer:.3f} exceeds review threshold.")
    if cer is not None and cer>=t["review_cer"]:
        if status=="PASS": status="REVIEW"
        reasons.append(f"CER {cer:.3f} exceeds review threshold.")
    if reference_text and transcript is not None:
        ref_words=reference_text.split(); hyp_words=transcript.split()
        if len(ref_words)>=4 and len(hyp_words)/len(ref_words)<=t["truncation_ratio_review"]:
            if status=="PASS": status="REVIEW"
            reasons.append("Recognized transcript is much shorter than intended text; possible truncation.")
    if transcript:
        words=transcript.casefold().split()
        if len(words)>=8 and len(set(words))/len(words)<=t["repetition_ratio_review"]:
            if status=="PASS": status="REVIEW"
            reasons.append("Recognized transcript has unusually high word repetition.")
    return {"status":status,"reasons":reasons or ["No configured failure or review condition detected."],"review_override":None}

def apply_override(classification: dict, override: dict|None) -> dict:
    if override:
        status=override.get("status")
        if status not in {"PASS","REVIEW","FAIL"}: raise ValueError("review override status must be PASS, REVIEW, or FAIL")
        classification={**classification,"status":status,"review_override":{"status":status,"reviewer":override.get("reviewer"),"reason":override.get("reason"),"timestamp":override.get("timestamp")}}
    return classification

def failure_rate(attempts: list[dict], observable_failures: int, supplied: int, complete: bool=True) -> dict:
    if complete:
        failed=sum(1 for a in attempts if str(a.get("outcome","")).casefold() in {"error","exception","failed","failure"} or a.get("artifact_missing",False))
        return {"rate":failed/len(attempts) if attempts else None,"failed":failed,"denominator":len(attempts),"label":"attempt-based (complete attempt manifest supplied)"}
    label="observable artifact failure rate only; attempt manifest marked incomplete" if attempts else "observable artifact failure rate only; no complete attempt manifest supplied"
    return {"rate":observable_failures/supplied if supplied else None,"failed":observable_failures,"denominator":supplied,"label":label}
