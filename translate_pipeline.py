#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
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
    "enable_p1_repairs":True,
    "enable_entity_guard":True,
    "enable_term_normalization":True,
    "p1_score_threshold":0.1265,
    "p2_score_threshold":0.1240,
    "p1_coverage_threshold":0.67,
    "p2_coverage_threshold":0.34,
    "p1_max_terms":8,
    "p2_max_terms":12,
    "kb_disable_semantic":False,
    "bootstrap_score_threshold":0.12,"bootstrap_margin_threshold":0.0003,
    "bootstrap_min_frequency":1,"bootstrap_max_rules":300,
    "drift_forbid_min_count":2,
},
"fast":{
    "chunk_chars":1200,"chunk_chars_min":900,"chunk_chars_max":1500,
    "batch_chunks":8,"kb_topk":3,"lock_score_threshold":0.35,
    "lock_margin_threshold":0.03,"max_repair_rounds":2,
    "promotion_min_frequency":5,"promotion_pass_runs":3,
    "unresolved_policy":"keep_en_with_tag","placeholder_strict":True,
    "enable_p1_repairs":True,
    "enable_entity_guard":True,
    "enable_term_normalization":True,
    "p1_score_threshold":0.1265,
    "p2_score_threshold":0.1240,
    "p1_coverage_threshold":0.67,
    "p2_coverage_threshold":0.34,
    "p1_max_terms":8,
    "p2_max_terms":12,
    "kb_disable_semantic":True,
    "bootstrap_score_threshold":0.12,"bootstrap_margin_threshold":0.0003,
    "bootstrap_min_frequency":1,"bootstrap_max_rules":300,
    "drift_forbid_min_count":2,
}}
PROTOCOL_SCHEMA_VERSION = 2
STRICT_MAX_EN_ONLY_LINE_RATIO = 0.10

RE_TERM_CANDIDATE=re.compile(r"\b(?:[A-Z][A-Za-z0-9'+/\-]{2,}|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-z0-9'+/\-]{2,}|[A-Z]{2,})){0,3}\b")
RE_EN_TOKEN=re.compile(r"[A-Za-z][A-Za-z0-9']+")
RE_PLACEHOLDER=re.compile(r"\{[^{}\n]+\}|%\d*\$?[sdif]|%[sdif]")
RE_NUMERIC=re.compile(r"\d+(?:\.\d+)?%?")
RE_UNRESOLVED=re.compile(r"\[\[TERM_UNRESOLVED:([^\]]+)\]\]")
RE_CJK_TOKEN=re.compile(r"[\u4e00-\u9fff]{2,12}")
RE_CJK_CHAR=re.compile(r"[\u4e00-\u9fff]")
RE_EN_WORD=re.compile(r"[A-Za-z]")

