from aiohttp import web, ClientSession
import asyncio
import json
import os
import sys
import urllib.parse
import yaml

###############################################################################
# Configuration

def load_config():
    if len(sys.argv) != 2:
        print('Usage: server.py CONFIG-PATH')
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        try:
            config = yaml.safe_load(f)
        except Exception as exc:
            print('Loading config failed')
            raise
    return config

#repo_path = '/repo'

def github_url(repo_config):
    return 'https://github.com/' + repo_config['github']['repo']

def gitlab_url(repo_config):
    c = repo_config['gitlab']
    return f"https://oauth2:{c['access_token']}@{c['host']}/{c['repo']}"


###############################################################################
# Dealing with git repo

git_queue = asyncio.Queue()

# Helper for running subprocesses
async def run(parts, **kwargs):
    proc = await asyncio.create_subprocess_exec(
        parts[0], *parts[1:],
        stdout=sys.stdout,
        stderr=sys.stderr,
        **kwargs)

    await proc.wait()
    if proc.returncode != 0:
        raise Exception('command failed')

async def git_run(cfg, parts):
    await run(['/usr/bin/git'] + parts, cwd=cfg['path'])

# pull all refs from github push them to gitlab
async def git_pull_push(cfg, ):
    await git_run(cfg, ['remote', 'update', '-p'])
    # show refs just for debugging
    await git_run(cfg, ['show-ref'])
    await git_run(cfg, ['push', '--mirror', gitlab_url(cfg)])


# clone repo from github and pull-push once
async def init_git(cfg):
    print('Initializing local git repo')
    # note that we cannot use git_run yet here as /repo does not exist yet
    await run(['/usr/bin/git', 'clone', '--bare', '--mirror', github_url(cfg),
        cfg['path']])
    # make sure we fetch all refs on future updates
    await git_run(cfg, ['config', '--add', 'remote.origin.fetch',
        '+refs/*:refs/*'])
    # Important tweak: add branch head refs for pull requests so they show up
    # as branches in gitlab!
    await git_run(cfg, ['config', '--add', 'remote.origin.fetch',
        '+refs/pull/*:refs/heads/pull/*'])
    await git_pull_push(cfg)

# Task sequentially executing operations on the git repo in the background
async def git_task():
    for n,cfg in config['repos'].items():
        await init_git(cfg)
    while True:
        (cfg,_,op) = await git_queue.get()
        print('Git Task: initiating push-pull')
        await git_pull_push(cfg)



###############################################################################
# Dealing with github api

# Send a POST request to github API
async def github_post(cfg, ep, data, args={}):
    url = f"https://api.github.com/repos/{cfg['github']['repo']}/{ep}"
    first = True
    for k,v in args.items():
        url += '&' if not first else '?'
        first = False
        url += k + '=' + v

    print('Invoking github API:', url)

    hs = {
        'Accept': 'application/vnd.github+json',
        'Authorization': f"Bearer {cfg['github']['access_token']}",
        'X-GitHub-Api-Version': '2022-11-28'
        }

    resp = await http_client.post(url, data=json.dumps(data), headers=hs)

    text = await resp.text()
    if resp.status != 200 and resp.status != 201:
        print(text)
        print(resp.status)
        raise Exception('github API call failed')
    return json.loads(text)

# Set Github Commit status
async def github_commit_status_set(cfg, commit, context, status, desc, url):
    data = {
        'state': status,
        'target_url': url,
        'description': desc,
        'context': context
        }
    await github_post(cfg, 'statuses/' + commit, data=data)

###############################################################################
# Dealing with gitlab api

gitlab_queue = asyncio.Queue()

