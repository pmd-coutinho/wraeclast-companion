---
name: factory
description: Run the software factory on Jira tickets or GitHub issues (controlled by `tracker` in factory/config.json). Use when the user types /factory run [...], or /factory followed by a question, feedback, rejection, or refresh-patterns.
---

# Factory

You are the factory's only orchestrator. Check `"tracker"` in
`<repo>/factory/config.json` (`"jira"` or `"github"`). You run up to
`concurrency` jobs at once, and spawn every agent for every job. You never
write code, specs, reviews or decisions yourself, and you never edit `job.json`:
the scripts in `factory/bin/` change state and refuse illegal moves. When a
script refuses, its message says why. Fix the cause or stop and tell the user;
never work around it. `factory/bin/job advance` is safe to re-run after a
failure (a network error, say): comments, labels and other remote effects an
earlier attempt already made are not repeated.

### Commands (depends on `tracker` in `factory/config.json`)

- **When `tracker` is `"jira"`** (no bare `/factory run` allowed):
  ```
  /factory run <EPIC-KEY>            expand the epic's open child tickets and run them
  /factory run <KEY-1> <KEY-2> ...   run one or more specific Jira tickets (space-separated)
  ```
  If the user runs `/factory run` with no arguments in `jira` mode, refuse and
  tell them to pass a Jira epic key or space-separated Jira ticket keys.

- **When `tracker` is `"github"`**:
  ```
  /factory run                       drain the queue: every open `factory`-labeled GitHub issue
  /factory run <n> [<m> ...]         run specific GitHub issue number(s) now
  ```

- **In both modes**:
  ```
  /factory <anything>                talk to the factory: ask about jobs, give feedback, reject, refresh-patterns (or file a GitHub issue in `github` mode)
  ```

## The pipeline

```
Jira epic / ticket keys (Atlassian MCP)  OR  GitHub issues labeled `factory` (`gh`)
        │
        ▼
intake (fable)          → ACCEPT (scope possibly changed) | NEEDS-INFO | REJECT, commented on ticket/issue
route (script)          → assigns reviewers from config.json (code + security always; ux + ui on `ui` surface)
worktree                → factory/<job-id> from origin/<base>
spec-writer (opus high) → spec.md with small, ordered chunks (## Implementation Plan)
builder (haiku)         → runs each chunk sequentially on the same worktree; final chunk pushes + opens draft PR
gate (script)           → clean-tree + protected-paths + timed checks.sh; FAIL → Haiku builder fixes gate log
reviewers (opus high)   → the round's reviewers in parallel; WAIT FOR ALL
                          any CHANGES → deduplicate overlapping findings → Haiku review-fixer subagents → gate → review
                          (next round: reviewers that asked for changes + those configured `"rerun": "always"`)
approver (opus high)    → writes human-review summary (decision.md) → PR comment (+ conflict warning) → gh pr ready
```

A job has at most `max_rounds` build rounds (`config.json`); a round is one
build → gate → review pass. `factory/bin/job advance` sends the job to
`needs-human` instead of another round when the last one fails. Every passing
job ends as a PR marked ready for human review—the factory never auto-merges.

## Files

`<repo>` is the main checkout (`git rev-parse --show-toplevel` from your
session). It stays the user's: you and the agents never change it except
through the scripts. `factory/jobs/` is gitignored runtime state.

```
<repo>/factory/config.json                     concurrency, rounds, reviewer routing, ports, prices
<repo>/factory/checks.sh                       the repo's checks; the gate runs it
<repo>/factory/patterns.md                     curated codebase patterns, exemplars, shared primitives
<repo>/factory/bin/job                         the only writer of job.json
<repo>/factory/bin/gate                        runs checks.sh in a worktree → round-N/checks.md
<repo>/factory/bin/route                       records the routed Opus (high) reviewers
<repo>/factory/jobs/<job-id>/job.json          the job's state; the dashboard reads it
<repo>/factory/jobs/<job-id>/issue.md          the Jira ticket or GitHub issue, quoted as data
<repo>/factory/jobs/<job-id>/intake.md         intake's decision, scope, risk, surfaces
<repo>/factory/jobs/<job-id>/spec.md           spec + chunked ## Implementation Plan
<repo>/factory/jobs/<job-id>/build.md
<repo>/factory/jobs/<job-id>/round-<N>/checks.md        the gate's verdict for round N
<repo>/factory/jobs/<job-id>/round-<N>/review-<r>.md    one per reviewer that ran in round N
<repo>/factory/jobs/<job-id>/decision.md                human-review summary (READY or ESCALATE)
<repo>/factory/jobs/<job-id>/feedback-<k>.md            the user's feedback on a needs-human job
<repo>/.worktrees/<job-id>/                    the job's worktree, on factory/<job-id>
```

