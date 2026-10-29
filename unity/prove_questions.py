"""Prove-only, persistent wakeups for explicitly addressed Forum questions."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile


class QuestionWakeups:
    # Limit pending memory and leave the cursor before any unscanned overflow.
    MAX_PENDING = 256

    def __init__(self, forum: Path):
        self.forum = Path(forum)
        self.path = self.forum / 'prove-question-wakeups.json'
        try:
            data = json.loads(self.path.read_text())
            self.cursor = int(data['cursor'])
            self.anchor = data['anchor']
            self.pending = data['pending']
            if self.cursor < 0 or not isinstance(self.pending, list):
                raise ValueError('invalid cursor')
        except FileNotFoundError:
            self.cursor, self.anchor, self.pending = 0, None, []
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'invalid Prove question wakeup state: {self.path}') from exc
        self.signature = None
        self.posts = []
        self.answered = set()

    def _save(self):
        self.forum.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.prove-question-wakeups-', dir=self.forum)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump({'cursor': self.cursor, 'anchor': self.anchor,
                           'pending': self.pending}, handle)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _read(self):
        path = self.forum / 'qa.json'
        try:
            # The Forum writes qa.json under qa.lock, but not atomically.
            with (self.forum / 'qa.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                    if signature == self.signature:
                        return True
                    data = json.loads(path.read_text())
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            if not isinstance(data, dict):
                return False
            posts = data.get('posts')
            if not isinstance(posts, list):
                return False
            if self.cursor > len(posts) or (self.cursor and
                    (not isinstance(posts[self.cursor - 1], dict) or
                     posts[self.cursor - 1].get('post_id') != self.anchor)):
                # Do not replay questions if the underlying thread was replaced.
                return False
            self.posts, self.signature = posts, signature
            self.answered = {post.get('fields', {}).get('question_id') for post in posts
                             if isinstance(post, dict) and post.get('act') == 'answer'
                             and isinstance(post.get('fields'), dict)
                             and isinstance(post['fields'].get('question_id'), str)}
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _recipient(value, names):
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value in names:
            return value
        matching = [name for name in names if name.casefold() == value.casefold()]
        return matching[0] if len(matching) == 1 else None

    @staticmethod
    def _target(fields, declarations, worker_target):
        explicit = [fields.get(key) for key in ('chunk', 'declaration') if fields.get(key)]
        if any(not isinstance(value, str) for value in explicit):
            return None
        if explicit:
            resolved = []
            for value in explicit:
                matches = [name for name, row in declarations.items()
                           if value == name or value == row.get('chunk_id')]
                if len(matches) != 1:
                    return None
                resolved.append(matches[0])
            if len(set(resolved)) != 1:
                return None
            target = resolved[0]
        elif worker_target in declarations:
            target = worker_target
        else:
            unresolved = [name for name, row in declarations.items() if row.get('status') == 'unresolved']
            target = unresolved[0] if len(unresolved) == 1 else None
        return target if target and declarations[target].get('status') == 'unresolved' else None

    def poll(self, state: dict, agents: dict, tasks: dict, worker_decls: dict) -> list[tuple[str, str, str, str]]:
        """Return once-only (recipient, target, question id, author) wakeups for idle agents."""
        if not self._read():
            return []
        before = (self.cursor, self.anchor, json.dumps(self.pending, sort_keys=True))
        declarations = state.get('declarations', {})
        # Scan only newly appended posts. Existing pending questions are reconsidered below.
        while self.cursor < len(self.posts):
            post = self.posts[self.cursor]
            if not isinstance(post, dict):
                break
            if post.get('act') == 'question' and len(self.pending) >= self.MAX_PENDING:
                break
            if post.get('act') == 'question' and isinstance(post.get('post_id'), str):
                self.pending.append({'index': self.cursor, 'id': post['post_id']})
            self.cursor += 1
            self.anchor = post.get('post_id')
        pending, wakeups, scheduled = [], [], set()
        for item in self.pending:
            try:
                post = self.posts[item['index']]
                if post.get('post_id') != item['id'] or post.get('act') != 'question':
                    continue
                fields = post.get('fields') or {}
                if not isinstance(fields, dict) or fields.get('status') != 'open' or item['id'] in self.answered:
                    continue
                recipient = self._recipient(fields.get('to'), agents)
                author = post.get('author')
                if not recipient or not isinstance(author, str) or not author.strip() or author.strip().casefold() == recipient.casefold():
                    continue
                target = self._target(fields, declarations, worker_decls.get(recipient))
                if not target:
                    continue
                if recipient in tasks or recipient in scheduled:
                    pending.append(item)
                    continue
                wakeups.append((recipient, target, item['id'], author))
                scheduled.add(recipient)
            except (IndexError, KeyError, TypeError):
                continue
        self.pending = pending
        if before != (self.cursor, self.anchor, json.dumps(self.pending, sort_keys=True)):
            self._save()
        return wakeups