HIGH_VALUE_KEYWORDS={
    "bat","beaver","fox","frog","harpy","human","lizard","seal","species","race",
    "hearth","house","mine","smelter","furnace","academy","court","grill","foundry","smithy",
    "workshop","camp","warehouse","tavern","forum","temple","monastery","kiln","cooperage",
    "firekeeper","worker","scout","builder","carrier","miner","smith","job","profession",
    "cornerstone","perk","resolve","hostility","reputation","impatience","blight","rainpunk",
    "glade","danger","forbidden","effect","status","mechanic","dedication",
    "town","panel","cycle","cache","equipment","city","biome","modifier","charm","timed","storage","recipe","farming","tool","surveying",
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
        hits=kb_search_cached(conn,kb_dir,eval_query,2,cache,bool(params.get("kb_disable_semantic",False)))
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

def english_token_set(text:str)->set[str]:
    out=set()
    for tok in RE_EN_TOKEN.findall(str(text or "").lower()):
        t=re.sub(r"[^a-z0-9]","",tok)
        if len(t)>=3:
            out.add(t)
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

def kb_search_cached(conn:sqlite3.Connection,kb_dir:Path,query:str,topk:int,cache:Dict[str,List[Tuple[float,Dict[str,Any]]]],disable_semantic:bool=False)->List[Tuple[float,Dict[str,Any]]]:
    key=f"{query.strip().lower()}|k={int(topk)}|sem={int(not disable_semantic)}"
    if key in cache:return cache[key]
    hits=hybrid_search(conn=conn,kb_dir=kb_dir,query=query,topk=max(1,topk),fts_topk=max(20,topk),vec_topk=max(20,topk),w_fts=0.55,w_vec=0.45,model_name_override=None,disable_semantic=bool(disable_semantic))
    if disable_semantic and not hits:
        # In fast mode, fallback once to semantic retrieval for terminology recall.
        hits=hybrid_search(conn=conn,kb_dir=kb_dir,query=query,topk=max(1,topk),fts_topk=max(20,topk),vec_topk=max(20,topk),w_fts=0.55,w_vec=0.45,model_name_override=None,disable_semantic=False)
    cache[key]=hits;return hits

def build_terms_for_chunk(chunk:Chunk,rules:List[Dict[str,Any]],conn:sqlite3.Connection,kb_dir:Path,params:Dict[str,Any],cache:Dict[str,List[Tuple[float,Dict[str,Any]]]])->Tuple[List[Dict[str,Any]],List[Dict[str,Any]]]:
    locked={};soft=[]
    for t in match_overrides(chunk.text,rules): locked[t["source"].lower()]=t
    for cand in extract_candidates(chunk.text):
        ck=cand.lower()
        if ck in locked:continue
        hits=kb_search_cached(conn,kb_dir,cand,int(params["kb_topk"]),cache,bool(params.get("kb_disable_semantic",False)))
        if not hits:continue
        top_score,top_rec=hits[0];second_score=hits[1][0] if len(hits)>1 else 0.0;margin=top_score-second_score
        obj={
            "source":cand,
            "target":top_rec.get("zh",""),
            "key":top_rec.get("key",""),
            "entity":top_rec.get("entity",""),
            "de_norm":top_rec.get("de_norm",""),
            "domain":top_rec.get("domain",""),
            "slot":top_rec.get("slot",""),
            "score":round(float(top_score),6),
            "margin":round(float(margin),6),
            "source_type":"kb",
            "forbid":[],
        }
        if top_rec.get("zh") and float(top_score)>=float(params["lock_score_threshold"]) and float(margin)>=float(params["lock_margin_threshold"]): locked[ck]=obj
        else: soft.append(obj)
    return sorted(locked.values(),key=lambda x:len(x["source"]),reverse=True),soft

def derive_priority_terms(
    soft_terms:List[Dict[str,Any]],
    locked_terms:List[Dict[str,Any]],
    params:Dict[str,Any],
)->Tuple[List[Dict[str,Any]],List[Dict[str,Any]]]:
    weak_single_words={
        "the","this","that","these","those","we","were","weve","we'll","were","you","your","our","their","its","it's","it","now","may","and","for","with","from","into","after","before","then",
        "also","here","there","panel","event","effect","reward","order","orders","camp","city","water","queen","hand","trial","tools",
    }
    p1=[];p2=[];seen_locked={str(t.get("source","")).strip().lower() for t in locked_terms}
    p1_th=float(params.get("p1_score_threshold",0.1265));p2_th=float(params.get("p2_score_threshold",0.1240))
    p1_cov_th=float(params.get("p1_coverage_threshold",0.67));p2_cov_th=float(params.get("p2_coverage_threshold",0.34))
    p1_cap=int(params.get("p1_max_terms",8));p2_cap=int(params.get("p2_max_terms",12))
    for t in soft_terms:
        src=str(t.get("source","")).strip();tgt=str(t.get("target","")).strip()
        if not src or not tgt: continue
        if src.lower() in seen_locked: continue
        if not RE_CJK_CHAR.search(tgt): continue
        words=[w for w in re.split(r"\s+",src) if w]
        src_lc=src.lower()
        alpha=re.sub(r"[^a-z]","",src_lc)
        if len(words)==1:
            if alpha in weak_single_words: continue
            if "'" in src_lc or "’" in src_lc: continue
            if len(alpha)<6: continue
        src_tokens=english_token_set(src)
        if not src_tokens: continue
        rec_blob=" ".join([str(t.get("key","")),str(t.get("entity","")),str(t.get("de_norm","")),str(t.get("domain","")),str(t.get("slot",""))])
        rec_tokens=english_token_set(rec_blob)
        overlap=len(src_tokens & rec_tokens)
        coverage=float(overlap/max(1,len(src_tokens)))
        high_value=any(tok in HIGH_VALUE_KEYWORDS for tok in src_tokens)
        if not high_value and len(src_tokens)<2:
            continue
        score=float(t.get("score",0.0))
        item={
            "source":src,
            "target":tgt,
            "key":t.get("key",""),
            "score":round(score,6),
            "coverage":round(coverage,6),
            "source_type":str(t.get("source_type","kb") or "kb"),
            "forbid":list(t.get("forbid",[])),
        }
        if score>=p1_th and coverage>=p1_cov_th:
            p1.append(item)
        elif score>=p2_th and coverage>=p2_cov_th:
            p2.append(item)
    p1=sorted(p1,key=lambda x:(-float(x.get("score",0.0)),-len(str(x.get("source",""))),str(x.get("source",""))))
    p2=sorted(p2,key=lambda x:(-float(x.get("score",0.0)),-len(str(x.get("source",""))),str(x.get("source",""))))
    if p1_cap>0: p1=p1[:p1_cap]
    if p2_cap>0: p2=p2[:p2_cap]
    return p1,p2

def write_json(path:Path,obj:Dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")

def load_json(path:Path)->Dict[str,Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))

def compute_file_sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def build_job_id()->str:
    return f"job_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"

def cleanup_protocol_workspace(work_dir:Path)->None:
    files=[
        work_dir/"translation.result.json",
        work_dir/"validation.report.json",
    ]
    for path in files:
        if path.exists():
            path.unlink()
    for pat in ("repair.job.r*.json","repair.result.r*.json"):
        for path in work_dir.glob(pat):
            if path.is_file():
                path.unlink()

def build_protocol_paths(base_dir:Path)->Dict[str,Path]:
    return {
        "translation_job":base_dir/"translation.job.json",
        "translation_result":base_dir/"translation.result.json",
        "validation_report":base_dir/"validation.report.json",
    }

def build_protocol_repair_paths(base_dir:Path,round_id:int)->Dict[str,Path]:
    return {
        "repair_job":base_dir/f"repair.job.r{round_id}.json",
        "repair_result":base_dir/f"repair.result.r{round_id}.json",
    }

def load_repair_result_from_file(path:Path)->List[Dict[str,Any]]:
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

def merge_terms_unique(*term_lists:List[Dict[str,Any]])->List[Dict[str,Any]]:
    out=[];seen=set()
    for terms in term_lists:
        for t in terms:
            src=str(t.get("source","")).strip().lower()
            if not src or src in seen: continue
            seen.add(src);out.append(t)
    return out

def validate_preferred_terms(
    chunk:Chunk,
    translated_sentences:List[str],
    preferred_terms:List[Dict[str,Any]],
    params:Dict[str,Any],
    phase:str,
    round_id:int,
)->Tuple[List[Dict[str,Any]],List[Dict[str,Any]]]:
    violations=[];advisories=[];src_sents=chunk.source_sentences
    if len(translated_sentences)<len(src_sents): translated_sentences=translated_sentences+[""]*(len(src_sents)-len(translated_sentences))
    if len(translated_sentences)>len(src_sents): translated_sentences=translated_sentences[:len(src_sents)]
    enable_entity_guard=bool(params.get("enable_entity_guard",True))
    enable_p1_repairs=bool(params.get("enable_p1_repairs",True))
    for i,src in enumerate(src_sents):
        out=translated_sentences[i]
        for term in preferred_terms:
            source_term=str(term.get("source","")).strip()
            target_term=str(term.get("target","")).strip()
            level=str(term.get("level","p1")).strip().lower() or "p1"
            if not source_term or not target_term or not contains_term(src,source_term): continue
            if target_term in out: continue
            if level=="p2":
                advisories.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":"missing_advisory_term","source_term":source_term,"expected":target_term,"actual":out,"phase":phase,"round":round_id})
                continue
            if not enable_p1_repairs:
                advisories.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":"missing_preferred_term","source_term":source_term,"expected":target_term,"actual":out,"phase":phase,"round":round_id})
                continue
            vtype="missing_preferred_term"
            if enable_entity_guard and contains_term(out,source_term):
                vtype="untranslated_entity"
            violations.append({"chunk_id":chunk.chunk_id,"sentence_id":i,"type":vtype,"source_term":source_term,"expected":target_term,"actual":out,"phase":phase,"round":round_id})
    return violations,advisories

def apply_term_normalization(
    chunk:Chunk,
    translated_sentences:List[str],
    normalize_terms:List[Dict[str,Any]],
)->Tuple[List[str],int]:
    src_sents=chunk.source_sentences
    out=list(translated_sentences)
    if len(out)<len(src_sents): out.extend([""]*(len(src_sents)-len(out)))
    if len(out)>len(src_sents): out=out[:len(src_sents)]
    changed=0
    for i,src in enumerate(src_sents):
        cur=out[i]
        for term in normalize_terms:
            source_term=str(term.get("source","")).strip()
            target_term=str(term.get("target","")).strip()
            if not source_term or not target_term or not contains_term(src,source_term): continue
            if target_term in cur: continue
            cur,n=replace_term_occurrences(cur,source_term,target_term);changed+=n
            for bad in term.get("forbid",[]):
                bad=str(bad).strip()
                if not bad: continue
                cur,n=replace_term_occurrences(cur,bad,target_term);changed+=n
        out[i]=cur
    return out,changed

def group_repair_tasks(chunk:Chunk,translated_sentences:List[str],locked_terms:List[Dict[str,Any]],preferred_terms:List[Dict[str,Any]],violations:List[Dict[str,Any]])->List[Dict[str,Any]]:
    bad=sorted({v["sentence_id"] for v in violations if v["type"] in {"missing_locked_term","forbidden_term","placeholder_mismatch","numeric_mismatch","missing_preferred_term","untranslated_entity"}})
    tasks=[]
    for sid in bad:
        src=chunk.source_sentences[sid];cur=translated_sentences[sid] if sid<len(translated_sentences) else "";terms=[t for t in locked_terms if contains_term(src,t["source"])];preferred=[t for t in preferred_terms if contains_term(src,str(t.get("source","")))]
        tasks.append({"chunk_id":chunk.chunk_id,"sentence_id":sid,"source_sentence":src,"current_translation":cur,"locked_terms":terms,"preferred_terms":preferred})
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
             "enable_p1_repairs":args.enable_p1_repairs,
             "enable_entity_guard":args.enable_entity_guard,
             "enable_term_normalization":args.enable_term_normalization,
             "p1_score_threshold":args.p1_score_threshold,
             "p2_score_threshold":args.p2_score_threshold,
             "p1_coverage_threshold":args.p1_coverage_threshold,
             "p2_coverage_threshold":args.p2_coverage_threshold,
             "p1_max_terms":args.p1_max_terms,
             "p2_max_terms":args.p2_max_terms,
             "kb_disable_semantic":args.kb_disable_semantic,
             "bootstrap_score_threshold":args.bootstrap_score_threshold,
             "bootstrap_margin_threshold":args.bootstrap_margin_threshold,
             "bootstrap_min_frequency":args.bootstrap_min_frequency,
             "bootstrap_max_rules":args.bootstrap_max_rules,
             "drift_forbid_min_count":args.drift_forbid_min_count}
    for k,v in mapping.items():
        if v is not None: params[k]=v
    if args.placeholder_strict is not None: params["placeholder_strict"]=args.placeholder_strict
    return params

