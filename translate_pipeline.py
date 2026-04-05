#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Tuple

from query_kb import hybrid_search

DEFAULT_PARAMS={"balanced":{
    "chunk_chars":600,"chunk_chars_min":450,"chunk_chars_max":700,
    "batch_chunks":4,"kb_topk":5,"lock_score_threshold":0.35,
    "lock_margin_threshold":0.03,"max_repair_rounds":2,
    "promotion_min_frequency":5,"promotion_pass_runs":3,
    "unresolved_policy":"keep_en_with_tag","placeholder_strict":True,
    "bootstrap_score_threshold":0.12,"bootstrap_margin_threshold":0.0003,
    "bootstrap_min_frequency":1,"bootstrap_max_rules":300,
    "drift_forbid_min_count":2,
}}

RE_TERM_CANDIDATE=re.compile(r"\b(?:[A-Z][A-Za-z0-9'+/\-]{2,}|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-z0-9'+/\-]{2,}|[A-Z]{2,})){0,3}\b")
RE_PLACEHOLDER=re.compile(r"\{[^{}\n]+\}|%\d*\$?[sdif]|%[sdif]")
RE_NUMERIC=re.compile(r"\d+(?:\.\d+)?%?")
RE_UNRESOLVED=re.compile(r"\[\[TERM_UNRESOLVED:([^\]]+)\]\]")
RE_CJK_TOKEN=re.compile(r"[\u4e00-\u9fff]{2,12}")

HIGH_VALUE_KEYWORDS={
    "bat","beaver","fox","frog","harpy","human","lizard","seal","species","race",
    "hearth","house","mine","smelter","furnace","academy","court","grill","foundry","smithy",
    "workshop","camp","warehouse","tavern","forum","temple","monastery","kiln","cooperage",
    "firekeeper","worker","scout","builder","carrier","miner","smith","job","profession",
    "cornerstone","perk","resolve","hostility","reputation","impatience","blight","rainpunk",
    "glade","danger","forbidden","effect","status","mechanic","dedication",
}
BOOTSTRAP_SOURCE_BLOCKLIST={"label","tooltip","header","objective","content","popup","dialogue","cycle","goal","npc","display","reward","order","news","menu","wiki"}
COLLAPSE_BLOCKLIST_FIRST={"perk","effect","category","label","goal","order","reward","news","menu","wiki"}
CATEGORY_PRIORITY={"profession":6,"race":5,"building":4,"cornerstone":3,"status":2,"mechanic":1}

@dataclass
class Chunk:
    chunk_id:str
    text:str
    source_sentences:List[str]

class HtmlTextExtractor(HTMLParser):
    def __init__(self)->None:
        super().__init__();self.parts:List[str]=[];self.skip=0
    def handle_starttag(self,tag:str,attrs:List[Tuple[str,str|None]])->None:
        t=tag.lower()
        if t in {"script","style"}: self.skip+=1
        elif t in {"p","div","section","article","li","br","h1","h2","h3","h4","h5","h6"}: self.parts.append("\n")
    def handle_endtag(self,tag:str)->None:
        t=tag.lower()
        if t in {"script","style"} and self.skip>0: self.skip-=1
        elif t in {"p","div","section","article","li","br","h1","h2","h3","h4","h5","h6"}: self.parts.append("\n")
    def handle_data(self,data:str)->None:
        if self.skip>0:return
        s=data.strip()
        if s:self.parts.append(s)
    def text(self)->str:
        merged=html.unescape(" ".join(self.parts))
        merged=re.sub(r"\n\s*\n+","\n\n",merged)
        merged=re.sub(r"[ \t]+"," ",merged)
        return merged.strip()