async def gitlab_get(cfg, ep, method='get', args={}):
    proj = urllib.parse.quote(cfg['gitlab']['repo'], safe='')
    url = f"https://{cfg['gitlab']['host']}/api/v4/projects/{proj}/{ep}"
    first = True
    for k,v in args.items():
        url += '&' if not first else '?'
        first = False
        url += k + '=' + v

    print('Invoking gitlab API:', url)

    hs = {'PRIVATE-TOKEN': cfg['gitlab']['access_token']}

    if method == 'get':
        resp = await http_client.get(url, headers=hs)
    elif method == 'post':
        resp = await http_client.post(url, headers=hs)
    else:
        raise Exception('unsupported method')
    text = await resp.text()
    if resp.status != 200:
        print(text)
        raise Exception('gitlab API call failed')
    return json.loads(text)


async def commit_status_set(cfg, commit, test, status, url):
    if status == 'success':
        gh_status = 'success'
    elif status == 'pending' or status == 'created' or status == 'running':
        gh_status = 'pending'
    elif status == 'failed':
        gh_status = 'failure'
    else:
        print('Unknown status not updating:', status)
        return

    description = f'Gitlab {test}'
    if 'job_descriptions' in cfg['gitlab']:
        description = cfg['gitlab']['job_descriptions'].get(test, description)

    await github_commit_status_set(cfg, commit, 'gitlab/' + test, gh_status,
            description, url)

# fetch most recent pipelines
async def init_statuses(cfg):
    print('initializing commit statuses')
    pls = await gitlab_get(cfg, 'pipelines', args={
        'pagination': 'keyset',
        'order_by': 'updated_at',
        'sort': 'desc',
        'per_page': '5',
        })
    for pl in pls:
        plid = pl['id']
        jobs = await gitlab_get(cfg, f'pipelines/{plid}/jobs',
                args={'per_page': '100'})
        for j in jobs:
            await commit_status_set(cfg, j['commit']['id'], j['name'],
                    j['status'], j['web_url'])
    print('Commit statuses initialized')

async def gitlab_update_pipeline(cfg, event):
    cid = event['commit']['id']
    for j in event['builds']:
        jid = j['id']
        url = f"https://{cfg['gitlab']['host']}/{cfg['gitlab']['repo']}/-/jobs/{jid}"
        await commit_status_set(cfg, cid, j['name'], j['status'], url)

# Task sequentially executing operations on the git repo in the background
async def gitlab_task():
    for n,cfg in config['repos'].items():
        await init_statuses(cfg)

    while True:
        (cfg, t, ev) = await gitlab_queue.get()
        print('GitLab Task: processing event', t)
        if t == 'Pipeline Hook':
            await gitlab_update_pipeline(cfg, ev)
        else:
            raise Exception('Unknown event type')

###############################################################################
# REST API

routes = web.RouteTableDef()

@routes.post('/{repo}/github')
async def github(request):
    repo_cfg = config['repos'][request.match_info['repo']]
    ev = request.headers['x-github-event']
    x = await request.json()
    if ev == 'push':
        print('Handling github push event...')
        await git_queue.put((repo_cfg, 'push', x))
    elif ev == 'pull_request':
        print('Handling github pull request event...')
        await git_queue.put((repo_cfg, 'push', x))
    else:
        print('Ignoring unknown github event', ev)
    return web.Response(text="OK")

@routes.post('/{repo}/gitlab')
async def gitlab(request):
    repo_cfg = config['repos'][request.match_info['repo']]
    ev = request.headers['x-gitlab-event']
    x = await request.json()
    if ev == 'Pipeline Hook':
        print('Handling gitlab pipeline event...')
        await gitlab_queue.put((repo_cfg, ev, x))
    else:
        print('Ignoring unknown gitlab event', ev)

    return web.Response(text="OK")


###############################################################################

# start by loading config
config = load_config()

loop = asyncio.get_event_loop()
asyncio.set_event_loop(loop)

http_client = ClientSession()

# initialize local git repo
loop.create_task(git_task())
loop.create_task(gitlab_task())

# start main REST API
app = web.Application()
app.add_routes(routes)
web.run_app(app, port=3000)