def compute_metrics(
    chunks:List[Chunk],
    translated_by_chunk:Dict[str,List[str]],
    terms_by_chunk:Dict[str,List[Dict[str,Any]]],
    preferred_terms_by_chunk:Dict[str,List[Dict[str,Any]]]|None=None,
)->Dict[str,int]:
    term_total=0;term_hit=0;placeholder_errors=0
    preferred_total=0;preferred_hit=0
    preferred_terms_by_chunk=preferred_terms_by_chunk or {}
    for chunk in chunks:
        translated=translated_by_chunk.get(chunk.chunk_id,[]);locked_terms=terms_by_chunk.get(chunk.chunk_id,[])
        preferred_terms=preferred_terms_by_chunk.get(chunk.chunk_id,[])
        if len(translated)<len(chunk.source_sentences): translated=translated+[""]*(len(chunk.source_sentences)-len(translated))
        for i,src in enumerate(chunk.source_sentences):
            out=translated[i]
            if Counter(extract_placeholders(src))!=Counter(extract_placeholders(out)): placeholder_errors+=1
            for term in locked_terms:
                if not contains_term(src,term["source"]): continue
                term_total+=1
                if term["target"] in out and all((not bad) or (bad not in out) for bad in term.get("forbid",[])): term_hit+=1
            for term in preferred_terms:
                source=str(term.get("source","")).strip();target=str(term.get("target","")).strip()
                if not source or not target or not contains_term(src,source): continue
                preferred_total+=1
                if target in out: preferred_hit+=1
    unresolved=0
    for lines in translated_by_chunk.values():
        for line in lines: unresolved+=len(RE_UNRESOLVED.findall(line))
    return {
        "term_total":term_total,
        "term_hit":term_hit,
        "term_miss":max(0,term_total-term_hit),
        "preferred_term_total":preferred_total,
        "preferred_term_hit":preferred_hit,
        "preferred_term_miss":max(0,preferred_total-preferred_hit),
        "term_unresolved":unresolved,
        "placeholder_errors":placeholder_errors,
    }

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

def utc_now()->str:
    return datetime.now(timezone.utc).isoformat()

