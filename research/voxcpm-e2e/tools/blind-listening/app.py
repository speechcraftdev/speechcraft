#!/usr/bin/env python3
"""Local, blinded listening test server. Standard library only."""
from __future__ import annotations
import json, mimetypes, os, re, sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE=Path(__file__).resolve().parent
DATA=Path(os.environ.get("BLIND_LISTENING_DATA", str(HERE/"data"))).resolve()
AUDIO=DATA/"audio"
STATIC=HERE/"static"
RESPONSES=DATA/"responses.json"
KEY=DATA/"answer-key.json"
SESSION=DATA/"session.json"
METRICS=DATA/"per-clip-metrics.json"

def load(path, default=None):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return default

def write_json(path, obj):
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(obj,indent=2,ensure_ascii=False))
    os.replace(temp,path)

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def all_answered(session, answers):
    return all((a:=answers.get(t["trial_id"])) and all(a.get(q) in ("A","B","Tie") for q in ("overall",)) for t in session["trials"])

def reveal_for(tid, session, key, responses):
    """Return only the requested trial's mapping, and only after its choice is saved."""
    answer=responses.get("answers",{}).get(tid,{})
    if answer.get("overall") not in ("A","B","Tie"):
        return None
    trial=next((t for t in session.get("trials",[]) if t.get("trial_id")==tid),None)
    keyed=next((t for t in key.get("trials",[]) if t.get("trial_id")==tid),None)
    if not trial or not keyed:
        return None
    metrics=load(METRICS,{})
    # Keep per-clip values separate from the collection-level TTSDS2 group.
    clip_index=metrics.get("clip_metrics",metrics) if isinstance(metrics,dict) else {}
    group_index=metrics.get("group_metrics",{}) if isinstance(metrics,dict) else {}
    prompt_metrics=(clip_index.get(keyed.get("speaker_id"),{}).get(keyed.get("reference_id"),{}).get(keyed.get("source_prompt_id"),{})
                    if isinstance(clip_index,dict) else {})
    reference_id=keyed.get("reference_id")
    group_metrics=(group_index.get(keyed.get("speaker_id"),{}).get(reference_id,{})
                   if isinstance(group_index,dict) else {})
    def side(letter):
        condition=keyed[f"{letter.lower()}_condition"]
        values=prompt_metrics.get(condition,[]) if isinstance(prompt_metrics,dict) else []
        group=group_metrics.get(condition) if isinstance(group_metrics,dict) else None
        return {"condition":"Fine-tuned" if condition=="fine_tuned" else "Zero-shot",
                "metrics":[x for x in values if isinstance(x,dict) and x.get("name") and x.get("value") is not None],
                "group_ttsds2":group if isinstance(group,dict) else None}
    return {"trial_id":tid,"reference_id":reference_id,"a":side("A"),"b":side("B"),
            "metrics_available":bool(side("A")["metrics"] or side("B")["metrics"])}