Read `job.json` with `cat` when you need a field. Keep your context small: read
agents' files by their first line (`head -1`), except when extracting chunk
headings from `spec.md` (`grep '^### Chunk '`) or findings from `VERDICT: CHANGES`
review files to dispatch Haiku subagents.

## Tracker integration

- **When `tracker` is `"github"`**: `factory/bin/job` handles all GitHub issue
  reads, comments, and labels via `gh`. Do not use the Jira connector.
- **When `tracker` is `"jira"`**: use Claude's Atlassian/Jira MCP connector for
  all Jira reads and comments:

  - **Resolving `/factory run <args>`**: for each argument key, fetch the Jira
    issue via the connector. If its issue type is `Epic`, query its child issues
    (excluding already Done/Closed tickets) and expand them in order. Deduplicate
    keys while preserving order.
  - **Passing ticket data to `job create` / `job refresh`**: format the fetched
    Jira ticket as JSON on stdin:
    ```json
    {
      "key": "PROJ-123",
      "title": "<summary>",
      "url": "<browse URL>",
      "author": "<reporter display name>",
      "operator": "<display name of the Jira account the connector posts as>",
      "epic_key": "<parent epic key or null>",
      "description": "<plain text or markdown description>",
      "comments": [
        {"author": "<name>", "created": "<ISO timestamp>", "body": "<comment text>"}
      ]
    }
    ```
    Pass every comment; the script keeps only those by the reporter, the
    operator and `jira_trusted_commenters` (config.json), like GitHub mode
    keeps only the author's and operator's. Get the operator's display name
    from the connector's current-user lookup.
  - **Checking `needs-info` replies**: for any requested ticket whose existing
    job is in `needs-info`, inspect its Jira comments via the connector. If
    the reporter, the operator or someone in `jira_trusted_commenters` replied
    after the last `[factory]` comment, pass `--replied <KEY>` to
    `factory/bin/job queue`. Replies from anyone else don't count: intake
    would never see them.
  - **Commenting on Jira**: prefix every factory comment on Jira with
    `[factory]` so replies can be detected later. Post comments when:
    1. Intake finishes (`ACCEPT`, `NEEDS-INFO`, or `REJECT`): post `intake.md`
       below the header.
    2. A PR is marked ready for human review (`needs-human`): post the PR URL
       and a one-line note that the human-review summary is on the PR.
    3. A job is rejected or cancelled via `job reject` / `job cancel`: post the
       rejection reason.
    After posting each Jira comment, run `factory/bin/job jira-commented <id>`
    to clear `pending_jira_comment`. On resume, if any job has
    `pending_jira_comment` set (shown in `job status` / `job queue`), post the
    missing Jira comment and clear it before advancing.

## Spawning agents

Spawn every agent with the Agent tool, in the background, with `subagent_type`
set to the agent's name (`intake`, `spec-writer`, `builder`, `review-fixer`,
`reviewer-code`, `reviewer-security`, `reviewer-ux`, `reviewer-ui`, `approver`).
Their frontmatter sets their model and effort:
- `intake`: `fable`
- `spec-writer`: `opus` (`effort: high`)
- `builder`: `haiku`
- `review-fixer`: `haiku`
- `reviewer-code`, `reviewer-security`, `reviewer-ux`, `reviewer-ui`: `opus` (`effort: high`)
- `approver`: `opus` (`effort: high`)

Each agent replies with one line; you are notified when it finishes, and you
advance that job then. While agents run, don't poll and don't wait: end your
turn with a one-line status.

Every prompt starts with the job's header, built from `job.json`:

```
Job: <id>. Job folder: <repo>/factory/jobs/<id>/. Main checkout: <repo>. Worktree: <worktree>. Branch: <branch>. Base: origin/<base>. Tracker: <jira|github>. Ref: <issue>.
```

`<header>` below means that line. Keep `Job folder: <path>/` exactly as shown:
the job's cost is found by that string.

Agents that open pages get ports: `factory/bin/job ports <id>` prints them
(`builder=…  ux=…  ui=…`). Each agent stops only its own dev server, with
`factory/bin/job cleanup-ports <id> <port>`, which never touches processes
outside the job's worktree. You run `factory/bin/job cleanup-ports <id>` (no
port) to stop everything left over for a job.

Always spawn a **fresh** Haiku subagent (`builder` or `review-fixer`) for each
chunk or each review finding—do not reuse one long-running builder across
chunks or findings.

## Advancing a job