class OpenAICompatClient:
    def __init__(self,model:str,api_key:str,base_url:str,timeout:int=120)->None:
        self.model=model;self.api_key=api_key;self.base_url=base_url.rstrip("/");self.timeout=timeout
    def chat_json(self,system_prompt:str,user_prompt:str)->Dict[str,Any]:
        req=urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps({
                "model":self.model,"temperature":0,"response_format":{"type":"json_object"},
                "messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],
            }).encode("utf-8"),
            headers={"Content-Type":"application/json","Authorization":f"Bearer {self.api_key}"},method="POST")
        try:
            with urllib.request.urlopen(req,timeout=self.timeout) as resp: body=resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail=e.read().decode("utf-8",errors="ignore") if e.fp else ""
            raise RuntimeError(f"LLM HTTP error {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM connection error: {e}") from e
        raw=json.loads(body);content=raw["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content=re.sub(r"^```(?:json)?\s*","",content);content=re.sub(r"\s*```$","",content)
        try:return json.loads(content)
        except json.JSONDecodeError:
            m=re.search(r"\{[\s\S]*\}",content)
            if not m: raise
            return json.loads(m.group(0))

def load_text(path:Path)->str:
    raw=path.read_text(encoding="utf-8-sig");ext=path.suffix.lower()
    if ext in {".html",".htm"}:
        p=HtmlTextExtractor();p.feed(raw);return p.text()
    return raw.strip()

def split_sentences(text:str)->List[str]:
    t=text.strip()
    if not t:return []
    parts=re.findall(r".+?(?:[。！？!?]|(?<!\d)\.(?!\d)|$)",t,flags=re.S)
    out=[p.strip() for p in parts if p.strip()]
    return out if out else [t]

def split_long_paragraph(text:str,min_chars:int,max_chars:int)->List[str]:
    sents=split_sentences(text)
    if not sents:return []
    blocks=[];buf=[];cur=0
    for sent in sents:
        add=len(sent)+(1 if buf else 0)
        if buf and cur+add>max_chars:
            blocks.append(" ".join(buf).strip());buf=[sent];cur=len(sent)
        else:
            buf.append(sent);cur+=add
    if buf: blocks.append(" ".join(buf).strip())
    if len(blocks)>=2 and len(blocks[-1])<min_chars:
        blocks[-2]=f"{blocks[-2]} {blocks[-1]}".strip();blocks.pop()
    return blocks

def build_chunks(text:str,params:Dict[str,Any])->List[Chunk]:
    min_chars=int(params["chunk_chars_min"]);max_chars=int(params["chunk_chars_max"]);target=int(params["chunk_chars"])
    paras=[p.strip() for p in re.split(r"\n\s*\n+",text) if p.strip()]
    if not paras:return []
    pieces=[]
    for para in paras:
        pieces.extend([para] if len(para)<=max_chars else split_long_paragraph(para,min_chars,max_chars))
    chunks=[];buf=[];cur=0
    def flush()->None:
        nonlocal buf,cur
        if not buf:return
        txt="\n\n".join(buf).strip();cid=f"c_{len(chunks)+1:04d}"
        chunks.append(Chunk(chunk_id=cid,text=txt,source_sentences=split_sentences(txt)));buf=[];cur=0
    for piece in pieces:
        add=len(piece)+(2 if buf else 0)
        if buf and (cur+add>max_chars or cur>=target): flush()
        buf.append(piece);cur+=add
    flush();return chunks

def load_overrides(path:Path)->List[Dict[str,Any]]:
    if not path.exists(): return []
    data=json.loads(path.read_text(encoding="utf-8-sig"));rules=data.get("rules",[])
    return [r for r in rules if r.get("enabled",True)]

def save_overrides(path:Path,rules:List[Dict[str,Any]])->None:
    path.write_text(json.dumps({"version":1,"rules":rules},ensure_ascii=False,indent=2),encoding="utf-8")

def normalize_rule(r:Dict[str,Any])->Dict[str,Any]:
    rule=dict(r)
    rule["id"]=str(rule.get("id","")).strip() or "rule"
    rule["source"]=str(rule.get("source","")).strip()
    rule["target"]=str(rule.get("target","")).strip()
    rule["match"]=str(rule.get("match","exact_ci")).strip() or "exact_ci"
    rule["priority"]=int(rule.get("priority",90))
    rule["scope"]=str(rule.get("scope","global")).strip() or "global"
    rule["enabled"]=bool(rule.get("enabled",True))
    forbid=[]
    for x in rule.get("forbid",[]):
        s=str(x).strip()
        if s and s!=rule["target"] and s not in forbid:
            forbid.append(s)
    rule["forbid"]=forbid
    return rule

def normalize_rules(rules:List[Dict[str,Any]])->List[Dict[str,Any]]:
    out=[];seen=set()
    for raw in rules:
        r=normalize_rule(raw)
        if not r["source"] or not r["target"]: continue
        k=r["source"].lower()
        if k in seen: continue
        seen.add(k);out.append(r)
    return out

def to_title_term(s:str)->str:
    parts=[p for p in re.split(r"\s+",s.strip()) if p]
    if not parts: return ""
    return " ".join(p[0:1].upper()+p[1:].lower() if re.search(r"[A-Za-z]",p) else p for p in parts)

def classify_high_value_category(key:str,domain:str,entity:str,de_norm:str)->str:
    key_l=key.lower();domain_l=domain.lower();hay=" ".join([key_l,domain_l,entity.lower(),de_norm.lower()])
    if domain_l=="race" or key_l.startswith("race_") or "_race_" in key_l: return "race"
    if domain_l=="building" or key_l.startswith("building_"): return "building"
    if domain_l=="profession" or key_l.startswith("profession_") or "firekeeper" in hay: return "profession"
    if domain_l=="perk" or "cornerstone" in hay: return "cornerstone"
    if "status" in hay or domain_l in {"resolve","need","modifier","threat"}: return "status"
    if any(k in hay for k in {"rainpunk","blight","glade","hostility","impatience","reputation","seal","water","hearth"}): return "mechanic"
    return ""

def collect_bootstrap_candidates(conn:sqlite3.Connection)->Dict[str,Dict[str,Any]]:
    cur=conn.cursor()
    cur.execute(
        """
        SELECT key, zh, domain, entity, slot, de_norm
        FROM records
        WHERE lower(slot) IN ('name','title','label','header')
        """
    )
    freq=Counter()
    seed={}
    for key,zh,domain,entity,slot,de_norm in cur.fetchall():
        key=str(key or "");zh=str(zh or "");domain=str(domain or "");entity=str(entity or "");de_norm=str(de_norm or "")
        category=classify_high_value_category(key,domain,entity,de_norm)
        if not category: continue
        source_raw=entity if entity else de_norm
        source=to_title_term(source_raw)
        source_tokens=[t.lower() for t in re.split(r"\s+",source) if t]
        if len(source)<3 or not re.search(r"[A-Za-z]",source): continue
        if len(source_tokens)==0 or len(source_tokens)>4: continue
        if any(t in BOOTSTRAP_SOURCE_BLOCKLIST for t in source_tokens): continue
        variants=[source]
        if len(source_tokens)>=2:
            first=source_tokens[0]
            if len(source_tokens)==2 and first not in COLLAPSE_BLOCKLIST_FIRST:
                camel=source_tokens[0].capitalize()+"".join(t.lower() for t in source_tokens[1:])
                for v in [camel]:
                    if len(v)>=3 and re.search(r"[A-Za-z]",v):
                        variants.append(v)
        for v in variants:
            lk=v.lower()
            freq[lk]+=1
            if lk not in seed:
                seed[lk]={"source":v,"target":zh,"key":key,"count":0,"category":category}
    for lk in list(seed.keys()):
        seed[lk]["count"]=int(freq[lk])
    return seed

def bootstrap_rules_from_kb(
    conn:sqlite3.Connection,
    kb_dir:Path,
    rules:List[Dict[str,Any]],
    params:Dict[str,Any],
    cache:Dict[str,List[Tuple[float,Dict[str,Any]]]],
)->List[str]:
    existing={str(r.get("source","")).lower() for r in rules}
    candidates=collect_bootstrap_candidates(conn)
    if not candidates: return []

    bootstrap_added=[]
    ranked=sorted(
        candidates.values(),
        key=lambda x:(-int(CATEGORY_PRIORITY.get(str(x.get("category","")),0)),-x["count"],len(str(x["source"]).split()),x["source"])
    )
    max_rules=int(params["bootstrap_max_rules"])
    for c in ranked:
        if len(bootstrap_added)>=max_rules: break
        src=c["source"];lk=src.lower()
        if lk in existing: continue
        if int(c["count"])<int(params["bootstrap_min_frequency"]): continue
        eval_query=str(c.get("key","")).strip() or src
        hits=kb_search_cached(conn,kb_dir,eval_query,2,cache)
        if not hits: continue
        top1=hits[0][0];top2=hits[1][0] if len(hits)>1 else 0.0;margin=float(top1)-float(top2)
        if float(top1)<float(params["bootstrap_score_threshold"]): continue
        if float(margin)<float(params["bootstrap_margin_threshold"]): continue
        top_rec=hits[0][1]
        if str(top_rec.get("slot","")).lower() not in {"name","title","label","header"}: continue
        if str(top_rec.get("key",""))!=str(c.get("key","")): continue
        target=str(c["target"]).strip() or str(top_rec.get("zh","")).strip()
        if not target: continue
        rid=f"bootstrap_{re.sub(r'[^a-z0-9]+','_',lk).strip('_')}"
        rules.append(normalize_rule({
            "id":rid,"source":src,"target":target,"match":"exact_ci",
            "priority":95,"scope":str(c.get("category","global")),"forbid":[],"enabled":True,
        }))
        existing.add(lk);bootstrap_added.append(src)
    return bootstrap_added

def get_drift_history_path(overrides_path:Path)->Path:
    return overrides_path.with_name("term_drift_history.json")

def load_drift_history(path:Path)->Dict[str,Dict[str,int]]:
    if not path.exists(): return {}
    raw=json.loads(path.read_text(encoding="utf-8-sig"))
    out={}
    for source,aliases in raw.items():
        s=str(source).strip().lower()
        if not s or not isinstance(aliases,dict): continue
        out[s]={str(a):int(c) for a,c in aliases.items() if str(a).strip() and int(c)>=0}
    return out

def save_drift_history(path:Path,history:Dict[str,Dict[str,int]])->None:
    path.write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding="utf-8")

