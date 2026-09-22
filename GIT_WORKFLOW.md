# Saving Changes to GitHub

Normally, when Claude edits a file in this project, it also stages,
commits, and pushes that change to GitHub for you — you don't need to do
anything. This doc is for the one case where that doesn't happen: Claude's
bridge to your computer can only write files, not run commands, if its
connection to your machine drops (which has happened before, e.g. after a
Windows update). When that happens, Claude will tell you the files are
updated but not yet committed, and walk you through pushing them yourself.
These are those steps, kept here so you don't have to ask again next time.

## The steps

**1. Open a terminal.** PowerShell is easiest — right-click the Start
button and choose "Windows PowerShell" or "Terminal." Command Prompt
works too, the commands below are identical either way.

If you already have a terminal open running the daemon, open a second
one for this — don't close the daemon's window.

**2. Go to the project's root folder** — the top-level `Options Scanner`
folder, not the `scanner` subfolder inside it:
```
cd "C:\Users\jruzz\Documents\Me\Options Scanner"
```
Keep the quotes — the folder name has a space in it.

**3. See what's changed:**
```
git status
```
This lists every file that's different from what's already on GitHub.
Claude will normally have told you which files to expect here. If you see
something unexpected, or an error like "not a git repository," stop and
check with Claude before continuing.

**4. Stage the changed files.** List only the files `git status` actually
showed as modified — if one of the files Claude mentioned doesn't show
up, that just means its content already matches what's on GitHub, nothing
to do there:
```
git add <file1> <file2> ...
```
For example:
```
git add scanner/combined_scanner.py GETTING_STARTED.md
```

**5. Commit them**, with a short message describing what changed:
```
git commit -m "Add csv+xlsx output to credit/debit/LEAPS scans"
```
You should see a line like `1 file changed` for each file committed.

**6. Push to GitHub:**
```
git push origin main
```
This shouldn't ask for a username or password — the access token is
already saved in this repo's local config.

**7. Confirm it landed:**
```
git log --oneline -3
```
Your new commit should be at the top. You can also refresh
`github.com/JRRenegade/Option-Scanner` in a browser and see the updated
timestamp.

## If something goes wrong

- **`git push` asks for a username/password.** That usually means the
  saved access token has expired or was revoked. Tell Claude — it'll walk
  you through generating a new one.
- **`git status` shows files you didn't expect, or none at all.** Paste
  what it shows to Claude before running `git add` — better to check
  first than to commit the wrong thing.
- **Any other error message.** Paste it to Claude exactly as shown rather
  than trying to work around it — most git errors are more informative
  than they look, and the exact wording matters.
