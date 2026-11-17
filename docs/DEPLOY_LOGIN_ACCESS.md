# Deploying per-user login and the access list (production server)

For the agent on the staff server (runs `main`). Follow in order. Background: `CLAUDE.md` (Auth line) and
`src/jutetransfer/access.py`.

**What changes for staff:** the shared logins `admin` / `user` stop working. Everyone logs in with their own
VoWERP e-mail and password, and only people on the access list get in. Only a transfer admin can delete a
chain step, un-finalize, or undo a marked move.

## Before you start

- The change must already be merged to `main` (`claudehost` → `stock-transfer` → `main`).
- The table `jt_user_access` **already exists** on sls (created 2026-11-17, same database as the dev VM).
  Do not create it again and do not run any other migration or repair script.
- Tell the owner when you are about to restart, because staff cannot log in until they are on the access list
  (step 6).

## Steps

1. **Get the code.** `git pull` on `main`. Confirm `src/jutetransfer/access.py` and
   `src/jutetransfer/pages/users.py` exist.

2. **Install the new dependency.** `uv sync` (adds `bcrypt`).

3. **Edit `.env`** (do not print or commit it). Add:

   ```
   JT_ADMIN_EMAILS=sidheshsarda@slsenterprise.com
   ```

   Make sure `JT_LEGACY_LOGIN` is absent or `0`. Without `JT_ADMIN_EMAILS` nobody new can be given access.

4. **Check, read-only.**

   ```
   uv run pytest tests/ -q
   uv run python -m scripts.migrate_jt_user_access
   ```

   The tests must pass. The second command is a dry run and must print `Table exists already: True`.
   Do **not** add `--apply`.

5. **Restart the app** the way it is normally run on this server (the `.env` is only read at start).

6. **Verify in the browser.**
   - `admin` / `admin123` is refused.
   - The login screen shows no demo-credentials box.
   - Ask the owner to log in with the admin e-mail. The sidebar shows "(admin)" and a **Users** page.
   - The Users page lists Manoj Thapa as admin.
   - The owner (or Manoj) adds each staff member on the Users page as `user` or `admin`. Until then that
     person sees "you do not have access to the transfer app yet".

7. **Report back** to the owner: commit deployed, test result, and who is on the access list.

## Also in this release: MR date order rule

No setup is needed. A hop, a finalize or a marked move is now refused when its MR date is before the latest
MR already numbered at that branch in the financial year, or after today. An admin can save it anyway by
typing a reason under "Date override (admin)"; that is recorded in the table `jt_change_log`. Check the
table exists with the dry run `uv run python -m scripts.migrate_jt_change_log` only if the owner asks; do
not `--apply` without the owner's OK.

## If something goes wrong

- **Nobody can log in:** check `JT_ADMIN_EMAILS` in `.env` and that the app was restarted.
- **Staff must work before the list is ready:** set `JT_LEGACY_LOGIN=1` in `.env` and restart. The old shared
  logins work again and the admin-only check is switched off. Tell the owner, and remove it afterwards.
- Do not delete or edit rows in `jt_user_access` by hand; use the Users page.