def extract_cjk_aliases(text:str)->List[str]:
    return [m.group(0).strip() for m in RE_CJK_TOKEN.finditer(text or "")]

def update_drift_history(history:Dict[str,Dict[str,int]],violations:List[Dict[str,Any]])->None:
    for v in violations:
        if v.get("type")!="forbidden_term": continue
        source=str(v.get("source_term","")).strip().lower()
        if not source: continue
        expected=str(v.get("expected","")).strip()
        actual=str(v.get("actual","")).strip()
        if not actual: continue
        aliases=extract_cjk_aliases(actual)
        if not aliases: aliases=[actual]
        bucket=history.setdefault(source,{})
        for a in aliases:
            if not a or a==expected: continue
            bucket[a]=int(bucket.get(a,0))+1

def merge_drift_forbid_into_rules(
    rules:List[Dict[str,Any]],
    drift_history:Dict[str,Dict[str,int]],
    min_count:int,
)->List[Tuple[str,str]]:
    added=[]
    by_source={str(r.get("source","")).lower():r for r in rules}
    for source,aliases in drift_history.items():
        rule=by_source.get(source)
        if not rule: continue
        forbid=list(rule.get("forbid",[]))
        target=str(rule.get("target","")).strip()
        for alias,count in aliases.items():
            if int(count)<int(min_count): continue
            alias=str(alias).strip()
            if len(alias)>16 or "\n" in alias: continue
            if re.search(r"[A-Za-z0-9]",alias): continue
            if not alias or alias==target or alias in forbid: continue
            forbid.append(alias);added.append((rule.get("source",""),alias))
        rule["forbid"]=forbid
    return added

def contains_term(text:str,term:str)->bool:
    if not term:return False
    esc=re.escape(term)
    if re.search(r"[A-Za-z]",term):
        return bool(re.search(rf"(?i)(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])",text))
    return term in text

def replace_term_occurrences(text:str,term:str,repl:str)->Tuple[str,int]:
    if not text or not term or not repl:return text,0
    esc=re.escape(term)
    if re.search(r"[A-Za-z]",term):
        pat=re.compile(rf"(?i)(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])")
        return pat.subn(repl,text)
    n=text.count(term)
    return text.replace(term,repl),n