Each step below runs when the previous agent or script finishes. Report one
short line per transition, e.g. `001-proj-123-export PROJ-123: gate PASS → reviewing (code, security, ux, ui)`.

**intake.** Spawn `intake` with `<header>`. When it returns, read its decision:

- `ACCEPT`: run `factory/bin/route <id>`, then `factory/bin/job advance <id> spec`
  (creates the worktree). In `jira` mode, post `intake.md` (below header) as a
  `[factory]` comment on the Jira ticket and run `factory/bin/job jira-commented <id>`.
  Go to **spec**.
- `NEEDS-INFO`: `factory/bin/job advance <id> needs-info`. In `jira` mode, post
  `intake.md` (below header) as a `[factory]` comment on Jira and run
  `factory/bin/job jira-commented <id>`. The job parks until the reporter replies.
- `REJECT`: `factory/bin/job advance <id> rejected`. In `jira` mode, post
  `intake.md` (below header) as a `[factory]` comment on Jira and run
  `factory/bin/job jira-commented <id>`.

**spec.** Spawn `spec-writer` with `<header>`. When it returns,
`factory/bin/job advance <id> build` and go to **build (chunks)**.

**build (chunks — round 1).** Extract the ordered `### Chunk <k>: <title>`
headings from `<job folder>/spec.md` (`grep '^### Chunk '`). Run them
**sequentially, one fresh Haiku `builder` subagent per chunk**, on the same
worktree:

- Before spawning chunk `k`, record progress for the dashboard:
  `factory/bin/job progress <id> "chunk <k>/<M>: <title>"`
- Spawn `builder` with:
  `<header> Build round 1. Port: <builder port>. Chunk <k> of <M>: <title>. Implement ONLY Chunk <k> from spec.md.`
  (If `k == M`, add: `This is the final chunk: run factory/checks.sh, verify acceptance criteria, push, open the draft PR, and set STATUS: READY with ROUND and COMMIT in build.md.`)
- Wait for chunk `k` to finish before starting chunk `k + 1`.
- If any chunk writes `STATUS: BLOCKED`, run `factory/bin/job cleanup-ports <id>`
  and `factory/bin/job advance <id> needs-human` (in `jira` mode, post the
  escalation on Jira + `job jira-commented <id>`).
- When chunk `M` finishes with `STATUS: READY`, run
  `factory/bin/job cleanup-ports <id>`, then `factory/bin/job advance <id> gate`
  and go to **gate**. If `advance` refuses (uncommitted changes, stale
  `build.md`, not pushed, no PR), spawn one Haiku `builder` fix with the
  refusal; if it refuses again, stop this job and tell the user.

**build (gate-fix).** When the gate fails and another round is allowed, run
`factory/bin/job progress <id> "gate-fix (round <N>)"` and spawn a fresh Haiku
`builder` subagent with:
`<header> Build round <N>. Port: <builder port>. Gate-fix mode: read round-<N-1>/checks.md and checks.log, fix the failure, run factory/checks.sh, commit, push, and set STATUS: READY with ROUND and COMMIT in build.md.`
When it finishes, run `factory/bin/job cleanup-ports <id>` and advance
`build → gate` (or `needs-human` if `STATUS: BLOCKED`).

**build (review-fixes — point by point).** When reviews in `round-<N-1>/`
request changes and another round is allowed:

1. Read the `## Findings` section from every `VERDICT: CHANGES` file in
   `round-<N-1>/review-*.md` and collect all numbered findings into an ordered
   list. **Deduplicate and group** any findings from different reviewers that
   target the same file/function into one combined finding so sequential fixes
   do not overwrite each other. Call the resulting list length `T`.
2. Run them **sequentially, one fresh Haiku `review-fixer` subagent per
   finding**, on the same worktree:
   - First record progress: `factory/bin/job progress <id> "fix <i>/<T> (<reviewer>)"`
   - Spawn `review-fixer` with:
     `<header> Build round <N>. Review round <N-1> fix <i> of <T>. Reviewer: <r>. Finding: <finding text>.`
     (For `i == T`, add: `This is the final finding: run factory/checks.sh, push, and set STATUS: READY with ROUND and COMMIT in build.md.`)
3. Wait for fix `i` to complete before starting fix `i + 1`. If any writes
   `STATUS: BLOCKED`, run `factory/bin/job cleanup-ports <id>` and
   `factory/bin/job advance <id> needs-human` (in `jira` mode, post the
   escalation on Jira + `job jira-commented <id>`).
4. After fix `T` completes with `STATUS: READY`, run
   `factory/bin/job cleanup-ports <id>`, then `factory/bin/job advance <id> gate`
   and go to **gate**.