def prepare_job(
    input_path:Path,
    output_path:Path,
    report_path:Path,
    kb_dir:Path,
    overrides_path:Path,
    work_dir:Path,
    params:Dict[str,Any],
    bootstrap_force:bool,
    profile:str,
)->Dict[str,Any]:
    sqlite_path=kb_dir/"kb.sqlite"
    if not sqlite_path.exists():
        raise FileNotFoundError(f"KB not found: {sqlite_path}. Build first with build_index.py / scripts/build_kb.ps1")
    source_text=load_text(input_path)
    chunks=build_chunks(source_text,params=params)
    if not chunks:
        raise RuntimeError("No translatable content found in input.")

    rules=normalize_rules(load_overrides(overrides_path))
    conn=sqlite3.connect(str(sqlite_path));cache={};terms_by_chunk={};soft_terms_by_chunk={};p1_terms_by_chunk={};p2_terms_by_chunk={};bootstrap_added=[]
    try:
        if bootstrap_force or not rules:
            bootstrap_added=bootstrap_rules_from_kb(conn,kb_dir,rules,params,cache)
            if bootstrap_added:
                rules=normalize_rules(rules);save_overrides(overrides_path,rules)
        for chunk in chunks:
            locked,soft=build_terms_for_chunk(chunk=chunk,rules=rules,conn=conn,kb_dir=kb_dir,params=params,cache=cache)
            p1,p2=derive_priority_terms(soft_terms=soft,locked_terms=locked,params=params)
            terms_by_chunk[chunk.chunk_id]=locked;soft_terms_by_chunk[chunk.chunk_id]=soft
            p1_terms_by_chunk[chunk.chunk_id]=p1;p2_terms_by_chunk[chunk.chunk_id]=p2
    finally:
        conn.close()

    items=[]
    for c in chunks:
        items.append({
            "chunk_id":c.chunk_id,
            "source_text":c.text,
            "source_sentences":c.source_sentences,
            "locked_terms":terms_by_chunk.get(c.chunk_id,[]),
            "soft_terms":soft_terms_by_chunk.get(c.chunk_id,[]),
            "p1_terms":p1_terms_by_chunk.get(c.chunk_id,[]),
            "p2_terms":p2_terms_by_chunk.get(c.chunk_id,[]),
        })
    payload={
        "version":1,
        "schema_version":PROTOCOL_SCHEMA_VERSION,
        "created_at":utc_now(),
        "job_id":build_job_id(),
        "input_sha256":compute_file_sha256(input_path),
        "profile":profile,
        "params":params,
        "input":str(input_path),
        "output":str(output_path),
        "report":str(report_path),
        "kb_dir":str(kb_dir),
        "overrides":str(overrides_path),
        "work_dir":str(work_dir),
        "bootstrap_added_terms":bootstrap_added,
        "chunks":len(chunks),
        "items":items,
    }
    return payload

def chunks_from_job(job:Dict[str,Any])->Tuple[List[Chunk],Dict[str,List[Dict[str,Any]]],Dict[str,List[Dict[str,Any]]],Dict[str,List[Dict[str,Any]]],Dict[str,List[Dict[str,Any]]],Dict[str,Any]]:
    chunks=[];terms_by_chunk={};soft_terms_by_chunk={};p1_terms_by_chunk={};p2_terms_by_chunk={}
    for it in job.get("items",[]):
        cid=str(it.get("chunk_id","")).strip()
        if not cid:continue
        sents=[str(x).strip() for x in it.get("source_sentences",[])]
        text=str(it.get("source_text","")).strip()
        if not sents:sents=split_sentences(text)
        chunks.append(Chunk(chunk_id=cid,text=text,source_sentences=sents))
        terms_by_chunk[cid]=[x for x in it.get("locked_terms",[]) if isinstance(x,dict)]
        soft_terms_by_chunk[cid]=[x for x in it.get("soft_terms",[]) if isinstance(x,dict)]
        p1_terms_by_chunk[cid]=[x for x in it.get("p1_terms",[]) if isinstance(x,dict)]
        p2_terms_by_chunk[cid]=[x for x in it.get("p2_terms",[]) if isinstance(x,dict)]
    params=dict(job.get("params",{})) or dict(DEFAULT_PARAMS["balanced"])
    return chunks,terms_by_chunk,soft_terms_by_chunk,p1_terms_by_chunk,p2_terms_by_chunk,params

def load_result_payload(path:Path)->Dict[str,Any]:
    payload=load_json(path)
    if not isinstance(payload,dict):
        raise ValueError(f"Invalid result payload: {path}")
    return payload

def load_result_map_from_payload(payload:Dict[str,Any])->Dict[str,List[str]]:
    items=payload.get("items",[]) if isinstance(payload,dict) else []
    out={}
    for it in items:
        cid=str(it.get("chunk_id","")).strip()
        if not cid:continue
        out[cid]=[str(x).strip() for x in it.get("translated_sentences",[])]
    return out

def load_result_map(path:Path)->Dict[str,List[str]]:
    return load_result_map_from_payload(load_result_payload(path))

def build_identity_mismatch_violation(job:Dict[str,Any],result_payload:Dict[str,Any],round_id:int)->Dict[str,Any]:
    expected=f"job_id={job.get('job_id','')};input_sha256={job.get('input_sha256','')};schema_version={job.get('schema_version',PROTOCOL_SCHEMA_VERSION)}"
    actual=f"job_id={result_payload.get('job_id','')};input_sha256={result_payload.get('input_sha256','')};schema_version={result_payload.get('schema_version','')}"
    return {
        "chunk_id":"*",
        "sentence_id":-1,
        "type":"stale_result",
        "source_term":"",
        "expected":expected,
        "actual":actual,
        "phase":"validate",
        "round":round_id,
    }