def enforce_locked_terms(chunk:Chunk,translated_sentences:List[str],locked_terms:List[Dict[str,Any]])->Tuple[List[str],int]:
    src_sents=chunk.source_sentences
    out=list(translated_sentences)
    if len(out)<len(src_sents): out.extend([""]*(len(src_sents)-len(out)))
    if len(out)>len(src_sents): out=out[:len(src_sents)]
    replacement_count=0
    for i,src in enumerate(src_sents):
        text=out[i]
        for term in locked_terms:
            src_term=str(term.get("source","")).strip()
            target_term=str(term.get("target","")).strip()
            if not src_term or not target_term or not contains_term(src,src_term): continue
            text,n=replace_term_occurrences(text,src_term,target_term);replacement_count+=n
            for bad in term.get("forbid",[]):
                bad=str(bad).strip()
                if not bad: continue
                text,n=replace_term_occurrences(text,bad,target_term);replacement_count+=n
        out[i]=text
    return out,replacement_count

def extract_candidates(text:str)->List[str]:
    seen=set();out=[]
    for m in RE_TERM_CANDIDATE.finditer(text):
        token=m.group(0).strip()
        if len(token)<3:continue
        k=token.lower()
        if k in seen:continue
        seen.add(k);out.append(token)
    return out

def match_overrides(text:str,rules:List[Dict[str,Any]])->List[Dict[str,Any]]:
    out=[]
    for r in rules:
        src=str(r.get("source","")).strip();tgt=str(r.get("target","")).strip()
        if not src or not tgt:continue
        mode=str(r.get("match","exact_ci"))
        matched=(src.lower() in text.lower()) if mode=="contains_ci" else contains_term(text,src)
        if matched:
            out.append({"source":src,"target":tgt,"key":r.get("id","override"),"score":1.0,"source_type":"override","forbid":list(r.get("forbid",[]))})
    return out

def kb_search_cached(conn:sqlite3.Connection,kb_dir:Path,query:str,topk:int,cache:Dict[str,List[Tuple[float,Dict[str,Any]]]])->List[Tuple[float,Dict[str,Any]]]:
    key=query.strip().lower()
    if key in cache:return cache[key]
    hits=hybrid_search(conn=conn,kb_dir=kb_dir,query=query,topk=max(1,topk),fts_topk=max(20,topk),vec_topk=max(20,topk),w_fts=0.55,w_vec=0.45,model_name_override=None,disable_semantic=False)
    cache[key]=hits;return hits

def build_terms_for_chunk(chunk:Chunk,rules:List[Dict[str,Any]],conn:sqlite3.Connection,kb_dir:Path,params:Dict[str,Any],cache:Dict[str,List[Tuple[float,Dict[str,Any]]]])->Tuple[List[Dict[str,Any]],List[Dict[str,Any]]]:
    locked={};soft=[]
    for t in match_overrides(chunk.text,rules): locked[t["source"].lower()]=t
    for cand in extract_candidates(chunk.text):
        ck=cand.lower()
        if ck in locked:continue
        hits=kb_search_cached(conn,kb_dir,cand,int(params["kb_topk"]),cache)
        if not hits:continue
        top_score,top_rec=hits[0];second_score=hits[1][0] if len(hits)>1 else 0.0;margin=top_score-second_score
        obj={"source":cand,"target":top_rec.get("zh",""),"key":top_rec.get("key",""),"score":round(float(top_score),6),"source_type":"kb","forbid":[]}
        if top_rec.get("zh") and float(top_score)>=float(params["lock_score_threshold"]) and float(margin)>=float(params["lock_margin_threshold"]): locked[ck]=obj
        else: soft.append(obj)
    return sorted(locked.values(),key=lambda x:len(x["source"]),reverse=True),soft

def build_translate_prompts(batch:List[Dict[str,Any]])->Tuple[str,str]:
    system_prompt=("You are a professional game localization translator. "
                   "Translate source sentences to Simplified Chinese (zh-CN) with strict term compliance.")
    user_payload={
        "task":"Translate each item to zh-CN.",
        "rules":[
            "Return JSON only.",
            "Keep placeholders exactly unchanged, such as {0}, {foo}, %s, %d.",
            "Keep numbers and percentages unchanged.",
            "If a source term appears in source sentence, output must use the required target term.",
            "Never use forbidden terms.",
            "Output format: {'items':[{'chunk_id':'...','translated_sentences':['...']}]}.",
        ],
        "items":batch,
    }
    return system_prompt,json.dumps(user_payload,ensure_ascii=False)

def run_translation_batch(client:OpenAICompatClient,batch_items:List[Dict[str,Any]])->Dict[str,List[str]]:
    sp,up=build_translate_prompts(batch_items);parsed=client.chat_json(system_prompt=sp,user_prompt=up)
    out={}
    for it in parsed.get("items",[]):
        cid=str(it.get("chunk_id","")).strip();sents=[str(x).strip() for x in it.get("translated_sentences",[])]
        if cid:out[cid]=sents
    return out

def write_json(path:Path,obj:Dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")

def load_json(path:Path)->Dict[str,Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))

def build_codex_paths(base_dir:Path)->Dict[str,Path]:
    return {
        "translation_job":base_dir/"codex.translation.job.json",
        "translation_result":base_dir/"codex.translation.result.json",
    }

def build_codex_repair_paths(base_dir:Path,round_id:int)->Dict[str,Path]:
    return {
        "repair_job":base_dir/f"codex.repair.r{round_id}.job.json",
        "repair_result":base_dir/f"codex.repair.r{round_id}.result.json",
    }

