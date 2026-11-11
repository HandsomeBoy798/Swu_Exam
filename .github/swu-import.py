"""One-shot, content-verified course import. Run only in the owner's Actions job."""
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zlib

REPO = 'HandsomeBoy798/Swu_Exam'
PLAN_DIGEST = 'fba20c3e0d662d99a22e6e714ab6c465ab76a006ecf0d0f64f1e3299a47f5fd4'
TEMP_FILES = ['.github/swu-import.py', '.github/swu-import-plan.json.gz.b64', '.github/workflows/swu-import-once.yml']

def blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()

def validate_structure(plan, files, temporary=True):
    expected = dict(plan['preserved'])
    expected.update({x['target']: x['sha'] for x in plan['copies']})
    expected.update({p: blob_sha(t.encode()) for p, t in plan['generated'].items()})
    clean = {p: s for p, s in files.items() if p not in TEMP_FILES}
    assert clean == expected, 'Final file manifest does not match the plan'
    if temporary:
        assert all(p in files for p in TEMP_FILES)
    assert [p for p in clean if p.rsplit('/', 1)[-1].lower().startswith('readme')] == ['README.md']
    assert {p.split('/')[0] for p in clean} == {'README.md', '各学院课程资料', '通识必修及选修课程资料'}
    prefix = plan['root'] + '/'
    grouped = {}
    for p in clean:
        if p.startswith(prefix):
            parts = p[len(prefix):].split('/')
            assert len(parts) >= 3
            grouped.setdefault(parts[0], set()).add(parts[1])
    assert set(grouped) == {p.split('/')[1] for p in plan['courses']}
    assert len(grouped) == 39 and all(v == {'复习资料', 'exam'} for v in grouped.values())

def build_tree(files, git):
    root = {}
    for path, sha in files.items():
        node = root
        bits = path.split('/')
        for bit in bits[:-1]:
            node = node.setdefault(bit, {})
        assert bits[-1] not in node
        node[bits[-1]] = sha
    def descend(node):
        entries = []
        for name, value in node.items():
            is_dir = isinstance(value, dict)
            sha = descend(value) if is_dir else value
            entries.append((name.encode() + (b'/' if is_dir else b''), (b'040000 tree ' if is_dir else b'100644 blob ') + sha.encode() + b'\t' + name.encode() + b'\0'))
        return git('mktree', '-z', data=b''.join(v for _, v in sorted(entries))).decode().strip()
    return descend(root)