def is_result_identity_match(job:Dict[str,Any],result_payload:Dict[str,Any])->bool:
    job_id=str(job.get("job_id","")).strip()
    input_sha256=str(job.get("input_sha256","")).strip()
    schema_version=int(job.get("schema_version",PROTOCOL_SCHEMA_VERSION))
    return (
        job_id
        and input_sha256
        and str(result_payload.get("job_id","")).strip()==job_id
        and str(result_payload.get("input_sha256","")).strip()==input_sha256
        and int(result_payload.get("schema_version",0))==schema_version
    )

def write_result_map(path:Path,chunks:List[Chunk],result_map:Dict[str,List[str]],job:Dict[str,Any],base_payload:Dict[str,Any]|None=None)->None:
    items=[{"chunk_id":c.chunk_id,"translated_sentences":list(result_map.get(c.chunk_id,[]))} for c in chunks]
    payload={
        "version":1,
        "schema_version":int(job.get("schema_version",PROTOCOL_SCHEMA_VERSION)),
        "updated_at":utc_now(),
        "job_id":str(job.get("job_id","")),
        "input_sha256":str(job.get("input_sha256","")),
        "items":items,
    }
    if base_payload:
        # Preserve custom metadata written by the model side if present.
        for k,v in base_payload.items():
            if k in {"version","schema_version","updated_at","job_id","input_sha256","items"}:
                continue
            payload[k]=v
    write_json(path,payload)

def compute_language_metrics(chunks:List[Chunk],translated_by_chunk:Dict[str,List[str]])->Dict[str,Any]:
    line_total=0
    line_non_empty=0
    en_only_lines=0
    unresolved_tags=0
    for chunk in chunks:
        lines=list(translated_by_chunk.get(chunk.chunk_id,[]))
        if len(lines)<len(chunk.source_sentences):
            lines.extend([""]*(len(chunk.source_sentences)-len(lines)))
        for line in lines:
            line_total+=1
            text=str(line or "").strip()
            if not text:
                continue
            line_non_empty+=1
            unresolved_tags+=len(RE_UNRESOLVED.findall(text))
            if RE_EN_WORD.search(text) and not RE_CJK_CHAR.search(text):
                en_only_lines+=1
    ratio=float(en_only_lines/line_non_empty) if line_non_empty else 0.0
    return {
        "line_total":line_total,
        "line_non_empty":line_non_empty,
        "en_only_lines":en_only_lines,
        "en_only_line_ratio":round(ratio,6),
        "unresolved_tags":unresolved_tags,
    }

def add_alignment_violations(chunk:Chunk,lines:List[str]|None,violations:List[Dict[str,Any]],phase:str,round_id:int)->bool:
    if lines is None:
        violations.append({"chunk_id":chunk.chunk_id,"sentence_id":-1,"type":"missing_chunk_result","source_term":"","expected":str(len(chunk.source_sentences)),"actual":"missing","phase":phase,"round":round_id})
        return True
    if len(lines)!=len(chunk.source_sentences):
        violations.append({"chunk_id":chunk.chunk_id,"sentence_id":-1,"type":"sentence_count_mismatch","source_term":"","expected":str(len(chunk.source_sentences)),"actual":str(len(lines)),"phase":phase,"round":round_id})
    return False

def collect_term_stats(chunks:List[Chunk],translated_by_chunk:Dict[str,List[str]],terms_by_chunk:Dict[str,List[Dict[str,Any]]])->Dict[str,Dict[str,Any]]:
    term_stats={}
    for chunk in chunks:
        translated=list(translated_by_chunk.get(chunk.chunk_id,[]))
        if len(translated)<len(chunk.source_sentences):translated.extend([""]*(len(chunk.source_sentences)-len(translated)))
        for term in terms_by_chunk.get(chunk.chunk_id,[]):
            if term.get("source_type")!="kb":continue
            source=str(term.get("source","")).strip()
            if not source:continue
            source_lc=source.lower()
            stats=term_stats.setdefault(source_lc,{"source":source,"target":str(term.get("target","")),"count":0,"hit":0})
            for i,src_sent in enumerate(chunk.source_sentences):
                if not contains_term(src_sent,source):continue
                stats["count"]+=1;out_sent=translated[i]
                if stats["target"] in out_sent and all((not bad) or (bad not in out_sent) for bad in term.get("forbid",[])):stats["hit"]+=1
    return term_stats

def run_prepare(args:argparse.Namespace)->int:
    start_ts=time.time()
    input_path=Path(args.input).resolve();output_path=Path(args.output).resolve()
    report_path=Path(args.report).resolve() if args.report else output_path.with_name("translation_report.json").resolve()
    work_dir=Path(args.work_dir).resolve() if args.work_dir else output_path.parent.resolve()/"work"
    kb_dir=Path(args.kb_dir).resolve();overrides_path=Path(args.overrides).resolve()
    params=resolve_params(args)
    if bool(args.clean_work):
        cleanup_protocol_workspace(work_dir)
    payload=prepare_job(input_path,output_path,report_path,kb_dir,overrides_path,work_dir,params,args.bootstrap_force,args.profile)
    paths=build_protocol_paths(work_dir)
    write_json(paths["translation_job"],payload)
    print(f"Prepared job: {paths['translation_job']}")
    print(f"Job id: {payload.get('job_id','')}")
    print(f"Expected result file: {paths['translation_result']}")
    print(f"Chunks: {payload.get('chunks',0)}")
    print(f"LatencyMs: {int((time.time()-start_ts)*1000)}")
    return 0