def load_codex_translation_result(path:Path,chunks:List[Chunk])->Dict[str,List[str]]:
    payload=load_json(path);items=payload.get("items",[])
    out={}
    for it in items:
        cid=str(it.get("chunk_id","")).strip()
        sents=[str(x).strip() for x in it.get("translated_sentences",[])]
        if cid: out[cid]=sents
    for c in chunks:
        if c.chunk_id not in out:
            out[c.chunk_id]=list(c.source_sentences)
    return out

def load_codex_repair_result(path:Path)->List[Dict[str,Any]]:
    payload=load_json(path)
    items=payload.get("items",[])
    out=[]
    for it in items:
        out.append({
            "chunk_id":str(it.get("chunk_id","")).strip(),
            "sentence_id":int(it.get("sentence_id",-1)),
            "translated":str(it.get("translated","")).strip(),
        })
    return out

def build_repair_prompt(tasks:List[Dict[str,Any]])->Tuple[str,str]:
    sp=("You repair zh-CN translated sentences for game localization. "
        "Only fix term/placeholder/number violations; keep meaning and style concise.")
    up={
        "task":"Repair sentences.",
        "rules":[
            "Return JSON only.","Do not change placeholders.","Do not change numbers.",
            "Use required target terms when source terms appear.",
            "Output format: {'items':[{'chunk_id':'...','sentence_id':0,'translated':'...'}]}.",
        ],
        "items":tasks,
    }
    return sp,json.dumps(up,ensure_ascii=False)

def run_repair_batch(client:OpenAICompatClient,tasks:List[Dict[str,Any]])->List[Dict[str,Any]]:
    sp,up=build_repair_prompt(tasks);parsed=client.chat_json(system_prompt=sp,user_prompt=up)
    return parsed.get("items",[])

def extract_placeholders(text:str)->List[str]: return RE_PLACEHOLDER.findall(text)
def extract_numbers(text:str)->List[str]: return RE_NUMERIC.findall(text)

def validate_chunk(chunk:Chunk,translated_sentences:List[str],locked_terms:List[Dict[str,Any]],params:Dict[str,Any],phase:str,round_id:int)->List[Dict[str,Any]]:
    violations=[];src_sents=chunk.source_sentences
    if len(translated_sentences)<len(src_sents): translated_sentences=translated_sentences+[""]*(len(src_sents)-len(translated_sentences))
    if len(translated_sentences)>len(src_sents): translated_sentences=translated_sentences[:len(src_sents)]
    for i,src in enumerate(src_sents):
        out=translated_sentences[i]
        if bool(params["placeholder_strict"]) and Counter(extract_placeholders(src))!=Counter(extract_placeholders(out)):
            violations.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":"placeholder_mismatch","source_term":"","expected":json.dumps(extract_placeholders(src),ensure_ascii=False),"actual":json.dumps(extract_placeholders(out),ensure_ascii=False),"phase":phase,"round":round_id})
        if Counter(extract_numbers(src))!=Counter(extract_numbers(out)):
            violations.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":"numeric_mismatch","source_term":"","expected":json.dumps(extract_numbers(src),ensure_ascii=False),"actual":json.dumps(extract_numbers(out),ensure_ascii=False),"phase":phase,"round":round_id})
        for term in locked_terms:
            source_term=term["source"];target_term=term["target"];forbid_terms=term.get("forbid",[])
            if not contains_term(src,source_term): continue
            if target_term and target_term not in out:
                violations.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":"missing_locked_term","source_term":source_term,"expected":target_term,"actual":out,"phase":phase,"round":round_id})
            for bad in forbid_terms:
                if bad and bad in out:
                    violations.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":"forbidden_term","source_term":source_term,"expected":target_term,"actual":bad,"phase":phase,"round":round_id})
    return violations

def group_repair_tasks(chunk:Chunk,translated_sentences:List[str],locked_terms:List[Dict[str,Any]],violations:List[Dict[str,Any]])->List[Dict[str,Any]]:
    bad=sorted({v["sentence_id"] for v in violations if v["type"] in {"missing_locked_term","forbidden_term","placeholder_mismatch","numeric_mismatch"}})
    tasks=[]
    for sid in bad:
        src=chunk.source_sentences[sid];cur=translated_sentences[sid] if sid<len(translated_sentences) else "";terms=[t for t in locked_terms if contains_term(src,t["source"])]
        tasks.append({"chunk_id":chunk.chunk_id,"sentence_id":sid,"source_sentence":src,"current_translation":cur,"locked_terms":terms})
    return tasks

def apply_unresolved_policy(chunk:Chunk,translated_sentences:List[str],locked_terms:List[Dict[str,Any]],unresolved_policy:str)->int:
    if unresolved_policy!="keep_en_with_tag": return 0
    unresolved=0
    for i,src in enumerate(chunk.source_sentences):
        out=translated_sentences[i] if i<len(translated_sentences) else ""
        for term in locked_terms:
            src_term=term["source"];tgt_term=term["target"]
            if not contains_term(src,src_term):continue
            if tgt_term and tgt_term in out:continue
            tag=f"[[TERM_UNRESOLVED:{src_term}]]"
            if tag not in out:
                out=f"{out} {tag}".strip();unresolved+=1
        if i<len(translated_sentences): translated_sentences[i]=out
        else: translated_sentences.append(out)
    return unresolved

