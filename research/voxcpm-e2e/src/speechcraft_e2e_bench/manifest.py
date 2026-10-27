from __future__ import annotations
import os
from pathlib import Path
from typing import Any
import yaml
import hashlib
from .models import AudioItem, Dataset

class ManifestError(ValueError):
    pass

def load_dataset(path: str | Path) -> Dataset:
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ManifestError(f"Manifest does not exist: {manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ManifestError(f"Cannot parse YAML {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("Manifest root must be a YAML mapping")
    speaker = raw.get("speaker_id")
    if not isinstance(speaker, str) or not speaker.strip():
        raise ManifestError("speaker_id is required")
    prompts: dict[str, str] = {}
    for row in raw.get("prompts", []) or []:
        if not isinstance(row, dict) or not row.get("id") or not isinstance(row.get("text"), str):
            raise ManifestError("Each prompt requires a unique id and text")
        if row["id"] in prompts:
            raise ManifestError(f"Duplicate prompt id: {row['id']}")
        prompts[str(row["id"])] = row["text"]
    root = manifest_path.parent
    seen_ids: set[str] = set()
    real: list[AudioItem] = []
    conditions: dict[str, list[AudioItem]] = {}
    condition_metadata: dict[str, dict[str, Any]] = {}
    path_owners: dict[str, str] = {}
    def item(row: dict[str, Any], kind: str, condition: str | None = None) -> AudioItem:
        ident = str(row.get("id", ""))
        if not ident:
            raise ManifestError(f"{kind} audio item requires id")
        if ident in seen_ids:
            raise ManifestError(f"Duplicate audio id: {ident}")
        seen_ids.add(ident)
        p = row.get("path")
        if not p:
            raise ManifestError(f"Audio item {ident} requires path")
        audio_path = (root / os.path.expandvars(str(p))).expanduser().resolve()
        if not audio_path.is_file():
            raise ManifestError(f"Missing audio file for {ident}: {audio_path}")
        key = str(audio_path)
        if key in path_owners:
            raise ManifestError(f"Duplicate recording path used by {path_owners[key]} and {ident}: {audio_path}")
        path_owners[key] = ident
        prompt_id = row.get("prompt_id")
        if kind == "generated" and prompt_id not in prompts:
            raise ManifestError(f"Clip {ident} references unknown prompt_id {prompt_id!r}")
        meta = {k: v for k, v in row.items() if k not in {"id", "path", "transcript", "prompt_id"}}
        if condition: meta["condition"] = condition
        return AudioItem(ident, audio_path, row.get("transcript"), prompt_id, meta, kind)
    for row in raw.get("real_recordings", []) or []:
        if not isinstance(row, dict): raise ManifestError("real_recordings entries must be mappings")
        real.append(item(row, "real"))
    conds = raw.get("conditions", {}) or {}
    if not isinstance(conds, dict): raise ManifestError("conditions must be a mapping of arbitrary condition names")
    for name, body in conds.items():
        if not isinstance(body, dict): raise ManifestError(f"Condition {name} must be a mapping")
        conditions[str(name)] = [item(row, "generated", str(name)) for row in body.get("clips", []) or []]
        condition_metadata[str(name)] = body.get("metadata", {}) or {}
    content_owners: dict[str,str] = {}
    for audio_item in [*real, *(x for values in conditions.values() for x in values)]:
        digest=hashlib.sha256()
        with audio_item.path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024*1024),b""): digest.update(chunk)
        content_hash=digest.hexdigest()
        if content_hash in content_owners:
            raise ManifestError(f"Duplicate recording content in {content_owners[content_hash]} and {audio_item.id}")
        content_owners[content_hash]=audio_item.id
    attempts = raw.get("attempts", []) or []
    attempts_complete=raw.get("attempts_complete",False)
    if not isinstance(attempts_complete,bool): raise ManifestError("attempts_complete must be true or false")
    attempt_ids: set[str] = set()
    generated_by_id={x.id:(condition,x) for condition,items in conditions.items() for x in items}
    for attempt in attempts:
        if not isinstance(attempt, dict) or not attempt.get("id"):
            raise ManifestError("Each attempt requires an id")
        aid = str(attempt["id"])
        if aid in attempt_ids: raise ManifestError(f"Duplicate generation attempt id: {aid}")
        attempt_ids.add(aid)
        if attempt.get("prompt_id") not in prompts: raise ManifestError(f"Attempt {aid} references unknown prompt_id {attempt.get('prompt_id')!r}")
        if attempt.get("condition") not in conditions: raise ManifestError(f"Attempt {aid} references unknown condition {attempt.get('condition')!r}")
        if attempt.get("clip_id") and attempt["clip_id"] not in generated_by_id: raise ManifestError(f"Attempt {aid} references unknown generated clip_id {attempt['clip_id']!r}")
        if attempt.get("clip_id") and generated_by_id[attempt["clip_id"]][0]!=attempt.get("condition"): raise ManifestError(f"Attempt {aid} condition does not match clip {attempt['clip_id']}")
        if attempt.get("output_path"):
            attempt["output_path"]=str((root / os.path.expandvars(str(attempt["output_path"]))).expanduser().resolve())
    if attempts_complete:
        linked={str(a.get("clip_id")) for a in attempts if a.get("clip_id")}
        linked.update(str(x.metadata.get("generation_attempt_id")) for _,items in conditions.items() for x in items if x.metadata.get("generation_attempt_id"))
        linked.update(str(x.metadata.get("attempt_id")) for _,items in conditions.items() for x in items if x.metadata.get("attempt_id"))
        for condition,items in conditions.items():
            for clip in items:
                aid=clip.metadata.get("generation_attempt_id") or clip.metadata.get("attempt_id")
                if clip.id not in linked and aid not in attempt_ids:
                    raise ManifestError(f"attempts_complete is true but generated clip {clip.id} is not linked to an attempt")
    return Dataset(manifest_path, speaker, real, prompts, conditions, condition_metadata, attempts, raw)

def validate_dataset(ds: Dataset, decode: bool = True) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not ds.real: issues.append({"severity":"error", "message":"No real reference recordings; speaker similarity and TTSDS2 require real audio."})
    if not ds.prompts: issues.append({"severity":"warning", "message":"No prompts; generated clips and WER/CER cannot be evaluated."})
    if not ds.conditions: issues.append({"severity":"warning", "message":"No conditions defined."})
    for item in [*ds.real, *(x for xs in ds.conditions.values() for x in xs)]:
        try:
            if decode:
                from .audio import load_audio
                data, sr = load_audio(item.path)
                if data.size == 0 or sr <= 0: raise ValueError("empty decoded audio")
            else:
                import soundfile as sf
                info = sf.info(str(item.path))
                if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0: raise ValueError("empty or invalid audio stream")
        except Exception as exc:
            issues.append({"severity":"error", "message":f"{item.id}: unreadable audio ({exc})"})
    for r in ds.real:
        if not r.transcript: issues.append({"severity":"warning", "message":f"Real recording {r.id} has no transcript; cannot provide reference-ASR baseline."})
    for name, clips in ds.conditions.items():
        if not clips: issues.append({"severity":"warning", "message":f"Condition {name} has no supplied clips."})
    return issues
