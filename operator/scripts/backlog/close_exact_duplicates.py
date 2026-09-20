#!/usr/bin/env python3
"""Conservative exact-body duplicate cleanup. Frozen plan; live revalidation; no comments.
GH_TOKEN is read only from environment. No catalog changes or candidate code execution.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

REPO = 'ProSkillsMD/proskills'
ALLOWED = {'submission', 'auto-discovered'}
# Existing manual/no-op holds from the operator's supervised reconciliation.
PROTECTED = {714, 3644, 4353, 5214, 5226, 5403, 2850, 2028, 2029, 2030}


def digest(issue):
    return hashlib.sha256(json.dumps([issue['title'].strip(), (issue.get('body') or '').strip()], ensure_ascii=False).encode()).hexdigest()


def eligible(issue):
    labels = {x['name'] if isinstance(x, dict) else x for x in issue.get('labels', [])}
    return (issue['number'] not in PROTECTED and issue.get('state') == 'open'
            and issue.get('comments') == 0 and not issue.get('assignees')
            and not issue.get('milestone') and 'auto-discovered' in labels
            and labels <= ALLOWED)


def make_plan(issues):
    groups = collections.defaultdict(list)
    for issue in issues:
        if issue.get('state') == 'open' and 'pull_request' not in issue:
            groups[digest(issue)].append(issue)
    result = []
    for checksum, group in groups.items():
        group.sort(key=lambda x: (x['created_at'], x['number']))
        keeper = group[0]
        for issue in group[1:]:
            if eligible(issue):
                result.append({'number': issue['number'], 'canonical': keeper['number'], 'digest': checksum})
    return sorted(result, key=lambda x: x['number'])


def api(path, data=None):
    token = os.environ.get('GH_TOKEN')
    if not token:
        raise RuntimeError('GH_TOKEN not available')
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + path,
        headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'ProSkills-Exact-Dedupe',
                 'Accept': 'application/vnd.github+json'},
        data=json.dumps(data).encode() if data is not None else None,
        method='PATCH' if data is not None else 'GET')
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def apply_plan(plan, journal, limit=100, delay=2):
    if not 1 <= limit <= 100 or delay < 2:
        raise ValueError('At most 100 writes/run; minimum two-second write spacing')
    journal = Path(journal)
    if journal.is_symlink(): raise ValueError('Journal symlink rejected')
    journal.parent.mkdir(parents=True, exist_ok=True)
    events = [json.loads(x) for x in journal.read_text().splitlines()] if journal.exists() else []
    done = {x['number'] for x in events if x['status'] in ('closed', 'skip')}
    counts = {'closed': 0, 'skipped': 0}
    def record(event):
        fd=os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a') as stream:
            stream.write(json.dumps(event)+'\n');stream.flush();os.fsync(stream.fileno())
    for row in plan:
        if row['number'] in done: continue
        current = api('/issues/' + str(row['number']))
        canonical = api('/issues/' + str(row['canonical']))
        if not (eligible(current) and canonical.get('state') == 'open'
                and digest(current) == row['digest'] == digest(canonical)):
            record({'number':row['number'],'status':'skip','reason':'changed_or_ineligible'})
            counts['skipped'] += 1
            continue
        original={'state':current['state'],'state_reason':current.get('state_reason'),
                  'labels':[x['name'] for x in current['labels']]}
        record({'number':row['number'],'canonical':row['canonical'],'status':'before','original':original})
        result=api('/issues/'+str(row['number']),{'state':'closed','state_reason':'not_planned',
                   'labels':original['labels']+['curio:duplicate']})
        if result.get('state')!='closed':raise RuntimeError('Closure not confirmed')
        record({'number':row['number'],'canonical':row['canonical'],'status':'closed'})
        counts['closed']+=1
        if counts['closed'] >= limit:break
        time.sleep(delay)
    return counts


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--issues');p.add_argument('--plan',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--journal');p.add_argument('--limit',type=int,default=100)
    args=p.parse_args()
    if args.apply:
        if not args.journal:p.error('--journal required')
        print(json.dumps(apply_plan(json.loads(Path(args.plan).read_text()),args.journal,args.limit)))
    else:
        if not args.issues:p.error('--issues required to build plan')
        plan=make_plan(json.loads(Path(args.issues).read_text()))
        Path(args.plan).write_text(json.dumps(plan,indent=2))
        print(json.dumps({'candidates':len(plan),'writes':0}))