class Handler(BaseHTTPRequestHandler):
    server_version="BlindListening/1.0"
    def log_message(self, fmt, *args): print("%s - %s"%(self.client_address[0],fmt%args))
    def send(self, code, payload, ctype="application/json; charset=utf-8"):
        data=payload if isinstance(payload,bytes) else payload.encode()
        self.send_response(code); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def json_body(self):
        size=int(self.headers.get("Content-Length","0"))
        if size>200_000: raise ValueError("Request too large")
        return json.loads(self.rfile.read(size) or b"{}")
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/": return self.serve_static("index.html")
        if path.startswith("/static/"): return self.serve_static(path.removeprefix("/static/"))
        if path=="/api/session":
            session=load(SESSION); responses=load(RESPONSES,{"answers":{},"submitted_at":None})
            if not session: return self.send(503,json.dumps({"error":"Trial manifest missing. Run prepare_trials.py first."}))
            # Sanitize all trial entries defensively; manifest has no condition labels.
            safe_trials=[{k:v for k,v in t.items() if k!="repeat"} for t in session["trials"]]
            safe_inventory=[{"speaker":i["speaker"],"reference_id":i.get("reference_id"),"requested":i.get("requested"),"selected":i.get("selected",0)} for i in session["inventory"]]
            out={"trials":safe_trials,"inventory":safe_inventory,"answers":responses.get("answers",{}),"submitted_at":responses.get("submitted_at"),"trial_count":len(session["trials"])}
            return self.send(200,json.dumps(out))
        if path=="/api/results":
            responses=load(RESPONSES,{"answers":{},"submitted_at":None})
            if not responses.get("submitted_at"): return self.send(403,json.dumps({"error":"Results are concealed until final submission."}))
            report=load(DATA/"results.json",{})
            return self.send(200,json.dumps(report))
        if path=="/api/reveal":
            from urllib.parse import parse_qs
            tid=parse_qs(urlparse(self.path).query).get("trial_id",[""])[0]
            result=reveal_for(tid,load(SESSION,{}),load(KEY,{}),load(RESPONSES,{"answers":{}}))
            if result is None: return self.send(403,json.dumps({"error":"This trial remains concealed until you choose an answer."}))
            return self.send(200,json.dumps(result))
        if path.startswith("/audio/"):
            name=path[len("/audio/"):]
            if not re.fullmatch(r"(?:clip-[0-9]{3}-[ab]|anchor-[a-z0-9_-]+)\.wav",name): return self.send(404,b"Not found","text/plain")
            target=AUDIO/name
            if not target.is_file(): return self.send(404,b"Not found","text/plain")
            return self.send(200,target.read_bytes(),"audio/wav")
        return self.send(404,b"Not found","text/plain")
    def do_POST(self):
        path=urlparse(self.path).path
        try: body=self.json_body()
        except Exception as e: return self.send(400,json.dumps({"error":str(e)}))
        session=load(SESSION); saved=load(RESPONSES,{"answers":{},"submitted_at":None})
        if not session: return self.send(503,json.dumps({"error":"Trial manifest missing"}))
        if saved.get("submitted_at"): return self.send(409,json.dumps({"error":"Session has been submitted and locked."}))
        valid_ids={t["trial_id"] for t in session["trials"]}
        if path=="/api/save":
            tid=body.get("trial_id")
            if tid not in valid_ids: return self.send(400,json.dumps({"error":"Unknown trial"}))
            allowed={"overall"}
            vals={k:body.get(k) for k in allowed}
            if any(v not in (None,"A","B","Tie") for v in vals.values()): return self.send(400,json.dumps({"error":"Invalid response choice"}))
            answer=saved["answers"].get(tid,{})
            answer.update(vals)
            answer["error_a"]=bool(body.get("error_a",False)); answer["error_b"]=bool(body.get("error_b",False))
            answer["notes"]=str(body.get("notes", ""))[:2000]
            answer["listened_at"]=now()
            answer["trial_id"]=tid
            # Store requested response metadata, while retaining only anonymous playback ids.
            trial=next(t for t in session["trials"] if t["trial_id"]==tid)
            key_trial=next(k for k in load(KEY)["trials"] if k["trial_id"]==tid)
            answer["speaker"]=trial["speaker"]
            answer["source_prompt_id"]=key_trial["source_prompt_id"]
            answer["anonymous_a"]=trial["a_url"].split("/")[-1][:-4]; answer["anonymous_b"]=trial["b_url"].split("/")[-1][:-4]
            saved["answers"][tid]=answer; saved["updated_at"]=now(); write_json(RESPONSES,saved)
            reveal=reveal_for(tid,session,load(KEY),saved)
            return self.send(200,json.dumps({"saved":True,"answered":sum(all(saved["answers"].get(t["trial_id"],{}).get(q) in ("A","B","Tie") for q in ("overall",)) for t in session["trials"]),"reveal":reveal}))
        if path=="/api/submit":
            if not all_answered(session,saved["answers"]): return self.send(400,json.dumps({"error":"Answer the overall-preference question on every trial before submitting."}))
            saved["submitted_at"]=now(); saved["updated_at"]=now(); write_json(RESPONSES,saved)
            # Analysis happens only after explicit submit.
            import analyze_results
            report=analyze_results.analyze(saved, load(KEY), session)
            write_json(DATA/"results.json",report)
            return self.send(200,json.dumps({"submitted_at":saved["submitted_at"],"results_url":"/api/results"}))
        return self.send(404,b"Not found","text/plain")
    def serve_static(self, name):
        if name not in ("index.html","app.js","style.css"): return self.send(404,b"Not found","text/plain")
        p=STATIC/name
        if not p.exists(): return self.send(404,b"Not found","text/plain")
        content=p.read_bytes()
        if name=="index.html":
            title=os.environ.get("BLIND_LISTENING_TITLE")
            if title: content=content.replace(b"Blind voice comparison",title.encode("utf-8"))
        return self.send(200,content,mimetypes.guess_type(str(p))[0] or "text/plain")

def main():
    if not (SESSION.exists() and KEY.exists() and AUDIO.exists()):
        raise SystemExit("Listening set missing. Run: python tools/blind-listening/prepare_trials.py")
    port=int(os.environ.get("BLIND_LISTENING_PORT","8765"))
    print(f"Blind listening test ready at http://127.0.0.1:{port}")
    print(f"Responses: {RESPONSES}")
    ThreadingHTTPServer(("127.0.0.1",port),Handler).serve_forever()
if __name__=="__main__": main()