def run_validate(args:argparse.Namespace)->int:
    work_dir=Path(args.work_dir).resolve();paths=build_protocol_paths(work_dir);repair_paths=build_protocol_repair_paths(work_dir,int(args.round))
    job_path=Path(args.job).resolve() if args.job else paths["translation_job"]
    result_path=Path(args.result).resolve() if args.result else paths["translation_result"]
    report_path=Path(args.validation_report).resolve() if args.validation_report else paths["validation_report"]
    repair_job_path=Path(args.repair_job).resolve() if args.repair_job else repair_paths["repair_job"]
    repair_result_path=Path(args.repair_result).resolve() if args.repair_result else repair_paths["repair_result"]
    if not job_path.exists(): raise FileNotFoundError(f"Job file not found: {job_path}. Run prepare first.")
    if not result_path.exists(): raise FileNotFoundError(f"Result file not found: {result_path}. Please fill translation result first.")
    job=load_json(job_path)
    chunks,terms_by_chunk,_soft_terms,p1_terms_by_chunk,p2_terms_by_chunk,params=chunks_from_job(job)
    result_payload=load_result_payload(result_path)
    result_map=load_result_map_from_payload(result_payload)
    violations=[];repair_tasks=[];translated_by_chunk={};advisories=[]
    identity_ok=is_result_identity_match(job,result_payload)
    if not identity_ok:
        violations.append(build_identity_mismatch_violation(job,result_payload,int(args.round)))
    for chunk in chunks:
        translated_by_chunk[chunk.chunk_id]=list(result_map.get(chunk.chunk_id,[]))
    if identity_ok:
        for chunk in chunks:
            got=result_map.get(chunk.chunk_id)
            missing=add_alignment_violations(chunk,got,violations,"validate",int(args.round))
            cur=list(got) if got is not None else []
            translated_by_chunk[chunk.chunk_id]=list(cur)
            if missing:
                repair_tasks.extend([{"chunk_id":chunk.chunk_id,"sentence_id":sid,"source_sentence":src,"current_translation":"","locked_terms":[t for t in terms_by_chunk.get(chunk.chunk_id,[]) if contains_term(src,str(t.get("source","")))],"preferred_terms":[t for t in p1_terms_by_chunk.get(chunk.chunk_id,[]) if contains_term(src,str(t.get("source","")))]} for sid,src in enumerate(chunk.source_sentences)])
                continue
            chunk_violations=validate_chunk(chunk,cur,terms_by_chunk.get(chunk.chunk_id,[]),params,"validate",int(args.round));violations.extend(chunk_violations)
            preferred_terms=list(p1_terms_by_chunk.get(chunk.chunk_id,[]))+[dict(x,level="p2") for x in p2_terms_by_chunk.get(chunk.chunk_id,[])]
            pref_violations,pref_advisories=validate_preferred_terms(chunk,cur,preferred_terms,params,"validate",int(args.round))
            violations.extend(pref_violations);advisories.extend(pref_advisories)
            repair_tasks.extend(group_repair_tasks(chunk,cur,terms_by_chunk.get(chunk.chunk_id,[]),p1_terms_by_chunk.get(chunk.chunk_id,[]),chunk_violations+pref_violations))
    metrics=compute_metrics(chunks,translated_by_chunk,terms_by_chunk,preferred_terms_by_chunk=p1_terms_by_chunk)
    language_metrics=compute_language_metrics(chunks,translated_by_chunk)
    strict_gate=bool(args.strict_gate)
    passed=identity_ok and len(violations)==0 and len(repair_tasks)==0
    report_payload={"version":1,"schema_version":PROTOCOL_SCHEMA_VERSION,"created_at":utc_now(),"round":int(args.round),"strict_gate":strict_gate,"identity_match":identity_ok,"passed":passed,"job_file":str(job_path),"result_file":str(result_path),"repair_job_file":str(repair_job_path),"repair_result_file":str(repair_result_path),"metrics":metrics,"language_metrics":language_metrics,"violation_count":len(violations),"repair_task_count":len(repair_tasks),"advisory_count":len(advisories),"violations":violations,"repair_tasks":repair_tasks,"advisories":advisories}
    write_json(report_path,report_payload)
    if repair_tasks and identity_ok:
        write_json(repair_job_path,{"version":1,"created_at":utc_now(),"round":int(args.round),"task":"Repair only listed sentences. Keep placeholders and numbers unchanged, satisfy locked_terms and preferred_terms(p1).","format":{"items":[{"chunk_id":"c_0001","sentence_id":0,"translated":"..."}]},"expected_result_file":str(repair_result_path),"items":repair_tasks})
    elif repair_job_path.exists():
        repair_job_path.unlink()
    print(f"Validation report: {report_path}")
    print(f"Passed: {passed}")
    print(f"Violations: {len(violations)}")
    print(f"Repair tasks: {len(repair_tasks)}")
    print(f"Advisories: {len(advisories)}")
    if repair_tasks:
        print(f"Repair job: {repair_job_path}")
        print(f"Expected repair result: {repair_result_path}")
    if not identity_ok:
        return 2
    if strict_gate and not passed:
        return 3
    return 0

def run_apply_repair(args:argparse.Namespace)->int:
    work_dir=Path(args.work_dir).resolve();paths=build_protocol_paths(work_dir);repair_paths=build_protocol_repair_paths(work_dir,int(args.round))
    job_path=Path(args.job).resolve() if args.job else paths["translation_job"]
    result_path=Path(args.result).resolve() if args.result else paths["translation_result"]
    repair_result_path=Path(args.repair_result).resolve() if args.repair_result else repair_paths["repair_result"]
    if not job_path.exists(): raise FileNotFoundError(f"Job file not found: {job_path}")
    if not result_path.exists(): raise FileNotFoundError(f"Result file not found: {result_path}")
    if not repair_result_path.exists(): raise FileNotFoundError(f"Repair result not found: {repair_result_path}")
    job=load_json(job_path);chunks,_,_,_,_,_=chunks_from_job(job)
    result_payload=load_result_payload(result_path)
    if not is_result_identity_match(job,result_payload):
        raise RuntimeError("Result file identity mismatch. Please regenerate translation.result.json from current translation.job.json.")
    result_map=load_result_map_from_payload(result_payload);repairs=load_repair_result_from_file(repair_result_path)
    chunk_len={c.chunk_id:len(c.source_sentences) for c in chunks};applied=0;skipped=0
    for fix in repairs:
        cid=str(fix.get("chunk_id","")).strip();sid=int(fix.get("sentence_id",-1));txt=str(fix.get("translated","")).strip()
        if not cid or sid<0 or not txt: skipped+=1;continue
        cur=result_map.setdefault(cid,[]);exp=chunk_len.get(cid,0)
        if exp and len(cur)<exp: cur.extend([""]*(exp-len(cur)))
        if sid>=len(cur): skipped+=1;continue
        cur[sid]=txt;applied+=1
    write_result_map(result_path,chunks,result_map,job=job,base_payload=result_payload)
    print(f"Applied repairs: {applied}")
    print(f"Skipped repairs: {skipped}")
    print(f"Updated result: {result_path}")
    return 0