def merge_chunk_text(sentences:List[str])->str: return "\n".join(s.strip() for s in sentences if s.strip()).strip()

def bool_flag(value:str)->bool:
    v=value.strip().lower()
    if v in {"1","true","yes","y","on"}: return True
    if v in {"0","false","no","n","off"}: return False
    raise argparse.ArgumentTypeError(f"Invalid boolean: {value}")

def resolve_params(args:argparse.Namespace)->Dict[str,Any]:
    if args.profile not in DEFAULT_PARAMS: raise ValueError(f"Unsupported profile: {args.profile}")
    params=dict(DEFAULT_PARAMS[args.profile])
    mapping={"chunk_chars":args.chunk_chars,"chunk_chars_min":args.chunk_chars_min,"chunk_chars_max":args.chunk_chars_max,
             "batch_chunks":args.batch_chunks,"kb_topk":args.kb_topk,"lock_score_threshold":args.lock_score_threshold,
             "lock_margin_threshold":args.lock_margin_threshold,"max_repair_rounds":args.max_repair_rounds,
             "promotion_min_frequency":args.promotion_min_frequency,"promotion_pass_runs":args.promotion_pass_runs,
             "unresolved_policy":args.unresolved_policy,
             "bootstrap_score_threshold":args.bootstrap_score_threshold,
             "bootstrap_margin_threshold":args.bootstrap_margin_threshold,
             "bootstrap_min_frequency":args.bootstrap_min_frequency,
             "bootstrap_max_rules":args.bootstrap_max_rules,
             "drift_forbid_min_count":args.drift_forbid_min_count}
    for k,v in mapping.items():
        if v is not None: params[k]=v
    if args.placeholder_strict is not None: params["placeholder_strict"]=args.placeholder_strict
    return params

def ensure_env_or_raise(args:argparse.Namespace)->OpenAICompatClient:
    api_key=args.api_key or os.getenv("TRANSLATE_API_KEY") or os.getenv("OPENAI_API_KEY","")
    base_url=args.api_base_url or os.getenv("TRANSLATE_API_BASE_URL") or "https://api.openai.com/v1"
    model=args.model or os.getenv("TRANSLATE_MODEL") or "gpt-4.1-mini"
    if not api_key: raise RuntimeError("Missing API key. Set --api-key or TRANSLATE_API_KEY / OPENAI_API_KEY.")
    return OpenAICompatClient(model=model,api_key=api_key,base_url=base_url,timeout=args.request_timeout)

def compute_metrics(chunks:List[Chunk],translated_by_chunk:Dict[str,List[str]],terms_by_chunk:Dict[str,List[Dict[str,Any]]])->Dict[str,int]:
    term_total=0;term_hit=0;placeholder_errors=0
    for chunk in chunks:
        translated=translated_by_chunk.get(chunk.chunk_id,[]);locked_terms=terms_by_chunk.get(chunk.chunk_id,[])
        if len(translated)<len(chunk.source_sentences): translated=translated+[""]*(len(chunk.source_sentences)-len(translated))
        for i,src in enumerate(chunk.source_sentences):
            out=translated[i]
            if Counter(extract_placeholders(src))!=Counter(extract_placeholders(out)): placeholder_errors+=1
            for term in locked_terms:
                if not contains_term(src,term["source"]): continue
                term_total+=1
                if term["target"] in out and all((not bad) or (bad not in out) for bad in term.get("forbid",[])): term_hit+=1
    unresolved=0
    for lines in translated_by_chunk.values():
        for line in lines: unresolved+=len(RE_UNRESOLVED.findall(line))
    return {"term_total":term_total,"term_hit":term_hit,"term_miss":max(0,term_total-term_hit),"term_unresolved":unresolved,"placeholder_errors":placeholder_errors}

def apply_auto_promotion(overrides_path:Path,rules:List[Dict[str,Any]],term_stats:Dict[str,Dict[str,Any]],params:Dict[str,Any])->List[str]:
    state_path=overrides_path.with_suffix(".state.json")
    state=json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {}
    existing={str(r.get("source","")).lower() for r in rules};promoted=[]
    for source_lc,st in term_stats.items():
        freq=int(st.get("count",0));hit=int(st.get("hit",0));target=st.get("target","")
        item=state.get(source_lc,{"pass_runs":0,"last_target":target})
        if freq>=int(params["promotion_min_frequency"]) and hit==freq and target:
            item["pass_runs"]=int(item.get("pass_runs",0))+1;item["last_target"]=target
        else:
            item["pass_runs"]=0;item["last_target"]=target
        state[source_lc]=item
        if source_lc not in existing and item["pass_runs"]>=int(params["promotion_pass_runs"]) and target:
            rules.append({"id":f"auto_{re.sub(r'[^a-z0-9]+','_',source_lc).strip('_')}","source":st.get("source",source_lc),"target":target,"match":"exact_ci","priority":90,"scope":"global","forbid":[],"enabled":True})
            existing.add(source_lc);promoted.append(st.get("source",source_lc))
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    if promoted: save_overrides(overrides_path,rules)
    return promoted

