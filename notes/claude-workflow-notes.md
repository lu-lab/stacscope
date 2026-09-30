# Working with Claude Code on the SPIM Engine reimplementation

Process notes from the session that produced `spim-engine-walkthrough.md`.
Read this before starting a new session on this problem — it's here so the
same mistakes and the same slow path aren't repeated.

## Why this file exists instead of relying on memory

Claude Code's memory system is scoped to the exact project directory path
it's invoked from. A new repo — even the "same" project recreated at a
different path, or on a different machine — gets a fresh, empty memory
store. Nothing said or learned in the original session carries over
automatically. These handoff files are the actual transfer mechanism;
memory is not. Point a new session at this folder (or paste its contents
into the new repo) explicitly.

## What this environment could and couldn't do

This session ran with **no LabVIEW installed and no hardware attached** —
pure static analysis of binary `.vi`/`.lvclass` files via ASCII string
extraction (see the Method note in `spim-engine-walkthrough.md`). That
technique is real but has a hard ceiling: it sees *what a VI references*,
never *how it's wired* — no case-structure branches, no button event logic,
no front-panel captions. Getting past that ceiling took a human (thesis
chapter with block-diagram screenshots), not a better prompt or more
sub-agent time.

**If the new session has LabVIEW installed:** use
`File → Print → VI Documentation` (or manual screenshots of the block
diagram / front panel) and hand the images to Claude directly — it reads
images fine. This closes the gap that cost the most time and produced the
one wrong conclusion in the original session (see below). Don't re-run the
string-extraction approach as a first resort once real diagram access is
available; it's a fallback, not the preferred method.

## The mistake that happened, and why

Investigating whether pressing "Record" differs from "Run," the session
checked the camera class's `.lvclass` for a "Write to disk" field among the
messages it could receive, found nothing wired to it, and concluded no
distinct Record path existed in the current code. That conclusion was
**wrong** — it was corrected only when the user supplied the thesis chapter,
which showed `Send Start Triggered Acq.vi` exposes a `Write to disk?`
boolean terminal directly. Re-checking confirmed it: the field is there,
just not in the file that was checked.

Root cause: message payload fields live in a message class's `.lvclass` as
an opaque base64 blob (not text-extractable), but the same fields are
plaintext as terminal labels on that message's `Send *.vi`. The session
checked the wrong file and treated absence-of-evidence as evidence of
absence. **Generalize this**: when a binary-format investigation concludes
"X doesn't exist" or "X isn't wired to anything," treat that as a weak,
provisional finding, not a fact to build on — say so explicitly, and verify
against a second source (the actual editor, a design doc, or the person who
knows the system) before designing around it.

## What worked well

- **Front-loading authoritative sources beats exploration.** The single
  highest-value input all session was the thesis chapter, supplied partway
  through. Everything before it was inference from strings and took far
  longer per fact established. If more thesis chapters or other design docs
  exist, get them into the new session's context on turn one, not
  turn thirty.
- **Parallel sub-agents for decomposing a large binary call graph.** Three
  Explore-type agents traced the DAQ Timed SPIM actor, the Basic SPIM
  Controller actor, and the camera actor in parallel, each reporting back a
  bounded summary. This kept the main conversation's context from being
  consumed by raw string dumps and was faster than doing it serially. Worth
  repeating for any similarly large, unfamiliar binary tree — but see the
  mistake above: give sub-agents the specific pitfalls list, not just the
  general technique, or they'll rediscover the same wrong turn.
- **Naming conventions and folder duplication were reliable dead-code
  signals** even without semantic understanding: `_v2` suffixes, `.backup.vi`
  files, `testing*.vi`, and parallel folders with near-identical contents
  (`HamamatsuFluor/` vs `HamamatsuFluor_v2/`) consistently marked superseded
  or experimental code. This heuristic held up well and is probably
  transferable to other poorly-source-controlled LabVIEW projects.

## Hardware safety — read this before running anything on the control machine

This session never had hardware access, so this is a boundary to set going
in, not one that was tested:

- **Don't let Claude execute anything that asserts real analog/digital
  output, triggers a camera acquisition, or otherwise actuates the
  microscope.** The original thesis itself notes improper camera shutdown
  can wedge the DCAM drivers badly enough to need a system reboot — the
  blast radius of a wrong autonomous action here is a broken instrument,
  not just broken code.
- Keep Claude in a **read, analyze, and write-code** role for anything
  hardware-adjacent. The user runs and observes; Claude interprets logs,
  screenshots, and error messages handed back to it.
- If prototyping a rewrite, do it in an isolated worktree/branch so the
  software actually driving the microscope is never mid-edit when someone
  needs to use it.

## Recommended sequence for the next session

1. Bring this folder's three files, plus the full thesis (not just Ch. 3 if
   other chapters are relevant — Ch. 2 covers the ETL/MPC optics this
   design depends on), into the new repo from the start.
2. `git init` immediately, before writing any new code. This project had no
   version control at all, which the thesis itself calls out as a real cost
   (§3.6) — don't reproduce that in the rewrite.
3. Decide the target framework early and explicitly, rather than letting it
   default: a from-scratch Python actor implementation, **Pycro-Manager**
   (µManager acquisition hooks/sockets), or **Python-Microscope**
   (client-server device model already close to this design) — see
   `spim-engine-walkthrough.md`'s carry-forward section for the tradeoffs
   the thesis itself raises (§3.6). This decision changes how the rest of
   the exploration and design work should be structured, so make it before
   diving into implementation details.
4. If LabVIEW is available, spend a little time up front exporting diagram
   images for the handful of VIs the walkthrough doc flags as inferred
   rather than confirmed (see its "Open discrepancy" section and the
   Fig-number citations) — cheap to resolve now, expensive to get wrong
   later.
5. Build and validate the shared-memory buffer contract first, in
   isolation, before touching device actors — it's the interface the
   existing napari viewer and converter tooling already depend on, so
   getting it byte-compatible early means those consumers keep working
   throughout the rest of the rewrite.
