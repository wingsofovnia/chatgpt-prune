# chatgpt-chats-bulk-remove

Bulk delete ChatGPT chats from the command line. One Python file. No dependencies.

**Delete is permanent.** It is the same soft delete as the ChatGPT UI (`is_visible: false`). You cannot restore a deleted chat. Run with `--dry-run` first.

## Requirements

- [uv](https://docs.astral.sh/uv/), or Python 3.11+.

## Token

1. Open chatgpt.com and log in.
2. In the DevTools console, run:
   ```js
   copy((await (await fetch('/api/auth/session')).json()).accessToken)
   ```
   This copies the token to the clipboard. Paste it with Cmd+V (Ctrl+V on Linux and Windows) or `pbpaste`. The console prints `undefined`. That is normal.
3. Export the token (plain JWT, no `Bearer `):
   ```sh
   export CHATGPT_ACCESS_TOKEN="$(pbpaste)"
   ```

`--authorization` wins over `CHATGPT_ACCESS_TOKEN`. The script does not read `OPENAI_API_KEY`: that name usually holds an OpenAI platform key (`sk-...`), which does not work here. Prefer the env var: `ps` and shell history show command-line args.

The token gives full access to your account. Do not share it. The server can reject a token before its `exp` time. If you get HTTP 401, get a new token.

## Usage

```sh
# List what matches. Delete nothing. Add `> chats.txt` to save the list.
./chatgpt_chats_bulk_remove.py --update-time-to=2026-06-30 --exclude-pinned --dry-run

# Delete. The script lists the chats and asks you to type a confirmation word.
./chatgpt_chats_bulk_remove.py --update-time-to=2026-06-30 --exclude-pinned

# Delete one project's chats. Ask for each chat.
./chatgpt_chats_bulk_remove.py --project-name="Work" --interactive
```

Run `./chatgpt_chats_bulk_remove.py --help` for all flags.

## Behavior

- The script reads the main list, the archive, and each project. The main list does not include all project chats.
- Dates are ISO (`2026-10-29` or `2026-10-29T09:03:50.719809Z`). No zone means UTC. A plain date in a `-to` flag includes that whole day.
- `--title-contains` and `--project-name` can repeat. A chat matches if any value matches.
- With `--exclude-project-name`, the script keeps chats of projects it cannot name (for example, shared projects). It reads names of your own projects only.
- `--limit` keeps the most recently updated chats.
- Requests run one at a time. Defaults avoid rate limits: 1s between requests, 3 retries on 429/5xx. Retries honor `Retry-After`.
- Errors print one line. Add `--verbose` to get the traceback.
- The delete step prints a summary at the end, also after an error or Ctrl-C.
- Exit code: 0 = OK, 1 = some deletes failed or you aborted, 2 = auth, API, network or usage error.

## Tests

```sh
python3 -m unittest -v
```

The tests use a fake API. They make no network calls.

## API

Not official. The format can change without notice.

| Action | Request |
|---|---|
| List chats | `GET /backend-api/conversations?offset=N&limit=28&order=updated&is_archived=false\|true` |
| List projects | `GET /backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=0&cursor=…` |
| List project chats | `GET /backend-api/gizmos/{g-p-…}/conversations?cursor=…` |
| Delete chat | `PATCH /backend-api/conversation/{id}` with `{"is_visible": false}` |

All requests need a browser `User-Agent`. Without it, Cloudflare returns HTTP 403.