def parse_args()->argparse.Namespace:
    script_dir=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description="Term-constrained translation pipeline")
    p.add_argument("--input",required=True,help="Input path: .txt/.md/.html")
    p.add_argument("--output",required=True,help="Output translated text path")
    p.add_argument("--kb-dir",default=str(script_dir/"kb"))
    p.add_argument("--report",default="",help="Report path (default: translation_report.json beside output)")
    p.add_argument("--profile",default="balanced")
    p.add_argument("--overrides",default=str(script_dir/"term_overrides.json"))
    p.add_argument("--chunk-chars",type=int);p.add_argument("--chunk-chars-min",type=int);p.add_argument("--chunk-chars-max",type=int)
    p.add_argument("--batch-chunks",type=int);p.add_argument("--kb-topk",type=int);p.add_argument("--lock-score-threshold",type=float)
    p.add_argument("--lock-margin-threshold",type=float);p.add_argument("--max-repair-rounds",type=int)
    p.add_argument("--promotion-min-frequency",type=int);p.add_argument("--promotion-pass-runs",type=int)
    p.add_argument("--unresolved-policy",default=None);p.add_argument("--placeholder-strict",type=bool_flag,default=None)
    p.add_argument("--bootstrap-force",action="store_true")
    p.add_argument("--bootstrap-score-threshold",type=float)
    p.add_argument("--bootstrap-margin-threshold",type=float)
    p.add_argument("--bootstrap-min-frequency",type=int)
    p.add_argument("--bootstrap-max-rules",type=int)
    p.add_argument("--drift-forbid-min-count",type=int)
    p.add_argument("--api-base-url",default="");p.add_argument("--api-key",default="");p.add_argument("--model",default="")
    p.add_argument("--request-timeout",type=int,default=120)
    p.add_argument("--llm-backend",choices=["codex","api"],default="codex")
    p.add_argument("--codex-dir",default="",help="Job/result directory for codex backend (default: output dir)")
    p.add_argument("--dry-run",action="store_true",help="Skip model calls and echo source sentences")
    return p.parse_args()