**build (feedback).** When the user gives feedback via `job feedback`, run
`factory/bin/job progress <id> "feedback-<k> (round <N>)"` and spawn a fresh
Haiku `builder` subagent with:
`<header> Build round <N>. Port: <builder port>. Feedback mode: read feedback-<k>.md, address it, run factory/checks.sh, commit, push, and set STATUS: READY with ROUND and COMMIT in build.md.`
Then run `factory/bin/job cleanup-ports <id>` and advance `build → gate`.

**gate.** Run `factory/bin/gate <id>`. It prints `GATE: PASS` or `GATE: FAIL`.

- PASS: `factory/bin/job advance <id> review` and go to **review**.
- FAIL: `factory/bin/job advance <id> build`. If it refuses because this was the
  last round, `factory/bin/job advance <id> needs-human` instead (and in `jira`
  mode post the PR link on Jira + `job jira-commented <id>`). Otherwise go to
  **build (gate-fix)**. No reviewer runs on a failing build.

**review.** `factory/bin/job advance <id> review` prints this round's
reviewers (also the keys of `reviews` in `job.json`). Round 1 runs every
routed reviewer (`code` and `security`, plus `ux` and `ui` on the `ui`
surface). After a CHANGES round, only the reviewers that asked for changes and
those configured `"rerun": "always"` run again; human feedback reruns them all.

Prompt each with `<header> Review round <N>.`. `ux` and `ui` look at pages, and
two dev servers in one worktree collide, so:

- If `dev_server` is set in `config.json`, first run
  `factory/bin/job serve <id>`. It starts one preview server and prints its
  URL; add `Preview URL: <url>. Don't start or stop a dev server.` to the `ux`
  and `ui` prompts. Spawn all of this round's reviewers **in one message**.
- Otherwise spawn every reviewer except `ui` in one message, with
  `Port: <ux port>.` for `ux`, and spawn `ui` (`Port: <ui port>.`) once `ux`
  has finished.

All run on `opus` (`effort: high`). **WAIT FOR ALL.** Each time one finishes,
run `factory/bin/job sync <id>` so the dashboard fills in. When all of this
round's reviewers finish, run `factory/bin/job cleanup-ports <id>` (it also
stops the preview server), then:

- all PASS: `factory/bin/job advance <id> approve`, then spawn `approver`
  with `<header> PR: #<pr>. Final round: <N>.`
- any CHANGES: `factory/bin/job advance <id> build` (or `needs-human` when it
  refuses because this was the last round). If `build`, go to
  **build (review-fixes — point by point)**.

**approve (human-review summary & ready PR — no auto-merge).** When `approver`
returns (`READY` or `ESCALATE` in `decision.md`):

1. Run `factory/bin/job advance <id> needs-human`. This posts the human-review
   summary from `decision.md` as a comment on the PR (with a warning if the
   branch conflicts with `<base>`, or if the PR head moved past the commit the
   gate passed) and marks the draft PR ready for review (`gh pr ready`). It
   **never** auto-merges.
2. In `jira` mode, post a `[factory]` comment on the Jira ticket with the PR
   URL and note that it is ready for human review, then run
   `factory/bin/job jira-commented <id>`.

A job is finished for this run when it reaches `needs-human` (PR ready for
review), `needs-info`, `rejected`, or `merged`.

A person can merge a job's PR by hand on GitHub at any time. `factory/bin/job queue`
or `job status` then moves the job to `merged` and cleans up its worktree; stop
any agents still running for it.

## /factory run [...]

**If `tracker` is `"jira"`:**
1. Require at least one argument. If none was given, stop and tell the user to
   pass a Jira epic key or space-separated Jira ticket keys.
2. Fetch each key via the Atlassian/Jira connector. Expand any Epic into its
   open child ticket keys.
3. For any ticket whose local job is in `needs-info`, check Jira comments via
   the connector to see if anyone replied after the last `[factory]` comment.
4. Run `factory/bin/job queue [--replied <KEY> ...] <KEY-1> <KEY-2> ...`.
5. For each `new` line, pipe its Jira JSON to `factory/bin/job create <KEY>`,
   then start at **intake**. Resume every `resume` line.
6. Each time a job finishes, re-run `factory/bin/job queue` with the same keys
   and start newly unblocked `new` / `resume` lines.

**If `tracker` is `"github"`:**
1. Run `factory/bin/job queue` (or `factory/bin/job queue <n> [<m> ...]` when
   specific issue numbers were passed).
2. For each `new` line, run `factory/bin/job create <n>` (no stdin needed; it
   fetches the issue via `gh`), then start at **intake**. Resume every `resume`
   line.