def run_finalize(args:argparse.Namespace)->int:
    start_ts=time.time()
    work_dir=Path(args.work_dir).resolve();paths=build_protocol_paths(work_dir)
    job_path=Path(args.job).resolve() if args.job else paths["translation_job"]
    result_path=Path(args.result).resolve() if args.result else paths["translation_result"]
    validation_report_path=Path(args.validation_report).resolve() if args.validation_report else paths["validation_report"]
    if not job_path.exists(): raise FileNotFoundError(f"Job file not found: {job_path}")
    if not result_path.exists(): raise FileNotFoundError(f"Result file not found: {result_path}")
    strict_gate=bool(args.strict_gate)
    if strict_gate:
        if not validation_report_path.exists():
            raise RuntimeError(f"Validation report not found: {validation_report_path}. Run validate first.")
        validation_payload=load_json(validation_report_path)
        if not bool(validation_payload.get("passed",False)):
            raise RuntimeError("Finalize blocked: latest validate did not pass.")
        if str(validation_payload.get("job_file",""))!=str(job_path) or str(validation_payload.get("result_file",""))!=str(result_path):
            raise RuntimeError("Finalize blocked: validate report does not match current job/result.")
    job=load_json(job_path)
    chunks,terms_by_chunk,soft_terms_by_chunk,p1_terms_by_chunk,p2_terms_by_chunk,params=chunks_from_job(job)
    result_payload=load_result_payload(result_path)
    if not is_result_identity_match(job,result_payload):
        raise RuntimeError("Result file identity mismatch. Please regenerate translation.result.json from current translation.job.json.")
    result_map=load_result_map_from_payload(result_payload)
    output_path=Path(args.output).resolve() if args.output else Path(str(job.get("output",""))).resolve()
    report_path=Path(args.report).resolve() if args.report else Path(str(job.get("report",""))).resolve()
    overrides_path=Path(str(job.get("overrides",""))).resolve()
    violations=[];translated_by_chunk={};advisories=[];normalization_replacements=0
    for chunk in chunks:
        got=result_map.get(chunk.chunk_id);missing=add_alignment_violations(chunk,got,violations,"finalize",int(args.round))
        cur=list(got) if got is not None else []
        if missing: cur=[""]*len(chunk.source_sentences)
        if bool(params.get("enable_term_normalization",True)):
            normalize_terms=merge_terms_unique(terms_by_chunk.get(chunk.chunk_id,[]),p1_terms_by_chunk.get(chunk.chunk_id,[]))
            cur,repl=apply_term_normalization(chunk,cur,normalize_terms)
            normalization_replacements+=int(repl)
        unresolved=apply_unresolved_policy(chunk,cur,terms_by_chunk.get(chunk.chunk_id,[]),str(params.get("unresolved_policy","keep_en_with_tag")))
        if unresolved:
            violations.append({"chunk_id":chunk.chunk_id,"sentence_id":-1,"type":"term_unresolved_applied","source_term":"","expected":"","actual":str(unresolved),"phase":"finalize","round":int(args.round)})
        violations.extend(validate_chunk(chunk,cur,terms_by_chunk.get(chunk.chunk_id,[]),params,"final",int(args.round)))
        preferred_terms=list(p1_terms_by_chunk.get(chunk.chunk_id,[]))+[dict(x,level="p2") for x in p2_terms_by_chunk.get(chunk.chunk_id,[])]
        pref_violations,pref_advisories=validate_preferred_terms(chunk,cur,preferred_terms,params,"final",int(args.round))
        violations.extend(pref_violations);advisories.extend(pref_advisories)
        translated_by_chunk[chunk.chunk_id]=cur
    metrics=compute_metrics(chunks,translated_by_chunk,terms_by_chunk,preferred_terms_by_chunk=p1_terms_by_chunk)
    language_metrics=compute_language_metrics(chunks,translated_by_chunk)
    gate_failures=[]
    if metrics.get("term_unresolved",0)>0:
        gate_failures.append(f"term_unresolved={metrics.get('term_unresolved',0)}")
    if metrics.get("placeholder_errors",0)>0:
        gate_failures.append(f"placeholder_errors={metrics.get('placeholder_errors',0)}")
    if float(language_metrics.get("en_only_line_ratio",0.0))>float(args.max_en_only_line_ratio):
        gate_failures.append(f"en_only_line_ratio={language_metrics.get('en_only_line_ratio',0.0)}>{args.max_en_only_line_ratio}")
    hard_violation_count=len([v for v in violations if v.get("type")!="term_unresolved_applied"])
    if hard_violation_count>0:
        gate_failures.append(f"hard_violations={hard_violation_count}")
    passed=len(gate_failures)==0
    promoted=[];drift_forbid_added=[]
    if passed and overrides_path.exists():
        rules=normalize_rules(load_overrides(overrides_path))
        term_stats=collect_term_stats(chunks,translated_by_chunk,terms_by_chunk)
        promoted=apply_auto_promotion(overrides_path,rules,term_stats,params)
        drift_path=get_drift_history_path(overrides_path);drift_history=load_drift_history(drift_path)
        update_drift_history(drift_history,violations)
        added=merge_drift_forbid_into_rules(rules,drift_history,int(params.get("drift_forbid_min_count",2)))
        save_drift_history(drift_path,drift_history)
        if added: rules=normalize_rules(rules);save_overrides(overrides_path,rules)
        drift_forbid_added=[{"source":s,"alias":a} for s,a in added]
    runtime_terms={cid:{"locked_terms":terms_by_chunk.get(cid,[]),"p1_terms":p1_terms_by_chunk.get(cid,[]),"p2_terms":p2_terms_by_chunk.get(cid,[]),"soft_terms":soft_terms_by_chunk.get(cid,[])} for cid in [c.chunk_id for c in chunks]}
    report={"version":1,"schema_version":PROTOCOL_SCHEMA_VERSION,"created_at":utc_now(),"profile":job.get("profile","balanced"),"strict_gate":strict_gate,"passed":passed,"gate_failures":gate_failures,"validation_report_file":str(validation_report_path),"params":params,"input":str(job.get("input","")),"output":str(output_path),"kb_dir":str(job.get("kb_dir","")),"overrides":str(overrides_path),"work_dir":str(work_dir),"chunks":len(chunks),"latency_ms":int((time.time()-start_ts)*1000),"repair_rounds":int(args.round),"promoted_terms":promoted,"drift_forbid_added":drift_forbid_added,"term_normalization_replacements":normalization_replacements,**metrics,"language_metrics":language_metrics,"runtime_terms":runtime_terms,"violations":violations,"advisories":advisories}
    report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if strict_gate and not passed:
        print(f"Report: {report_path}")
        print(f"Finalize blocked by strict gate: {'; '.join(gate_failures)}")
        return 4
    final_text="\n\n".join([merge_chunk_text(translated_by_chunk[c.chunk_id]) for c in chunks if merge_chunk_text(translated_by_chunk[c.chunk_id]).strip()]).strip()+"\n"
    output_path.parent.mkdir(parents=True,exist_ok=True);output_path.write_text(final_text,encoding="utf-8")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    print(f"Metrics: term_hit={report['term_hit']}/{report['term_total']} term_unresolved={report['term_unresolved']} placeholder_errors={report['placeholder_errors']}")
    return 0