def main():
    assert os.environ['GITHUB_REPOSITORY'] == REPO
    seed = os.environ['SEED_SHA']
    token = os.environ['GH_TOKEN']
    plan_url = f'https://raw.githubusercontent.com/{REPO}/{seed}/.github/swu-import-plan.json.gz.b64'
    payload = urllib.request.urlopen(plan_url, timeout=90).read()
    assert hashlib.sha256(payload).hexdigest() == PLAN_DIGEST, 'Plan digest mismatch'
    plan = json.loads(zlib.decompress(base64.b64decode(payload)))
    assert plan['dest_repo'] == REPO
    work = Path(tempfile.mkdtemp(prefix='swu-import-'))
    bare = work / 'repo.git'
    env = os.environ.copy()
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'github-actions[bot]', 'GIT_AUTHOR_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com', 'GIT_COMMITTER_NAME': 'github-actions[bot]', 'GIT_COMMITTER_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com'})
    # The credential stays in the process environment; commands and logs contain no token.
    env['GIT_CONFIG_COUNT'] = '1'
    env['GIT_CONFIG_KEY_0'] = f'http.https://github.com/{REPO}.git.extraheader'
    env['GIT_CONFIG_VALUE_0'] = 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()
    def git(*args, data=None):
        r = subprocess.run(['git', '--git-dir=' + str(bare), *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if r.returncode:
            raise RuntimeError('git ' + args[0] + ' failed: ' + r.stderr.decode(errors='replace')[-3000:])
        return r.stdout
    subprocess.run(['git', 'init', '--bare', str(bare)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git('remote', 'add', 'origin', f'https://github.com/{REPO}.git')
    git('fetch', '--depth=1', 'origin', 'main')
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == seed, 'main changed; import paused to preserve user changes'
    def list_files(ref):
        out = {}
        for row in git('ls-tree', '-r', '-z', ref).split(b'\0'):
            if row:
                info, path = row.split(b'\t', 1)
                mode, kind, sha = info.split()
                assert mode == b'100644' and kind == b'blob'
                out[path.decode()] = sha.decode()
        return out
    original = list_files(seed)
    assert all(original[p] == s for p, s in plan['preserved'].items()), 'User-uploaded material changed'
    assert all(p in original for p in TEMP_FILES)
    current = dict(original)
    parent = seed
    staging = 'codex/swu-course-import-' + seed[:12]
    downloaded = set(original.values())

    def fetch_blob(entry):
        sha, size = entry['sha'], entry['size']
        url = f'https://raw.githubusercontent.com/{plan["source_repo"]}/{plan["source_commit"]}/' + urllib.parse.quote(entry['source'], safe='/')
        temp = work / (sha + '.download')
        for attempt in range(5):
            try:
                digest = hashlib.sha1(b'blob ' + str(size).encode() + b'\0')
                total = 0
                req = urllib.request.Request(url, headers={'User-Agent': 'Swu-Exam-Course-Importer'})
                with urllib.request.urlopen(req, timeout=90) as response, temp.open('wb') as out:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk); digest.update(chunk); total += len(chunk)
                assert total == size and digest.hexdigest() == sha, 'Downloaded file checksum mismatch: ' + entry['source']
                actual = git('hash-object', '-w', str(temp)).decode().strip()
                assert actual == sha
                temp.unlink()
                return sha
            except Exception:
                temp.unlink(missing_ok=True)
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    for semester in plan['semesters']:
        rows = [x for x in plan['copies'] if x['semester'] == semester]
        unique = {x['sha']: x for x in rows if x['sha'] not in downloaded}
        print(f'{semester}: downloading {len(unique)} unique verified files ({sum(x["size"] for x in unique.values()) / 1e6:.1f} MB)', flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for i, sha in enumerate(pool.map(fetch_blob, unique.values()), 1):
                downloaded.add(sha)
                if i % 100 == 0:
                    print(f'{semester}: verified {i}/{len(unique)} files', flush=True)
        current.update({x['target']: x['sha'] for x in rows})
        tree = build_tree(current, git)
        parent = git('commit-tree', tree, '-p', parent, data=f'Import SWU course files: {semester}\n\nSource {plan["source_commit"]}; original blobs verified by SHA-1.\n'.encode()).decode().strip()
        git('push', 'origin', parent + ':refs/heads/' + staging)
        print(f'{semester}: uploaded stage {parent}', flush=True)

    final = dict(plan['preserved'])
    final.update({p: original[p] for p in TEMP_FILES})
    final.update({x['target']: x['sha'] for x in plan['copies']})
    for path, text in plan['generated'].items():
        final[path] = git('hash-object', '-w', '--stdin', data=text.encode()).decode().strip()
    validate_structure(plan, final)
    final_tree = build_tree(final, git)
    final_commit = git('commit-tree', final_tree, '-p', parent, data=(
        'Organize 39 SWU computing courses into review materials and exam\n\n'
        f'Copy {len(plan["copies"])} content-verified files from {plan["source_commit"]}.\n'
        'Remove nested READMEs, preserve existing general-course uploads and academy directories, add course index and source links.\n').encode()).decode().strip()
    assert list_files(final_commit) == final
    remote = git('ls-remote', 'origin', 'refs/heads/main').decode().split()[0]
    assert remote == seed, 'main changed during upload; final push aborted to preserve concurrent edits'
    git('push', '--force-with-lease=refs/heads/main:' + seed, 'origin', final_commit + ':refs/heads/main')
    git('push', 'origin', ':refs/heads/' + staging)
    summary = {'final_commit': final_commit, 'tree': final_tree, 'copied_files': len(plan['copies']), 'copied_bytes': sum(x['size'] for x in plan['copies']), 'courses': len(plan['courses']), 'deleted_readmes': len(plan['deleted']), 'preserved_user_files': len(plan['preserved']), 'temporary_files_pending_cleanup': TEMP_FILES}
    print('IMPORT_RESULT=' + json.dumps(summary, ensure_ascii=False), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write('## Course migration verified\n\n```json\n' + json.dumps(summary, ensure_ascii=False, indent=2) + '\n```\n')

if __name__ == '__main__':
    main()
