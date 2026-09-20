#!/usr/bin/env python3
"""Read-only GitHub metadata ranking. Popularity is queue priority, not safety proof.
Consumes the backlog dry-run manifest. No AI calls or GitHub/catalog writes.
"""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import urllib.request


def rank(items, metadata, limit=100):
    if not 1 <= limit <= 100: raise ValueError('Daily shortlist cap is 100')
    out=[]
    for row in items:
        if row.get('disposition')!='canonical_actionable':continue
        source=row.get('source','')
        m=metadata.get(source)
        if not m or m.get('isArchived'):continue
        out.append({'issue':row['number'],'source':source,'subpath':row.get('subpath'),
                    'stars':m['stargazerCount'],'forks':m['forkCount'],'pushedAt':m.get('pushedAt'),
                    'license':(m.get('licenseInfo') or {}).get('spdxId'),
                    'reviewRequired':True,'popularityIsNotSafety':True})
    out.sort(key=lambda x:(x['stars'],x['forks'],x['pushedAt'] or ''),reverse=True)
    return out[:limit]


def collect(sources):
    token=os.environ.get('GH_TOKEN')
    if not token:raise ValueError('GH_TOKEN required for live metadata')
    valid=[]
    for source in sorted(set(sources)):
        match=re.fullmatch(r'https://github.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)',source or '')
        if match:valid.append((source,*match.groups()))
    result={}
    for start in range(0,len(valid),40):
        batch=valid[start:start+40]
        fields=' '.join('r%d:repository(owner:%s,name:%s){stargazerCount forkCount isArchived pushedAt licenseInfo{spdxId}}'%(i,json.dumps(owner),json.dumps(repo)) for i,(_,owner,repo) in enumerate(batch))
        req=urllib.request.Request('https://api.github.com/graphql',data=json.dumps({'query':'query{'+fields+'}'}).encode(),headers={'Authorization':'Bearer '+token,'User-Agent':'ProSkills-Popularity','Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=45) as response:d=json.load(response)
        if not isinstance(d.get('data'),dict):raise ValueError('Metadata query failed')
        for i,(source,_,_) in enumerate(batch):result[source]=d['data'].get('r'+str(i))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True);p.add_argument('--metadata',required=True);p.add_argument('--refresh',action='store_true');p.add_argument('--output',required=True);p.add_argument('--limit',type=int,default=100);a=p.parse_args()
    data=json.loads(Path(a.manifest).read_text());items=data['items']
    if a.refresh:
        meta=collect([x.get('source') for x in items if x.get('disposition')=='canonical_actionable'])
        payload={'observedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'repositories':meta};Path(a.metadata).write_text(json.dumps(payload))
    else:payload=json.loads(Path(a.metadata).read_text())
    out={'observedAt':payload['observedAt'],'scope':'existing actionable backlog; GitHub stars, not viral velocity','newListingsPublished':0,'aiCalls':0,'candidates':rank(items,payload['repositories'],a.limit)}
    Path(a.output).write_text(json.dumps(out,indent=2));print(json.dumps({'ranked':len(out['candidates']),'metadataRepositories':len(payload['repositories']),'aiCalls':0}))