def main()->int:
    args=parse_args();params=resolve_params(args);start_ts=time.time()
    input_path=Path(args.input).resolve();output_path=Path(args.output).resolve()
    report_path=Path(args.report).resolve() if args.report else output_path.with_name("translation_report.json").resolve()
    codex_base_dir=Path(args.codex_dir).resolve() if args.codex_dir else output_path.parent.resolve()
    codex_paths=build_codex_paths(codex_base_dir)
    kb_dir=Path(args.kb_dir).resolve();overrides_path=Path(args.overrides).resolve();sqlite_path=kb_dir/"kb.sqlite"
    if not sqlite_path.exists():
        raise FileNotFoundError(f"KB not found: {sqlite_path}. Build first with build_index.py / scripts/build_kb.ps1")
    source_text=load_text(input_path);chunks=build_chunks(source_text,params=params)
    if not chunks: raise RuntimeError("No translatable content found in input.")
    rules=normalize_rules(load_overrides(overrides_path))

    conn=sqlite3.connect(str(sqlite_path));cache={};terms_by_chunk={};soft_terms_by_chunk={};bootstrap_added=[]
    try:
        if args.bootstrap_force or not rules:
            bootstrap_added=bootstrap_rules_from_kb(conn,kb_dir,rules,params,cache)
            if bootstrap_added:
                rules=normalize_rules(rules)
                save_overrides(overrides_path,rules)
        for chunk in chunks:
            locked,soft=build_terms_for_chunk(chunk=chunk,rules=rules,conn=conn,kb_dir=kb_dir,params=params,cache=cache)
            terms_by_chunk[chunk.chunk_id]=locked;soft_terms_by_chunk[chunk.chunk_id]=soft
    finally:
        conn.close()

    translated_by_chunk={}
    if args.dry_run:
        for chunk in chunks: translated_by_chunk[chunk.chunk_id]=list(chunk.source_sentences)
    else:
        if args.llm_backend=="api":
            client=ensure_env_or_raise(args);batch_size=max(1,int(params["batch_chunks"]))
            for i in range(0,len(chunks),batch_size):
                batch=chunks[i:i+batch_size]
                req_items=[{"chunk_id":c.chunk_id,"source_sentences":c.source_sentences,"locked_terms":terms_by_chunk.get(c.chunk_id,[]),"soft_terms":soft_terms_by_chunk.get(c.chunk_id,[])} for c in batch]
                ret=run_translation_batch(client=client,batch_items=req_items)
                for c in batch:
                    got=ret.get(c.chunk_id)
                    translated_by_chunk[c.chunk_id]=got if got else list(c.source_sentences)
        else:
            req_items=[{"chunk_id":c.chunk_id,"source_sentences":c.source_sentences,"locked_terms":terms_by_chunk.get(c.chunk_id,[]),"soft_terms":soft_terms_by_chunk.get(c.chunk_id,[])} for c in chunks]
            if not codex_paths["translation_result"].exists():
                write_json(codex_paths["translation_job"],{
                    "task":"Translate each source sentence to zh-CN using locked_terms as hard constraints.",
                    "format":{"items":[{"chunk_id":"c_0001","translated_sentences":["..."]}]},
                    "items":req_items,
                })
                raise RuntimeError(
                    f"Codex translation job written: {codex_paths['translation_job']}. "
                    f"Please create result file: {codex_paths['translation_result']} and rerun."
                )
            translated_by_chunk=load_codex_translation_result(codex_paths["translation_result"],chunks)

    violation_history=[];repair_rounds_used=0;term_replacements=0
    if args.dry_run or args.llm_backend!="api":
        client=None
    else:
        client=ensure_env_or_raise(args)
    for chunk in chunks:
        cur=translated_by_chunk.get(chunk.chunk_id,list(chunk.source_sentences));locked_terms=terms_by_chunk.get(chunk.chunk_id,[])
        cur,rep=enforce_locked_terms(chunk,cur,locked_terms);term_replacements+=rep
        v0=validate_chunk(chunk,cur,locked_terms,params,"initial",0);violation_history.extend(v0)
        for r in range(1,int(params["max_repair_rounds"])+1):
            reparable=validate_chunk(chunk,cur,locked_terms,params,"repair_check",r)
            if not reparable: break
            violation_history.extend(reparable)
            if args.dry_run: break
            tasks=group_repair_tasks(chunk,cur,locked_terms,reparable)
            if not tasks: break
            if args.llm_backend=="api":
                fixes=run_repair_batch(client,tasks)
            else:
                repair_paths=build_codex_repair_paths(codex_base_dir,r)
                if not repair_paths["repair_result"].exists():
                    write_json(repair_paths["repair_job"],{
                        "task":"Repair only these translated sentences. Keep placeholders/numbers unchanged and satisfy locked_terms.",
                        "format":{"items":[{"chunk_id":"c_0001","sentence_id":0,"translated":"..."}]},
                        "items":tasks,
                    })
                    violation_history.append({
                        "chunk_id":chunk.chunk_id,
                        "sentence_id":-1,
                        "type":"repair_skipped_missing_result",
                        "source_term":"",
                        "expected":str(repair_paths["repair_result"]),
                        "actual":"",
                        "phase":"repair",
                        "round":r,
                    })
                    break
                fixes=load_codex_repair_result(repair_paths["repair_result"])
            fix_map={}
            for f in fixes:
                sid=int(f.get("sentence_id",-1));txt=str(f.get("translated","")).strip()
                if sid>=0 and txt: fix_map[sid]=txt
            if not fix_map: break
            for sid,txt in fix_map.items():
                if sid<len(cur): cur[sid]=txt
            cur,rep=enforce_locked_terms(chunk,cur,locked_terms);term_replacements+=rep
            repair_rounds_used=max(repair_rounds_used,r)

        cur,rep=enforce_locked_terms(chunk,cur,locked_terms);term_replacements+=rep
        unresolved=apply_unresolved_policy(chunk,cur,locked_terms,str(params["unresolved_policy"]))
        if unresolved:
            violation_history.append({"chunk_id":chunk.chunk_id,"sentence_id":-1,"type":"term_unresolved_applied","source_term":"","expected":"","actual":str(unresolved),"phase":"finalize","round":repair_rounds_used})

        final_v=validate_chunk(chunk,cur,locked_terms,params,"final",repair_rounds_used);violation_history.extend(final_v)
        translated_by_chunk[chunk.chunk_id]=cur

    term_stats={}
    for chunk in chunks:
        locked_terms=terms_by_chunk.get(chunk.chunk_id,[]);translated=translated_by_chunk.get(chunk.chunk_id,[])
        if len(translated)<len(chunk.source_sentences): translated=translated+[""]*(len(chunk.source_sentences)-len(translated))
        for term in locked_terms:
            if term.get("source_type")!="kb": continue
            source_lc=term["source"].lower();stats=term_stats.setdefault(source_lc,{"source":term["source"],"target":term["target"],"count":0,"hit":0})
            for i,src_sent in enumerate(chunk.source_sentences):
                if not contains_term(src_sent,term["source"]): continue
                stats["count"]+=1;out_sent=translated[i]
                if term["target"] in out_sent and all((not bad) or (bad not in out_sent) for bad in term.get("forbid",[])): stats["hit"]+=1

    promoted=apply_auto_promotion(overrides_path,rules,term_stats,params)
    drift_path=get_drift_history_path(overrides_path)
    drift_history=load_drift_history(drift_path)
    update_drift_history(drift_history,violation_history)
    drift_forbid_added=merge_drift_forbid_into_rules(rules,drift_history,int(params["drift_forbid_min_count"]))
    save_drift_history(drift_path,drift_history)
    if drift_forbid_added:
        rules=normalize_rules(rules)
        save_overrides(overrides_path,rules)
    final_text="\n\n".join([merge_chunk_text(translated_by_chunk[c.chunk_id]) for c in chunks if merge_chunk_text(translated_by_chunk[c.chunk_id]).strip()]).strip()+"\n"
    output_path.parent.mkdir(parents=True,exist_ok=True);output_path.write_text(final_text,encoding="utf-8")
    metrics=compute_metrics(chunks,translated_by_chunk,terms_by_chunk)
    runtime_terms={cid:{"locked_terms":terms_by_chunk.get(cid,[]),"soft_terms":soft_terms_by_chunk.get(cid,[])} for cid in [c.chunk_id for c in chunks]}
    report={"profile":args.profile,"params":params,"input":str(input_path),"output":str(output_path),"kb_dir":str(kb_dir),"overrides":str(overrides_path),"chunks":len(chunks),"latency_ms":int((time.time()-start_ts)*1000),"repair_rounds":repair_rounds_used,"bootstrap_added_terms":bootstrap_added,"promoted_terms":promoted,"drift_forbid_added":[{"source":s,"alias":a} for s,a in drift_forbid_added],"term_replacements":term_replacements,**metrics,"runtime_terms":runtime_terms,"violations":violation_history}
    report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Output: {output_path}");print(f"Report: {report_path}")
    print(f"Metrics: term_hit={report['term_hit']}/{report['term_total']} term_unresolved={report['term_unresolved']} placeholder_errors={report['placeholder_errors']}")
    return 0

if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as e:
        print(f"Pipeline failed: {e}",file=sys.stderr);raise
