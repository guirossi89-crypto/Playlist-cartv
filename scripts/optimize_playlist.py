#!/usr/bin/env python3
from __future__ import annotations
import json, multiprocessing, math, re, sys, time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
import requests
from urllib3.util import Timeout as Urllib3Timeout

TIMEOUT = 12
WORKERS = 16
SOURCE_BUDGET = 12
SEGMENTS_TO_TEST = 2
FAILURES_BEFORE_REMOVAL = 2
UA = "Mozilla/5.0 (compatible; PlaylistHealthCheck/1.0)"

@dataclass
class Entry:
    block: list[str]
    url: str
    key: str
    label: str
    quality: int
    headers: dict[str, str]
    order: int

@dataclass
class Check:
    entry: Entry
    ok: bool
    quality: int
    latency: int
    reason: str

def quality_of(s):
    dims = re.findall(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", s)
    if dims: return max(int(w)*int(h) for w,h in dims)
    t=s.upper()
    for token,score in (("4K",3840*2160),("UHD",3840*2160),("FHD",1920*1080),("1080",1920*1080),("HD",1280*720),("720",1280*720),("SD",854*480),("540",854*480),("480",854*480),("360",640*360)):
        if token in t: return score
    return 0

def parse(text):
    lines=[x.rstrip("\r") for x in text.splitlines() if x.strip()]
    header=next((x for x in lines if x.startswith("#EXTM3U")),"#EXTM3U")
    entries=[]; pending=[]
    for line in lines:
        if line.startswith("#EXTINF:"):
            pending=[line]
        elif pending:
            pending.append(line)
            if line and not line.startswith("#"):
                info=pending[0]
                attrs=dict(re.findall(r'([\w-]+)="([^"]*)"',info.split(",",1)[0]))
                title=info.split(",",1)[1].strip() if "," in info else "Canal"
                raw=attrs.get("tvg-id") or attrs.get("tvg-name") or title
                key=re.sub(r"\s*\[[^\]]*(?:4K|UHD|FHD|HD|SD|\d{3,4}\s*[xX]\s*\d{3,4})[^\]]*\]\s*$","",raw,flags=re.I)
                key=re.sub(r"\s+"," ",key).strip().casefold() or title.casefold()
                headers={}
                for d in pending[1:-1]:
                    m=re.match(r"#EXTVLCOPT:([^=]+)=(.*)",d,re.I)
                    if m:
                        k,v=m.group(1).lower(),m.group(2).strip()
                        if k=="http-referrer": headers["Referer"]=v
                        elif k=="http-user-agent": headers["User-Agent"]=v
                entries.append(Entry(pending[:],line,key,title,quality_of(info),headers,len(entries)))
                pending=[]
    return header,entries

def request_timeout(deadline, timeout):
    remaining=deadline-time.monotonic()
    if remaining <= 0: raise requests.Timeout("limite total da fonte excedido")
    cap=min(float(timeout),remaining)
    return Urllib3Timeout(total=cap,connect=min(5.0,cap),read=min(5.0,cap))

def get(url,headers,deadline,timeout):
    start=time.monotonic()
    r=requests.get(url,headers={"User-Agent":UA,**headers},timeout=request_timeout(deadline,timeout),allow_redirects=True)
    ms=int((time.monotonic()-start)*1000)
    r.raise_for_status()
    return r,ms

def is_hls_playlist(response):
    body=response.content[:1_000_000].decode(response.encoding or "utf-8",errors="replace")
    if not body.lstrip().startswith("#EXTM3U"): return None
    # Reject HTML/error pages and malformed playlists rather than approving by response size.
    head=body[:4096].lower()
    if "<html" in head or "<!doctype html" in head or "<body" in head: return None
    rows=[x.strip() for x in body.splitlines() if x.strip()]
    if not rows or rows[0] != "#EXTM3U": return None
    if any(x.startswith("#EXT-X-STREAM-INF:") for x in rows): return ("master",rows)
    has_segments=any(x and not x.startswith("#") for x in rows)
    has_media_tag=any(x.startswith(("#EXTINF:","#EXT-X-TARGETDURATION:","#EXT-X-PART:")) for x in rows)
    if has_segments and has_media_tag: return ("media",rows)
    return None

def variant_score(pixels, bandwidth, latency):
    # Combined score: resolution, declared bitrate and measured response latency.
    # Bandwidth is HLS metadata, not a direct measurement of decoded video quality.
    p=max(0,pixels or 0)
    b=max(0,bandwidth or 0)
    resolution_factor=(max(p,640*360)/(1920*1080))**0.55
    bitrate_factor=(max(b,1_500_000)/5_000_000)**0.35 if b else 1.0
    latency_factor=(1.0+max(0,latency)/2500.0)**0.10
    return int(1_000_000*resolution_factor*bitrate_factor/latency_factor)

def test_media_playlist(rows,base_url,headers,deadline,timeout):
    urls=[]
    for row in rows:
        if row and not row.startswith("#"):
            urls.append(urljoin(base_url,row))
            if len(urls)>=SEGMENTS_TO_TEST: break
    if not urls: return False,0,"playlist sem segmentos"
    tested=0; total_latency=0
    for url in urls:
        try:
            start=time.monotonic()
            response=requests.get(url,headers={"User-Agent":UA,**headers,"Range":"bytes=0-511"},
                timeout=request_timeout(deadline,timeout),stream=True,allow_redirects=True)
            chunk=next(response.iter_content(512),b"")
            status=response.status_code
            response.close()
            total_latency+=int((time.monotonic()-start)*1000)
            if status in (200,206) and len(chunk)>=64:
                tested+=1
            else:
                return False,total_latency,"segmento inválido"
        except requests.RequestException:
            return False,total_latency,"falha no segmento"
    return tested==len(urls),total_latency,f"{tested} segmento(s) HLS verificado(s)"

def check(e,timeout):
    deadline=time.monotonic()+SOURCE_BUDGET
    try:
        response,latency=get(e.url,e.headers,deadline,timeout)
        parsed=is_hls_playlist(response)
        if not parsed: return Check(e,False,0,latency,"resposta não é uma playlist HLS válida")
        kind,rows=parsed
        if kind=="media":
            ok,seg_latency,reason=test_media_playlist(rows,response.url,e.headers,deadline,timeout)
            score=variant_score(e.quality,0,latency+seg_latency)
            return Check(e,ok,score if ok else 0,latency+seg_latency,reason)
        variants=[]
        for i,row in enumerate(rows):
            if row.startswith("#EXT-X-STREAM-INF:"):
                attrs=row.split(":",1)[1]
                res=re.search(r"RESOLUTION=(\d+)x(\d+)",attrs,re.I)
                pixels=int(res.group(1))*int(res.group(2)) if res else 0
                bw=re.search(r"(?:AVERAGE-BANDWIDTH|BANDWIDTH)=(\d+)",attrs,re.I)
                bandwidth=int(bw.group(1)) if bw else 0
                nxt=next((x for x in rows[i+1:] if not x.startswith("#")),None)
                if nxt:
                    # A primeira URL não-comentário após STREAM-INF é a variante correspondente.
                    variants.append((variant_score(pixels,bandwidth,latency),pixels,bandwidth,urljoin(response.url,nxt)))
        if not variants: return Check(e,False,0,latency,"manifesto master sem variantes")
        variants.sort(reverse=True)
        failures=[]
        for initial_score,pixels,bandwidth,url in variants:
            try:
                vr,variant_latency=get(url,e.headers,deadline,timeout)
                latency+=variant_latency
                child=is_hls_playlist(vr)
                if not child or child[0]!="media":
                    failures.append("variante não contém playlist HLS de mídia")
                    continue
                ok,seg_latency,reason=test_media_playlist(child[1],vr.url,e.headers,deadline,timeout)
                latency+=seg_latency
                if ok:
                    return Check(e,True,variant_score(pixels,bandwidth,latency),latency,"variante aprovada; "+reason)
                failures.append(reason)
            except requests.RequestException as ex:
                failures.append(type(ex).__name__)
                continue
        return Check(e,False,0,latency,"nenhuma variante reproduzível: "+(failures[-1] if failures else "falha"))
    except requests.RequestException as ex:
        return Check(e,False,0,0,type(ex).__name__)
    except Exception as ex:
        return Check(e,False,0,0,type(ex).__name__)

def _check_in_child(index,entry,timeout,result_queue):
    try: result_queue.put((index,check(entry,timeout)))
    except BaseException as ex: result_queue.put((index,Check(entry,False,0,0,type(ex).__name__)))

def run_checks(entries,timeout):
    ctx=multiprocessing.get_context("fork")
    result_queue=ctx.Queue(); pending=iter(enumerate(entries)); active={}
    results=[None]*len(entries); completed=0; last_reported=0; started=time.monotonic()
    def launch():
        try: index,entry=next(pending)
        except StopIteration: return False
        proc=ctx.Process(target=_check_in_child,args=(index,entry,timeout,result_queue),daemon=True)
        proc.start(); active[index]=(proc,entry,time.monotonic()+SOURCE_BUDGET); return True
    try:
        for _ in range(min(WORKERS,len(entries))): launch()
        while active:
            try:
                index,result=result_queue.get(timeout=0.10)
                item=active.pop(index,None)
                if item is not None:
                    proc,entry,deadline=item; proc.join(timeout=0.2)
                    if proc.is_alive(): proc.terminate(); proc.join(timeout=1)
                    results[index]=result; completed+=1
            except Exception: pass
            now=time.monotonic()
            for index in [i for i,(_,_,deadline) in active.items() if now>=deadline]:
                proc,entry,deadline=active.pop(index)
                if proc.is_alive(): proc.terminate()
                proc.join(timeout=1); results[index]=Check(entry,False,0,SOURCE_BUDGET*1000,"HardTimeout"); completed+=1
            for index,(proc,entry,deadline) in list(active.items()):
                if not proc.is_alive() and proc.exitcode is not None:
                    proc.join(); active.pop(index); results[index]=Check(entry,False,0,0,"ProcessExit"); completed+=1
            while len(active)<WORKERS and launch(): pass
            if completed and (completed-last_reported>=25 or completed==len(entries)):
                print(f"Progresso: {completed}/{len(entries)} fontes — {time.monotonic()-started:.1f}s",flush=True); last_reported=completed
    finally:
        for proc,entry,deadline in active.values():
            if proc.is_alive(): proc.terminate()
            proc.join(timeout=1)
        result_queue.close(); result_queue.join_thread()
    return results

def load_state(path):
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError): return {}

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",default="playlist-edited (1).m3u")
    ap.add_argument("--output",default="playlist-otimizada.m3u")
    ap.add_argument("--state",default="playlist-health-state.json")
    ap.add_argument("--timeout",type=int,default=TIMEOUT)
    a=ap.parse_args()
    src=Path(a.input); output=Path(a.output); state_path=Path(a.state)
    if not src.is_file(): print(f"Arquivo original não encontrado: {src}",file=sys.stderr); return 2
    header,entries=parse(src.read_text(encoding="utf-8-sig",errors="replace"))
    if not entries: print("Nenhuma entrada M3U válida.",file=sys.stderr); return 2
    _,previous_entries=parse(output.read_text(encoding="utf-8-sig",errors="replace")) if output.is_file() else ("#EXTM3U",[])
    previous={}
    for entry in previous_entries:
        entry.order=-1  # marca como fonte anterior, preservada apenas durante a tolerância
        previous.setdefault(entry.key,entry)
    state=load_state(state_path)
    failures=state.get("failures",{}) if isinstance(state.get("failures",{}),dict) else {}
    channels=len({e.key for e in entries})
    print(f"Testando {len(entries)} fontes / {channels} canais; {WORKERS} verificações simultâneas.",flush=True)
    results=run_checks(entries,a.timeout)
    good={}
    for result in results:
        if result.ok: good.setdefault(result.entry.key,[]).append(result)
    winners={}
    for key,items in good.items():
        winners[key]=max(items,key=lambda x:(x.quality,-x.latency,-x.entry.order)).entry
    new_failures={}
    retained=0
    all_failed=not winners
    if all_failed:
        print("Nenhum canal foi validado nesta execução; saída e histórico preservados por segurança.",file=sys.stderr,flush=True)
        return 3
    for key in set(e.key for e in entries):
        if key in winners:
            new_failures.pop(key,None)
        else:
            count=int(failures.get(key,0))+1
            if key in previous and count<FAILURES_BEFORE_REMOVAL:
                winners[key]=previous[key]; retained+=1
                new_failures[key]=count
                print(f"Falha {count}/{FAILURES_BEFORE_REMOVAL}: mantendo temporariamente {previous[key].label}.",flush=True)
            elif count<FAILURES_BEFORE_REMOVAL:
                new_failures[key]=count
    chosen_orders={e.order for e in winners.values() if e.order>=0}
    # Entradas mantidas vêm da playlist anterior; emite os blocos vencedores pela ordem atual
    # e acrescenta ao final os canais preservados que não existem na origem atual.
    out=[header]
    emitted=set()
    for e in entries:
        if e.order in chosen_orders and e.key in winners and winners[e.key].order == e.order:
            out.extend(e.block); emitted.add(e.key)
    for key,e in winners.items():
        if key not in emitted and e.order<0:
            out.extend(e.block); emitted.add(key)
    if len(out)<=1:
        print("Erro: saída sem canais; arquivo não alterado.",file=sys.stderr); return 3
    output.write_text("\n".join(out)+"\n",encoding="utf-8")
    state_path.write_text(json.dumps({"failures":new_failures},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    valid=sum(x.ok for x in results)
    print(f"Aprovados: {len(winners)} canais ({retained} mantidos por tolerância); fontes válidas: {valid}/{len(results)}.",flush=True)
    print(f"Gerada: {a.output} ({len(winners)} canais)",flush=True)
    return 0

if __name__=="__main__": raise SystemExit(main())
