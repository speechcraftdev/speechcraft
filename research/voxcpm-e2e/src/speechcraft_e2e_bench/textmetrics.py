from __future__ import annotations
import re, unicodedata

def normalize_text(text: str, lowercase: bool = True, remove_punctuation: bool = True) -> str:
    text = unicodedata.normalize("NFKC", text)
    if lowercase: text = text.casefold()
    if remove_punctuation: text = "".join(" " if unicodedata.category(c).startswith("P") else c for c in text)
    return re.sub(r"\s+", " ", text).strip()

def score_pair(reference: str, hypothesis: str) -> dict:
    """Score normalized text with upstream JiWER alignment objects."""
    import jiwer
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    word_alignment = jiwer.process_words(ref, hyp)
    char_ref = " ".join(list(ref.replace(" ","")))
    char_hyp = " ".join(list(hyp.replace(" ","")))
    char_alignment = jiwer.process_words(char_ref, char_hyp)
    words={"substitutions":word_alignment.substitutions,"deletions":word_alignment.deletions,"insertions":word_alignment.insertions,
           "reference_words":len(ref.split()),"reference_chars":len(ref.replace(" ",""))}
    char_edits=char_alignment.substitutions+char_alignment.deletions+char_alignment.insertions
    return {"intended_text":reference,"recognized_text":hypothesis,"normalized_reference":ref,"normalized_hypothesis":hyp,
            "wer":word_alignment.wer,"cer":char_alignment.wer,"char_edits":char_edits,**words}

def aggregate(scores: list[dict]) -> dict:
    s=sum(x["substitutions"] for x in scores); d=sum(x["deletions"] for x in scores); i=sum(x["insertions"] for x in scores)
    rw=sum(x["reference_words"] for x in scores); rc=sum(x["reference_chars"] for x in scores)
    ce=sum(x.get("char_edits",0) for x in scores)
    return {"substitutions":s,"deletions":d,"insertions":i,"reference_words":rw,"reference_chars":rc,
            "wer":(s+d+i)/rw if rw else None,"cer":ce/rc if rc else None,"sample_count":len(scores)}
