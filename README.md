# ChatGPT Chat Bulk Remover

Delete ChatGPT chats in bulk from the command line. Filter by date, project, title, pinned and archived state. One Python file, no dependencies.

**Delete is permanent.** You cannot restore a deleted chat. Run with `--dry-run` first.

## Prerequisites

- [uv](https://docs.astral.sh/uv/), or Python 3.11+.

## Access Token

1. Open chatgpt.com and log in.
2. In the DevTools console, run this. It copies the token to the clipboard. The console prints `undefined`. That is normal.
   ```js
   copy((await (await fetch('/api/auth/session')).json()).accessToken)
   ```
3. Pass the token to the script:
   ```sh
   ./chatgpt_chats_bulk_remove.py --authorization "$(pbpaste)" --dry-run
   ```

As an alternative, set the token once per shell:

```sh
export CHATGPT_ACCESS_TOKEN="$(pbpaste)"
```

The token gives full access to your account. Do not share it. If you get HTTP 401, get a new token.

## Usage

```sh
# List chats last updated before July. Delete nothing.
./chatgpt_chats_bulk_remove.py --update-time-to=2026-06-30 --exclude-pinned --dry-run

# Delete them. The script lists the chats and asks you to type a confirmation word.
./chatgpt_chats_bulk_remove.py --update-time-to=2026-06-30 --exclude-pinned

# Delete chats of one project. Ask for each chat.
./chatgpt_chats_bulk_remove.py --project-name="Work" --interactive
```

Run `--help` for all flags.

The script works in 3 stages:

1. **Select** what to delete: `--create-time-*`, `--update-time-*`, `--project-name`, `--title-contains`. Chats outside the selection are ignored: not deleted, not listed.
2. **Filter** what to protect: the `--exclude-*` flags. The script lists excluded chats with the reason before the confirmation.
3. **Delete.** The chats that pass both stages, after you confirm.

Important behavior:

- Dates are ISO (`2026-10-29` or `2026-10-29T09:03:50Z`). No zone means UTC. A plain date in a `-to` flag includes that whole day.
- `--limit` caps the chats to delete, most recently updated first. The script reads only the pages it needs. `--update-time-from` stops reading at the first older chat.
- With `--exclude-project-name`, chats in projects you do not own (for example, shared projects) are never deleted. The script cannot read those project names, so it cannot check them against the exclude list.
- Requests run one at a time, 1s apart. The script retries on 429 and 5xx.
- Exit code: 0 = OK, 1 = some deletes failed or you aborted, 2 = auth, API, network or usage error.

## Tests

```sh
python3 -m unittest -v
```

The tests use a fake API. They make no network calls.

## ChatGPT API

The script uses these endpoints under `https://chatgpt.com/backend-api`. They are not official and can change.

- `GET conversations?offset=N&limit=28&order=updated&is_archived=false|true`: list chats.
- `GET gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=0`: list projects.
- `GET gizmos/{project_id}/conversations?cursor=…`: list the chats of a project. The main list does not include all of them.
- `PATCH conversation/{id}` with `{"is_visible": false}`: delete a chat. This is the same call as the ChatGPT UI.

All requests need a browser `User-Agent`. Without it, Cloudflare returns HTTP 403.

## License

[MIT](LICENSE)