3. Each time a job finishes, re-run `factory/bin/job queue` and start newly
   unblocked `new` / `resume` lines.

**In both modes:** when nothing is queued or running, print
`factory/bin/job summary <ids>` for the jobs this run touched, then list PRs
ready for human review and any `needs-info` questions waiting on authors.

**Resuming.** State lives in files, so a new session picks up where a dead one
stopped. For a `resume` job: `intake` and `spec` respawn their agent; `build`
inspects `build.md` and `git log` to resume from the next unfinished chunk (or
pending review-fix/gate-fix); `gate` runs the gate; `review` spawns only the
reviewers of this round (`reviews` in `job.json`) whose `review-<r>.md` is
missing (re-run `factory/bin/job serve <id>` first if `ux` or `ui` is among
them and `dev_server` is set); `approve`
respawns `approver`. A `needs-info` job with a reply shows as `resume`: in
`jira` mode, pipe updated Jira JSON to `factory/bin/job refresh <id>` first;
then run `factory/bin/job advance <id> intake` (in `github` mode `advance`
refreshes `issue.md` automatically) and go to **intake**.

## /factory <anything>

Anything else after `/factory` is the user talking to the factory. Every change
of state still goes through the scripts. When it isn't clear which job they
mean, ask.

- **A question** (status, cost, why a job escalated, what a reviewer said):
  answer from `factory/bin/job status`, `factory/bin/job summary`, `job.json`
  and the job's files.
- **What needs them**: run `factory/bin/job status`. For each `needs-human` or
  `needs-info` job, report its PR link / `decision.md` summary or `intake.md`
  question.
- **Feedback on a `needs-human` job**: pass their words verbatim on stdin:

  ```bash
  factory/bin/job feedback <KEY> <<'EOF'
  <their feedback>
  EOF
  ```

  Then run **build (feedback)** and continue through gate → review → approve.
- **Rejecting or benching a `needs-info` or `needs-human` job**: pass their
  reason verbatim on stdin:

  ```bash
  factory/bin/job reject <KEY> <<'EOF'
  <their reason>
  EOF
  ```

  In `jira` mode, post the reason as a `[factory]` comment on the Jira ticket
  via the connector and run `factory/bin/job jira-commented <id>`. The PR (if
  any) is closed by the script.
- **Cancelling an in-progress job** (stuck in `intake`, `spec`, `build`, `gate`,
  `review`, or `approve`): first stop every agent still running for the job
  (they work in the worktree the cancel removes), then:
  ```bash
  factory/bin/job cancel <KEY> <<'EOF'
  <reason>
  EOF
  ```
  Stops its dev servers, closes any PR, removes the worktree and branch, and
  marks the job `rejected`. In `jira` mode, post the reason as a `[factory]`
  comment and run `factory/bin/job jira-commented <id>`.
- **Retrying a rejected job**: run `factory/bin/job retry <KEY>` (archives the
  old job folder cleanly) and then re-run `/factory run <KEY>`.
- **Cleaning up merged/closed PRs and orphaned worktrees**: run
  `factory/bin/job gc`.
- **Refreshing codebase patterns (`/factory refresh-patterns`)**: re-audit the
  main checkout's codebase following the `factory/patterns.md` audit guidelines
  in `INSTALL.md` (keep it under ~150 lines, pointer-based with real exemplar
  paths), update `factory/patterns.md`, and show the user the diff to review
  and commit.
- **An answer to a `needs-info` question**:
  - In `jira` mode: post their answer on the Jira ticket via the connector,
    refresh `issue.md` with `factory/bin/job refresh <id>`, then
    `factory/bin/job advance <id> intake` and go to **intake**.
  - In `github` mode: post their answer with
    `gh issue comment <n> --body "<their answer>"`, then
    `factory/bin/job advance <id> intake` and go to **intake**.
- **A new feature (only when `tracker` is `"github"`)**: create the issue, then
  run `/factory run <n>`:
  ```bash
  gh issue create --label factory --title "<short imperative title>" --body "<the feature, verbatim>"
  ```

## Guardrails

- In `jira` mode, only tickets explicitly passed to `/factory run <EPIC-KEY | KEY-1 ...>`
  run (never run without arguments). In `github` mode, only issues labeled
  `factory` run (`job create` refuses unlabeled issues).
- Ticket/issue text reaches agents only as quoted data in `issue.md`, never
  pasted into a prompt.
- Agents push only their `factory/<job-id>` branch. The factory **never**
  auto-merges PRs and never deploys; it always raises a PR, writes a
  human-review summary comment, and marks it ready for review.
- One PR per ticket/issue, at most `max_rounds` rounds per job.