def add_prepare_args(p:argparse.ArgumentParser)->None:
    p.add_argument("--profile",default="balanced")
    p.add_argument("--chunk-chars",type=int);p.add_argument("--chunk-chars-min",type=int);p.add_argument("--chunk-chars-max",type=int)
    p.add_argument("--batch-chunks",type=int);p.add_argument("--kb-topk",type=int);p.add_argument("--lock-score-threshold",type=float)
    p.add_argument("--lock-margin-threshold",type=float);p.add_argument("--max-repair-rounds",type=int)
    p.add_argument("--promotion-min-frequency",type=int);p.add_argument("--promotion-pass-runs",type=int)
    p.add_argument("--unresolved-policy",default=None);p.add_argument("--placeholder-strict",type=bool_flag,default=None)
    p.add_argument("--kb-disable-semantic",type=bool_flag,default=None)
    p.add_argument("--enable-p1-repairs",type=bool_flag,default=None)
    p.add_argument("--enable-entity-guard",type=bool_flag,default=None)
    p.add_argument("--enable-term-normalization",type=bool_flag,default=None)
    p.add_argument("--p1-score-threshold",type=float)
    p.add_argument("--p2-score-threshold",type=float)
    p.add_argument("--p1-coverage-threshold",type=float)
    p.add_argument("--p2-coverage-threshold",type=float)
    p.add_argument("--p1-max-terms",type=int)
    p.add_argument("--p2-max-terms",type=int)
    p.add_argument("--bootstrap-force",action="store_true")
    p.add_argument("--bootstrap-score-threshold",type=float)
    p.add_argument("--bootstrap-margin-threshold",type=float)
    p.add_argument("--bootstrap-min-frequency",type=int)
    p.add_argument("--bootstrap-max-rules",type=int)
    p.add_argument("--drift-forbid-min-count",type=int)
    p.add_argument("--clean-work",type=bool_flag,default=True)

def parse_args()->argparse.Namespace:
    script_dir=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description="Model-orchestrated translation utility pipeline")
    sub=p.add_subparsers(dest="command",required=True)
    sp=sub.add_parser("prepare",help="Build translation job with chunks and locked terms")
    sp.add_argument("--input",required=True);sp.add_argument("--output",required=True)
    sp.add_argument("--kb-dir",default=str(script_dir/"kb"))
    sp.add_argument("--report",default="")
    sp.add_argument("--overrides",default=str(script_dir/"term_overrides.json"))
    sp.add_argument("--work-dir",default="")
    add_prepare_args(sp)
    sv=sub.add_parser("validate",help="Validate translation result and emit repair tasks")
    sv.add_argument("--work-dir",required=True);sv.add_argument("--job",default="");sv.add_argument("--result",default="")
    sv.add_argument("--validation-report",default="");sv.add_argument("--repair-job",default="");sv.add_argument("--repair-result",default="")
    sv.add_argument("--round",type=int,default=1)
    sv.add_argument("--strict-gate",type=bool_flag,default=True)
    sa=sub.add_parser("apply-repair",help="Apply repair.result.rN.json into translation.result.json")
    sa.add_argument("--work-dir",required=True);sa.add_argument("--job",default="");sa.add_argument("--result",default="");sa.add_argument("--repair-result",default="")
    sa.add_argument("--round",type=int,default=1)
    sf=sub.add_parser("finalize",help="Finalize output and translation report")
    sf.add_argument("--work-dir",required=True);sf.add_argument("--job",default="");sf.add_argument("--result",default="")
    sf.add_argument("--output",default="");sf.add_argument("--report",default="");sf.add_argument("--round",type=int,default=1)
    sf.add_argument("--validation-report",default="")
    sf.add_argument("--strict-gate",type=bool_flag,default=True)
    sf.add_argument("--max-en-only-line-ratio",type=float,default=STRICT_MAX_EN_ONLY_LINE_RATIO)
    return p.parse_args()

def main()->int:
    args=parse_args()
    if args.command=="prepare": return run_prepare(args)
    if args.command=="validate": return run_validate(args)
    if args.command=="apply-repair": return run_apply_repair(args)
    if args.command=="finalize": return run_finalize(args)
    raise RuntimeError(f"Unknown command: {args.command}")

if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as e:
        print(f"Pipeline failed: {e}",file=sys.stderr);raise
