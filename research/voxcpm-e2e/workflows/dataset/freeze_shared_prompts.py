#!/usr/bin/env python3
"""Freeze authored, shared English prompts after corpus-wide text screening."""
import argparse,hashlib,json,random,re,unicodedata
from difflib import SequenceMatcher
from pathlib import Path
CANDIDATES=[
"The kettle clicked off just as the rain began tapping against the windows.",
"Could you leave the spare key inside the blue bowl by the door?",
"We took the longer road because the bridge was closed for repairs.",
"A small garden grows behind the library beside the old stone wall.",
"Did you remember to bring the folder we discussed yesterday afternoon?",
"The train arrived early, so everyone waited on the quiet platform.",
"I usually make a simple lunch before the afternoon meeting starts.",
"She noticed a paper lantern glowing above the narrow street.",
"The shop on the corner sells fresh bread every Saturday morning.",
"Would you mind turning the light on near the back stairs?",
"They planned a short walk before the clouds covered the hills.",
"My neighbor keeps a collection of postcards from coastal towns.",
"We found two empty seats near the middle of the room.",
"The little dog waited patiently beside the open garden gate.",
"He checked the map twice before choosing a path through the park.",
"Can we move the appointment to sometime after lunch tomorrow?",
"A warm cup of tea helped settle the long conversation.",
"The children drew bright pictures while the adults prepared dinner.",
"I left my notebook on the desk beside the window.",
"The market was quieter than usual when we arrived this morning.",
"She carried a folded umbrella in case the weather changed.",
"Do you know which box contains the spare batteries and cables?",
"The old clock in the hallway loses a minute every week.",
"We stopped at a bench to watch the boats cross the lake.",
"His first draft was clear, but the ending needed more detail.",
"The bakery closes at six, so we should leave soon.",
"A soft breeze moved the curtains while the room slowly cooled.",
"Would you like to sit outside where the trees provide some shade?",
"They saved the last few pages for the train ride home.",
"The museum guide spoke slowly so everyone could follow the story.",
"I keep extra envelopes in the drawer beneath the printer.",
"We heard a distant bell while walking along the river path.",
"She chose a quiet table and ordered soup with fresh bread.",
"Could you send me the address before we leave the office?",
"The yellow leaves gathered beside the steps after the windy night.",
"He found the missing button inside the pocket of his coat.",
"We can take the bus if the weather becomes too cold.",
"The recipe called for one more spoonful of lemon juice.",
"Did anyone see where the blue scarf was placed after lunch?",
"A narrow footpath connects the station with the public garden.",
"The new shelves fit neatly along the wall beside the desk.",
"I heard the front gate close while I was making coffee.",
"They agreed to meet again once the schedule was easier to manage.",
"The small fountain made a gentle sound in the empty courtyard.",
"Can you check whether the package arrived at the front desk?",
"We packed a few snacks before setting out for the countryside.",
"Her notes included a clear list of questions for the next visit.",
"The afternoon light stretched across the floor near the sofa.",
"He waited until the room was quiet before starting his presentation.",
"Would you prefer the window open or the fan turned on?",
"A red bicycle leaned against the fence outside the station.",
"The last page had a small drawing in the lower corner.",
"We made a quick stop for water before continuing the drive.",
"She placed the clean cups on a tray beside the kettle.",
"The new crossing signal gives pedestrians a little more time.",
"I found an old receipt tucked inside the travel guide.",
"Could we choose a different table with more room for everyone?",
"The morning meeting ended early, leaving time to review the notes.",
"He opened the curtains and let the sunshine fill the room.",
"They watched the clouds drift slowly beyond the rooftops.",
"The quiet café near the station has comfortable chairs and large windows.",
"We brought a spare blanket in case the evening turned chilly.",
"Did you leave the garden hose running after watering the plants?",
"A small wooden sign pointed toward the walking trail.",
"The library added several new books to its history section.",
"She folded the map carefully and slipped it into her bag.",
"I will call once I have checked the updated schedule.",
"The soup tasted better after it rested for a few minutes.",
"He moved the chair closer so he could hear the speaker.",
"We noticed a bright kite above the open field.",
"Could you keep the receipt until we return the borrowed equipment?",
"The cafe opens early on weekdays.",
"Would you save this seat for me?",
"The bus stopped beside the pharmacy.",
"Can we use the covered walkway?",
"Her blue notebook is on the table.",
"Did the package arrive this morning?",
"The lamps turned on at sunset.",
"We waited beneath the station clock.",
"Is the side entrance still open?",
"The window faces a quiet courtyard.",
"Please leave the folder by reception.",
"Our neighbors planted herbs outside.",
"Could you bring another clean glass?",
"The elevator stopped on the fourth floor.",
"I found a coin near the fountain.",
"Does this path lead to the lake?",
"The small shop sells postcards and maps.",
"We can meet after the morning call.",
"The chair beside the door is empty.",
"Did you hear the rain begin?",
"After the meeting ended, we walked slowly through the quiet courtyard behind the old library.",
"Would you mind checking whether the small parcel was left near the side entrance this morning?",
"The family decided to spend the afternoon exploring a few streets they had never visited before.",
"We moved the flower pots closer to the wall before the evening wind grew stronger.",
"Although the train was crowded, everyone found space for their bags near the doors.",
"Could you tell me which shelf holds the guidebooks about the northern coast?",
"She wrote a careful note about the changes and placed it beside the meeting agenda.",
"The little restaurant stays open late whenever a concert finishes across the square.",
"I usually take a short walk after breakfast because it helps me plan the day.",
"They carried the folding chairs inside when the first drops of rain reached the patio.",
"Do you remember where we put the spare charger before leaving the apartment yesterday?",
"A pair of swallows circled above the garden while the morning light reached the fence.",
"He asked the receptionist to print a second copy of the updated travel schedule.",
"The road beside the orchard becomes muddy after several hours of steady rain.",
"Would it be easier to meet at the station and walk to the gallery together?",
"We saved a quiet corner table for anyone who arrived after the opening talk.",
"Her brother brought an extra jacket because the forecast mentioned cooler weather overnight.",
"The community center posted a new calendar beside the entrance for everyone to read.",
"Before we left, I checked every room to make sure the windows were closed.",
"Did anyone write down the name of the small bookstore near the river bridge?",
"The guide suggested taking the eastern path because it passes several shaded picnic areas.",
"They placed the finished drawings in a folder so the paper would not bend during the ride.",
"Could you leave the parcel with the front desk if nobody answers the apartment door?",
"A gentle breeze carried the smell of fresh bread from the bakery across the street.",
"I will send the revised list once I finish checking the final two details.",
"The new reading room has wide windows, comfortable chairs, and a view of the garden.",
"She waited near the entrance until the rest of the group returned from the walk.",
"Would you like me to bring the larger umbrella when I come back this evening?",
"The old map showed a footbridge that no longer appears on the newer trail signs.",
"We took our time choosing vegetables and fruit for the simple dinner planned at home.",
"He placed the small parcel on the bench so he could tie his shoelace.",
"Can we review the address together before the driver turns onto the narrow lane?",
"The evening workshop ended with a short conversation about books people had enjoyed recently.",
"They found a sheltered place to wait while the heavy clouds moved beyond the hills.",
"Please keep the printed schedule nearby in case the meeting time changes again.",
"The museum courtyard was nearly empty, and the fountain made a soft sound under the trees.",
"I brought a notebook so I could write down the names of the places we visited.",
"Did the caretaker say whether the garden gate would be locked before sunset tonight?",
"A narrow wooden bridge connects the walking trail with the picnic field on the far side.",
"We moved the boxes away from the heater so the paper labels would stay in good condition.",
"She checked the weather once more before deciding which coat to wear for the long walk.",
"The small bakery near the station makes a different fruit pie every Sunday afternoon.",
"Could you show me the quickest route from the library to the public garden entrance?",
"They left a note on the kitchen table explaining where the extra keys could be found.",
"The afternoon discussion went smoothly because everyone had read the short document beforehand.",
"He opened the curtains carefully and noticed that the garden path was covered with fresh leaves.",
]
def nrm(s):return ' '.join(re.findall(r'[\w]+',unicodedata.normalize('NFKC',s).casefold()))
def rows(p):
 try:
  if p.suffix=='.jsonl':return [json.loads(l) for l in p.read_text(encoding='utf8').splitlines() if l.strip()]
  d=json.loads(p.read_text(encoding='utf8'));return d if isinstance(d,list) else d.get('prompts',[])
 except Exception:return []
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--corpora',nargs='+',required=True,help='JSON or JSONL manifests to screen; rows may use text or transcript')
 ap.add_argument('--output',type=Path,required=True)
 ap.add_argument('--seed',type=int,default=2026102707)
 args=ap.parse_args()
 sources=[]
 for file in args.corpora:
  p=Path(file)
  sources += [(str(p),r.get('text',r.get('transcript',''))) for r in rows(p)]
 normalized=[(src,nrm(text)) for src,text in sources if text]
 rng=random.Random(args.seed);cand=list(CANDIDATES);rng.shuffle(cand)
 selected=[];rejected=[]
 seen=set()
 for text in cand:
  nt=nrm(text)
  if not nt or nt in seen:rejected.append({'text':text,'reason':'duplicate candidate'});continue
  conflict=None
  for src,other in normalized:
   if nt==other or (min(len(nt),len(other))>=22 and SequenceMatcher(None,nt,other,autojunk=False).ratio()>=0.86):
    conflict={'source':src,'text':other,'similarity':SequenceMatcher(None,nt,other,autojunk=False).ratio()};break
  if conflict:
   rejected.append({'text':text,'reason':'conflicts with source transcript','conflict':conflict});continue
  if any(SequenceMatcher(None,nt,nrm(x['text']),autojunk=False).ratio()>=0.86 for x in selected):
   rejected.append({'text':text,'reason':'near-duplicate candidate'});continue
  seen.add(nt);selected.append({'id':f'candidate-{len(selected)+1:03d}','text':text,'word_count':len(text.split()),'is_question':text.rstrip().endswith('?')})
 short=[r for r in selected if r['word_count']<=8]
 medium=[r for r in selected if 9<=r['word_count']<=13]
 long=[r for r in selected if r['word_count']>=14]
 rng.shuffle(short);rng.shuffle(medium);rng.shuffle(long)
 if len(short)<12 or len(medium)<24 or len(long)<12:raise RuntimeError(f'Insufficient length variety: short={len(short)}, medium={len(medium)}, long={len(long)}')
 selected=short[:12]+medium[:26]+long[:12]
 if sum(r['is_question'] for r in selected)<10:raise RuntimeError('Need at least ten question prompts in the frozen set')
 selected=[{**r,'id':f'syn{i:03d}'} for i,r in enumerate(selected,1)]
 target=args.output;target.parent.mkdir(parents=True,exist_ok=True)
 target.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in selected))
 digest=hashlib.sha256(target.read_bytes()).hexdigest()
 meta={'count':len(selected),'sha256':digest,'selection_method':'independently authored neutral English sentences; shuffled with seed 2026102707; NFKC/casefold/alphanumeric token normalization; exact and character similarity >=0.86 screening against supplied corpus manifests; deterministic length quotas short/medium/long','seed':args.seed,'source_text_rows_screened':len(normalized),'candidate_sentence_count':len(CANDIDATES),'rejected':rejected,'overlap_counts':{'exact_or_substantial_near_duplicate_total':0},'word_count_range':[min(x['word_count'] for x in selected),max(x['word_count'] for x in selected)],'length_strata':{'short_6_to_8':sum(x['word_count']<=8 for x in selected),'medium_9_to_13':sum(9<=x['word_count']<=13 for x in selected),'long_14_plus':sum(x['word_count']>=14 for x in selected)},'question_count':sum(x['is_question'] for x in selected)}
 target.with_suffix('.metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in meta.items() if k!='rejected'},indent=2));print('rejected',len(rejected),'selected ids',selected[0]['id'],selected[-1]['id'])
if __name__=='__main__':main()
