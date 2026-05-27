---
name: planfix-real-world
description: Hard-won knowledge about PlanFix REST API from a production integration. Documents the quirks, broken endpoints, formatting gotchas, polling strategies, and battle-tested code patterns that don't appear in the official docs. Use this BEFORE you write any non-trivial PlanFix integration — it will save hours of trial and error. TRIGGER on any PlanFix-related task: API queries, custom fields, objects/templates, automations, status changes, RSVP/subtasks, comments, time/date handling, file uploads, webhooks, or when an existing PlanFix integration is misbehaving.
---

# PlanFix Real-World — Hard-Earned Lessons

This skill documents things you can ONLY learn by actually building a PlanFix integration. The official docs are incomplete and misleading. Read this **first** before writing any code.

## 📁 Map

| Section | What's inside |
|---|---|
| [Quirks & Gotchas](#quirks--gotchas) | Things that don't work as docs claim |
| [Working Endpoints](#working-endpoints-cheat-sheet) | What's actually callable vs 404 |
| [Date/Time](#datetime---input-vs-output) | The 4 different formats |
| [HTML Rendering](#html-rendering-comments--descriptions) | Markdown is dead, long live HTML |
| [User ID Format](#user-ids-the-usern-trap) | The `user:N` prefix trap |
| [Custom Fields](#custom-fields) | How to find IDs + set values |
| [Objects/Templates](#objectstemplates) | Read-only via REST |
| [Subtasks & RSVP](#subtasks--rsvp-patterns) | Patterns that work |
| [Polling Strategy](#polling-strategy) | Because webhook automation needs UI |
| [State Persistence](#state-persistence-in-task-description) | Hidden meta in description |
| [Cancellation Logic](#cancellation-logic) | Author-only access control |
| [Common Errors](#common-errors) | Diagnoses + fixes |
| [Snippets](#ready-to-use-snippets) | Copy-paste code |

---

## Quirks & Gotchas

### 1. `/task/list` returns OLDEST first, sort doesn't work
- `sort: [{"field": "id", "order": "desc"}]` is **silently ignored**
- All `sort` field names tried (`id`, `date`, `dateCreate`, `created`, `createdAt`) return same order
- **Newest tasks are at the END of pagination** (highest offset)
- To find latest tasks: scan from `offset = total - 100` upward, NOT from `offset = 0`

### 2. Filter by date modified (`type: 4`) returns nothing
Despite docs, `{"type": 4, "operator": "greaterOrEqual", "value": <unix_ts>}` consistently returns 0 results. **Don't use it.** Use full scan + hash-based change detection instead.

### 3. Filter by ID (`type: 102`) returns HTTP 500
Same for many "advanced" filter types. Stick to `type: 1` (name contains), `type: 6` (status), `type: 7` (assignee), `type: 8` (project), `type: 9` (priority).

### 4. `/task/{id}` without `fields` returns minimal object
```python
# Returns just {"id": 4708, "name": "..."}
pf.get("/task/4708")

# Always specify fields explicitly:
pf.get("/task/4708", params={"fields": "id,name,description,status,owner,assignees,participants,startDateTime,endDateTime,object,template,customFieldData"})
```

### 5. The `parent` filter for finding subtasks DOESN'T WORK
```python
# DOES NOT WORK — returns 0 results
pf.post("/task/list", {"filters": [{"type": "parent", "operator": "equal", "value": {"id": 4708}}]})
```
To find subtasks: scan all tasks with `fields=id,name,parent` and filter in Python by `t["parent"]["id"] == parent_id`.

### 6. Task list pagination caps at PAGE LIMIT (~2000 tasks visible)
If the account has 5000 tasks, your bot may only see ~2000 most-recent. Older tasks invisible. Don't rely on `/task/list` for historical data.

### 7. Tasks created via API may be INVISIBLE to other queries
If you create a task with `POST /task` but the API user (the token's user) isn't in `assignees`, `participants`, or `owner` of the task — the task **won't appear** in subsequent `/task/list` calls from that same token. Always include the bot user in some role.

### 8. PlanFix DELETE on `/task/{id}` returns 405
You cannot delete tasks via REST API. Best you can do: rename + close status.

---

## Working Endpoints Cheat Sheet

### ✅ Works
| Endpoint | Notes |
|---|---|
| `POST /task` | Create task. Returns `{result, id}`. |
| `POST /task/{id}` | Update task (description, status, customFieldData, etc.) |
| `GET /task/{id}?fields=...` | Get task — **specify fields explicitly** |
| `POST /task/list` | List with filters, pagination by offset |
| `POST /task/{id}/comments` | Add comment to task |
| `POST /task/{id}/comments/list` | Get comments — **specify fields** |
| `GET /object/{id}` | Get object/template details (with `customFieldData` of attached fields) |
| `POST /object/list` | List all objects |
| `POST /project/list` | List projects |
| `POST /contact/list` | List contacts |
| `POST /user/list` | **WORKS** — list users (employees too) |
| `GET /user/me`, `GET /user/current` | Current user (returns id=0 for system tokens!) |

### ❌ Doesn't exist (404 / 405)
- `/task/status/list`, `/processstatus/list`
- `/customField/list`, `/field/list`, `/customfield/list`
- `/task/template/list`, `/tasktemplate/list`
- `/automation/list`, `/scenario/list`, `/process/list`
- `/employee/list` (use `/user/list` instead)
- `/object` (POST — can't create new objects via API)
- `/object/{id}` (POST — can't modify object settings via API)
- `DELETE /task/{id}` (405)

**Anything UI-configurable (statuses, custom fields, templates, automations, buttons) is NOT accessible via REST.** Read existing — yes. Create/modify — only through admin UI.

---

## Date/Time — Input vs Output

PlanFix uses **FOUR different date formats** in the same API:

### Input (when creating/updating tasks)
```json
{
  "startDateTime": {"datetime": "23-05-2026 14:30"},  // DD-MM-YYYY HH:MM
  "endDateTime": {"datetime": "23-05-2026 15:00"}
}
```
**Format:** `DD-MM-YYYY HH:MM`. Not `YYYY-MM-DD`. Not Unix timestamp. Don't confuse with ISO.

### Output (when reading tasks)
```json
{
  "startDateTime": {
    "date": "23-05-2026",                                  // DD-MM-YYYY
    "time": "14:30",                                        // HH:MM
    "datetime": "2026-05-23T14:30Z",                        // ISO with Z (no offset)
    "dateTimeUtcSeconds": "2026-05-23T14:30:00+0000"       // ISO with +0000 (no colon!)
  }
}
```

### Python helper
```python
def extract_datetime(dt_value):
    if not dt_value:
        return None
    if isinstance(dt_value, dict):
        # Prefer dateTimeUtcSeconds, fallback to others
        return (
            dt_value.get("dateTimeUtcSeconds")
            or dt_value.get("datetime")
            or dt_value.get("date")
        )
    return dt_value

def to_iso_for_zoom(dt_str, tz="Asia/Bishkek"):
    """Convert PlanFix date to Zoom's expected ISO format"""
    from datetime import datetime
    import pytz
    # "2026-05-23T14:30:00+0000" — replace +0000 → +00:00
    if "+0000" in dt_str:
        dt_str = dt_str.replace("+0000", "+00:00")
    dt = datetime.fromisoformat(dt_str)
    local = dt.astimezone(pytz.timezone(tz))
    return local.strftime("%Y-%m-%dT%H:%M:%S")
```

### Filter date is Unix timestamp
For date filters in `/task/list`:
```json
{"type": 5, "operator": "greaterOrEqual", "value": 1779538585}
```
(Integer Unix timestamp, even though field returns object format.)

---

## HTML Rendering (Comments + Descriptions)

### Markdown is DEAD
```python
# ❌ DOES NOT render — saved as literal text
pf.add_comment(task_id, "**bold** *italic*")
# Result in UI: literal "**bold**"
```

### `\n` is STRIPPED
```python
# ❌ Newlines become spaces
pf.add_comment(task_id, "Line 1\nLine 2\nLine 3")
# Result: "Line 1 Line 2 Line 3"
```

### HTML is preserved AND rendered
```python
# ✅ Works perfectly
pf.add_comment(task_id, '<b>Title</b><br><br>Line 1<br>Line 2')
```

### Tested formatting elements

| Tag | Behavior |
|---|---|
| `<b>` / `<strong>` | ✅ Bold |
| `<i>` / `<em>` | ✅ Italic |
| `<br>` | ✅ Line break |
| `<p>` | ✅ Paragraph (renders with spacing, adds `\n` in source) |
| `<div>` | ✅ Block (adds `\n` in source) |
| `<a href="...">` | ✅ Clickable link (PlanFix auto-wraps raw URLs too) |
| `<ul>` / `<ol>` / `<li>` | ✅ Lists |
| `<hr>` | ✅ Horizontal rule |
| `<code>` | ✅ Monospace |
| `<!-- comment -->` | ❌ STRIPPED on save — don't use for hidden meta |

### Pattern: well-structured comment
```python
comment = (
    f'<p><b>🎥 Title</b></p>'
    f'<p>'
    f'<b>Field 1:</b> {value1}<br>'
    f'<b>Field 2:</b> {value2}'
    f'</p>'
    f'<p><b>List header:</b></p>'
    f'<ul>'
    f'<li>Item 1</li>'
    f'<li>Item 2</li>'
    f'</ul>'
    f'<hr>'
    f'<p><b>Section 2</b></p>'
    f'<p>...</p>'
)
```

---

## User IDs — the `user:N` Trap

### When reading
PlanFix returns user references as `"id": "user:33"`:
```json
{"owner": {"id": "user:33", "name": "Ivan"}}
```

### When writing (creating/updating)
You MUST also use the `user:N` string format, otherwise the API **silently falls back** to assigning the API token's user:

```python
# ❌ WRONG — bot's user ends up as assignee
pf.post("/task", {"assignees": {"users": [{"id": 33}]}})

# ✅ CORRECT
pf.post("/task", {"assignees": {"users": [{"id": "user:33"}]}})
```

### Helper to normalize
```python
def parse_user_id(user_obj: dict) -> int | None:
    """Read PlanFix user ID into plain integer"""
    raw = (user_obj or {}).get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and ":" in raw:
        try:
            return int(raw.split(":")[-1])
        except ValueError:
            return None
    return int(raw) if raw else None

def to_pf_user_id(uid) -> str:
    """Convert integer ID to user:N string format"""
    if isinstance(uid, int):
        return f"user:{uid}"
    s = str(uid)
    return s if s.startswith("user:") else f"user:{s}"
```

---

## Custom Fields

### Finding IDs (no `/customField/list` endpoint!)
**The only way** to discover custom field IDs is via the `customFieldData` array on an OBJECT (template) that has them attached:

```python
r = requests.get(f"{BASE}/object/{template_id}", headers=H)
for f in r.json()["object"].get("customFieldData", []):
    fi = f["field"]
    print(f"ID={fi['id']}  name='{fi['name']}'  type={fi['type']}")
# Output:
#   ID=130225  name='Zoom Ссылка'  type=2
#   ID=130227  name='Zoom Meeting ID'  type=2
```

You can also pull from any TASK that uses these fields with `fields=customFieldData`.

### Setting values
```python
pf.post(f"/task/{task_id}", {
    "customFieldData": [
        {"field": {"id": 130225}, "value": "https://us06web.zoom.us/j/..."},
        {"field": {"id": 130227}, "value": "12345678901"},
    ]
})
```

### Type cheat sheet
| `type` | Meaning | Value format |
|---|---|---|
| 1 | Number | integer |
| 2 | String / URL | string |
| 4 | Date | `"YYYY-MM-DD"` |
| 5 | Money | `{"value": 5000, "currency": "RUB"}` |
| 7 | User reference | `{"id": "user:N"}` |
| 8 | Directory entry | `{"id": N}` |
| ... | (more, but rarely needed) |

---

## Objects/Templates

In PlanFix UI: **"Объекты"** in admin → templates for new tasks.

### Discovery
```python
# All objects
pf.post("/object/list", {"offset": 0, "pageSize": 50, "fields": "id,name,description"})

# Specific object with full config
pf.get(f"/object/{id}")  # Returns settings, statuses, custom fields, default participants
```

### Two different IDs!
- The "objectId" shown in the **PlanFix admin URL** (e.g., `objectId=5647693`) is the **UI-internal ID**.
- The **API ID** is different (e.g., `4703`) and is what you use everywhere in REST.

To find the API ID, list all objects and match by name.

### Creating tasks with an object
```python
pf.post("/task", {
    "name": "ZOOM: Meeting",
    "object": {"id": 4637},       # Both fields work — set both for safety
    "template": {"id": 4637},
    # ...
})
```

### You CANNOT modify objects via API
- `POST /object` → 404
- `POST /object/{id}` → 405

All object configuration (Карточка, Набор статусов, Автоматические сценарии, Кнопки, Данные) must be done in admin UI.

---

## Subtasks & RSVP Patterns

### Creating a subtask correctly
```python
pf.post("/task", {
    "name": "[RSVP] Иван: будет на встрече?",
    "parent": {"id": parent_task_id},
    "description": "<p>...</p>",        # HTML!
    "assignees": {"users": [{"id": "user:33"}]},  # user:N format!
    "object": {"id": rsvp_template_id},    # for buttons from object config
    "template": {"id": rsvp_template_id},
})
```

### Common pitfall: subtask assigned to bot, not to participant
**Cause:** Using `{"id": 33}` instead of `{"id": "user:33"}` → PlanFix silently uses the API token's user (the bot).
**Fix:** Always use `user:N` string format.

### Finding subtasks of a task (the parent filter is broken)
```python
# Doesn't work
pf.post("/task/list", {"filters": [{"type": "parent", "value": {"id": parent_id}}]})

# Workaround: scan tail of all tasks, filter in Python
all_recent = []
for offset in range(start_offset, total + 100, 100):
    r = pf.post("/task/list", {"offset": offset, "pageSize": 100,
                                "fields": "id,name,parent,status,assignees"})
    items = r.get("tasks", [])
    if not items:
        break
    all_recent.extend(items)

subtasks = [t for t in all_recent if (t.get("parent") or {}).get("id") == parent_id]
```

### RSVP pattern that actually works
1. Store `rsvp_users: [uid1, uid2]` in parent task's **meta** (hidden marker in description).
2. On every poll, if user not in `rsvp_users` → create RSVP subtask → add to list.
3. This is more reliable than querying subtasks (which has the parent filter bug).

```python
meta = extract_meta(task["description"])
rsvp_users = set(meta.get("rsvp_users", []))

for p in participants:
    uid = parse_user_id(p)
    if uid in rsvp_users:
        continue  # already has RSVP
    pf.post("/task", {
        "parent": {"id": task_id},
        "name": f"[RSVP] {p['name']}",
        "assignees": {"users": [{"id": f"user:{uid}"}]},
        "object": {"id": rsvp_template_id},
        # ...
    })
    rsvp_users.add(uid)

meta["rsvp_users"] = list(rsvp_users)
pf.post(f"/task/{task_id}", {"description": inject_meta(task["description"], meta)})
```

### Detecting RSVP button clicks
PlanFix "Кнопки" (object buttons) trigger a status change. To detect:

```python
# Poll RSVP subtasks every N seconds
# Cache the last-known status_id per subtask
# When it changes → handle the response

self._rsvp_status_cache: dict[int, int] = {}  # {subtask_id: status_id}

for st in rsvp_subtasks:
    sid = st["id"]
    new_id = (st.get("status") or {}).get("id")
    status_name = (st.get("status") or {}).get("name", "").lower()
    old = self._rsvp_status_cache.get(sid)

    # Detect by NAME not just ID — works with custom statuses
    is_yes = any(kw in status_name for kw in ["буду", "приму", "соглас", "yes", "да"])
    is_no = any(kw in status_name for kw in ["не буду", "откаж", "no", "нет"])

    if old != new_id:
        # Status changed — handle
        ...
        self._rsvp_status_cache[sid] = new_id
```

---

## Participants — `participants` ≠ `assignees`

This is the **#1 source of bugs** for RSVP-style integrations:

| PlanFix field | UI label | Purpose |
|---|---|---|
| `owner` | Постановщик | Who set the task |
| `assignees` | Исполнители | Who must execute |
| `participants` | Участники | Additional people involved |
| `auditors` | Аудиторы | Read-only observers |

For "people who attend a meeting", PlanFix uses **`participants.users`**, NOT `assignees.users`.

If your code reads `assignees` for an attendee list, RSVPs go to the wrong people.

```python
def get_meeting_attendees(task: dict) -> list:
    """The CORRECT way to get meeting attendees"""
    # Primary: participants field
    result = list(task.get("participants", {}).get("users", []))
    # Fallback for tasks without participants set
    if not result:
        result = list(task.get("assignees", {}).get("users", []))
    return result
```

---

## Polling Strategy

**Why polling and not webhooks?** PlanFix automations (HTTP-request triggers) must be configured **in the admin UI** — there's no API to create them. So if you can't or won't set up automations:

### Recipe
```python
class Poller:
    def __init__(self):
        self._task_state_cache: dict = {}     # task_id → state_hash
        self._created_meetings: dict = {}     # task_id → meeting_id (in-memory dedup)
        self._task_first_seen: dict = {}      # task_id → unix_ts
        self._initialized = False

    async def tick(self):
        if not self._initialized:
            # First run: scan all tasks, cache state, don't trigger anything
            await self._initial_scan()
            self._initialized = True
            return

        # Scan TAIL of task list (where newest are)
        tasks = self._fetch_tail_tasks(pages=3)

        # Detect what's changed via hash
        changed = []
        for t in tasks:
            new_hash = self._compute_state_hash(t)
            if self._task_state_cache.get(t["id"]) != new_hash:
                changed.append(t)
                self._task_state_cache[t["id"]] = new_hash

        for t in changed:
            await self._process(t)
```

### State hash (detect changes)
```python
import hashlib, json

def state_hash(task: dict) -> str:
    key = json.dumps({
        "name": task.get("name"),
        "description": task.get("description") or "",
        "status_id": (task.get("status") or {}).get("id"),
        "start": task.get("startDateTime"),
        "end": task.get("endDateTime"),
        "assignees": task.get("assignees"),
        "participants": task.get("participants"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(key.encode()).hexdigest()
```

### Smart "is task ready" check
Don't create the meeting on first sight — task might still be filling out:

```python
participants = get_meeting_attendees(task)
has_start = bool(extract_datetime(task.get("startDateTime")))

if not participants or not has_start:
    # Just remember we've seen it; don't act yet
    return

# OK to process
```

### Deduplication
Even with hashing, you'll occasionally double-process. Belt + suspenders:

```python
# 1. In-memory cache (resets on server restart)
if task_id in self._created_meetings:
    # We already created one this session → just update, never create new
    ...

# 2. Persistent state in task description (survives restarts)
meta = extract_meta(task["description"])
if meta and meta.get("meeting_id"):
    # Even if cache is empty, we know from description
    ...
```

---

## State Persistence in Task Description

Since PlanFix has no place for "service data" on a task, store it **in the description as a hidden marker**.

### What WORKS
```python
META_BEGIN = "[ZOOM_META_v1]"
META_END   = "[/ZOOM_META_v1]"
META_RE    = re.compile(r"\[ZOOM_META_v1\]([^\[]+)\[/ZOOM_META_v1\]", re.DOTALL)

import base64, json

def inject_meta(description: str, meta: dict) -> str:
    b64 = base64.b64encode(
        json.dumps(meta, ensure_ascii=False).encode()
    ).decode()
    marker = f"{META_BEGIN}{b64}{META_END}"
    if META_BEGIN in (description or ""):
        return META_RE.sub(marker, description)
    return f"{description or ''}\n\n{marker}".strip()

def extract_meta(description: str) -> dict | None:
    if not description or META_BEGIN not in description:
        return None
    m = META_RE.search(description)
    if not m:
        return None
    try:
        return json.loads(base64.b64decode(m.group(1).strip()).decode())
    except Exception:
        return None
```

### What does NOT work
- `<!-- HTML comment -->` — PlanFix **strips** these on save
- `<span style="display:none">` — kept as literal text, but visible in raw HTML view; users see weird artifacts
- Plain newlines as delimiters — newlines are also stripped

### Pattern: meta + visible content
```python
desc = "Some original description by the user."
meta = {"meeting_id": "12345", "rsvp_users": [33, 25]}
new_desc = inject_meta(desc, meta)
pf.post(f"/task/{task_id}", {"description": new_desc})
# In UI: user sees their original text, then a base64 blob.
# Encourage users to NOT touch the [ZOOM_META_v1]...[/...] block.
```

---

## Cancellation Logic (Author-Only)

PlanFix doesn't have per-field access control suitable for "only initiator can cancel". Solution: **detect via authored COMMENTS** (which include `owner.id`).

```python
async def check_authorized_cancel(task_id: int, meta: dict) -> str:
    """Return 'authorized', 'unauthorized', or 'none'"""
    initiator_id = meta.get("initiator_id")
    if not initiator_id:
        return "none"

    r = pf.post(f"/task/{task_id}/comments/list",
                {"offset": 0, "pageSize": 20,
                 "fields": "id,description,owner,dateTime"})

    for c in r.get("comments", []):
        text = (c.get("description") or "")
        clean = re.sub(r"<[^>]+>", "", text)  # strip HTML
        if not re.search(r"^\s*/cancel\b|^\s*\[ОТМЕНА\]", clean, re.IGNORECASE | re.MULTILINE):
            continue

        # Compare comment author with initiator
        comment_author_id = parse_user_id(c.get("owner") or {})
        if comment_author_id == initiator_id:
            return "authorized"
        else:
            # Reject + notify
            pf.add_comment(task_id, "<p><b>⚠️ Отмена отклонена</b></p>...")
            return "unauthorized"

    return "none"
```

Why this works: PlanFix records the comment author reliably in `comment.owner.id`. Anyone can write `/cancel` but we only honor it from the initiator.

---

## Common Errors

| Error | Meaning | Fix |
|---|---|---|
| `HTTP 400 "Cannot deserialize, invalid JSON"` | Body format wrong | Check field names; use `user:N` format for assignees |
| `HTTP 404` on a documented endpoint | Endpoint doesn't actually exist in v2 | Look for alternative (e.g., `/user/list` instead of `/employee/list`) |
| `HTTP 405` | Method not allowed (DELETE, POST on read-only) | Action not supported via API |
| `HTTP 500 "Rest API error"` | Filter type unsupported | Try a different filter type |
| `HTTP 504` | Gateway timeout — PlanFix overloaded | Add retry with backoff; reduce parallel requests |
| Returns empty results | Visibility filter (your bot user doesn't see them) | Add bot to `participants` or `auditors` of those tasks |
| `assignees` end up as bot user | `user:N` format not used | Use `{"id": f"user:{uid}"}` not `{"id": uid}` |
| Description loses `\n` | Newline → space | Use `<br>` or `<p>` HTML |
| Markdown not rendering | PlanFix doesn't process MD | Use HTML tags |
| `<!-- comment -->` lost | PlanFix sanitizes HTML comments | Use plain text marker like `[META]...[/META]` |

---

## Ready-to-Use Snippets

### Throttled client with retries
```python
import time, requests

class PlanfixClient:
    def __init__(self, account: str, token: str):
        self.base = f"https://{account}.planfix.com/rest"
        self.h = {"Authorization": f"Bearer {token}",
                  "Content-Type": "application/json"}
        self._last = 0

    def _throttle(self):
        wait = 1.05 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def post(self, path, body=None, silent=False):
        self._throttle()
        url = f"{self.base}{path}" + ("?silent=true" if silent else "")
        r = requests.post(url, headers=self.h, json=body or {})
        r.raise_for_status()
        return r.json()

    def get(self, path, params=None):
        self._throttle()
        r = requests.get(f"{self.base}{path}", headers=self.h, params=params)
        r.raise_for_status()
        return r.json()
```

### Find users by name
```python
def find_user_by_name(pf, name_substring: str) -> dict | None:
    r = pf.post("/user/list", {"offset": 0, "pageSize": 100,
                                "fields": "id,name,login,email,role"})
    for u in r.get("users", []):
        if name_substring.lower() in u.get("name", "").lower():
            return u
    return None
```

### Create task with proper format (full example)
```python
from datetime import datetime, timedelta

start = datetime.now() + timedelta(hours=2)
end = start + timedelta(minutes=30)

task = pf.post("/task", {
    "name": "ZOOM: Weekly sync",
    "description": "<p>Recurring team sync</p>",
    "object": {"id": ZOOM_OBJECT_ID},      # template
    "template": {"id": ZOOM_OBJECT_ID},
    "startDateTime": {"datetime": start.strftime("%d-%m-%Y %H:%M")},
    "endDateTime": {"datetime": end.strftime("%d-%m-%Y %H:%M")},
    "assignees": {"users": [{"id": "user:33"}]},   # Author/executor
    "participants": {"users": [
        {"id": "user:25"}, {"id": "user:27"}       # Attendees
    ]},
    "customFieldData": [
        {"field": {"id": 130225}, "value": "https://us06web.zoom.us/j/..."},
    ],
})
print(f"Created task {task['id']}")
```

### Status diagnostic
```python
def diagnose(account: str, token: str):
    base = f"https://{account}.planfix.com/rest"
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for name, path, method, body in [
        ("Tasks",     "/task/list",     "POST", {"offset":0,"pageSize":1}),
        ("Objects",   "/object/list",   "POST", {"offset":0,"pageSize":1}),
        ("Users",     "/user/list",     "POST", {"offset":0,"pageSize":1}),
        ("Projects",  "/project/list",  "POST", {"offset":0,"pageSize":1}),
        ("Contacts",  "/contact/list",  "POST", {"offset":0,"pageSize":1}),
    ]:
        r = requests.post(f"{base}{path}", headers=h, json=body)
        icon = "✅" if r.status_code == 200 else "❌"
        print(f"  {icon} {name:<10} HTTP {r.status_code}")
```

---

## Decision Tree: "I need to..."

```
... read existing data            → REST API works, just specify fields
... create a new task             → POST /task (use user:N format!)
... update task fields            → POST /task/{id}
... add comment                   → POST /task/{id}/comments (HTML!)
... add custom field value        → POST /task/{id} with customFieldData
... find custom field IDs         → GET /object/{template_id} (look in customFieldData)
... create new custom field       → ❌ admin UI only
... create new task template      → ❌ admin UI only
... configure object settings     → ❌ admin UI only
... create automation             → ❌ admin UI only
... configure buttons             → ❌ admin UI only
... delete a task                 → ❌ not supported; rename + close instead
... react to task creation        → ✅ polling (or have user set up automation manually)
... react to status change        → ✅ polling with status_id cache
... store integration state       → base64 meta in description
... detect cancellation by author → /cancel comment + check comment.owner
... assign to user                → {"id": "user:N"} STRING format
... reference participants        → task.participants.users (not assignees!)
... format text                   → HTML (<b>, <br>, <p>, <ul>, <a>, <hr>)
... preserve newlines             → use <br> or <p>, not \n
```

---

## Companion Files

- `scripts/planfix_client.py` — Production-ready throttled REST client
- `scripts/poller_template.py` — Polling architecture template (state hash, dedup, lock)
- `scripts/html_helpers.py` — HTML formatting helpers for PlanFix
- `scripts/meta_storage.py` — Hidden meta in task description (base64)
