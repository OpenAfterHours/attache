---
icon: lucide/chart-no-axes-combined
---

# Data analysis

The web workspace's **Pages** list shows saved results pages under **Analyses**. Ask
Attaché to analyse a dataset and it can use the optional bundled `analysis` skill to keep
calculations in a persistent [Hailer](https://github.com/OpenAfterHours/hailer) notebook.
Attaché remains the chat and uses your existing model configuration; no second Hailer model
or API key is needed.

After upgrading an existing installation, run `attache init` to add the bundled analysis
skill, using the same `--home` if you use a custom home. Existing files are preserved.

For example: “Analyse sales.csv in a saved notebook and show monthly revenue.” Put the
dataset in the chat's files folder first and tell Attaché its filename. The skill accepts
CSV, TSV, Parquet, JSON, JSONL and Excel files. It copies each selected file into a dedicated
analysis workspace and refuses to replace an existing copy.

## Saved results and live notebooks

Saved results are HTML pages named `analysis-<name>`, listed under **Analyses**. These
are snapshots of verified calculations; ask in chat to refresh them after data changes.
They use the same sandbox as every other Attaché page.

The **live editable notebook opens in a separate browser window on the machine running
Attaché**. It is not embedded in the workspace. Keep the notebook open while working;
opening a notebook establishes the session its calculations use. Tables, charts and Python
cells remain saved in the workspace, so Attaché can reopen them later.

This first bridge uses Hailer's versioned runtime and notebook interfaces without exposing
the notebook server through Attaché's artifact sandbox. A future inline editor needs a
separately authenticated notebook transport and WebSocket proxy, not a relaxation of the
rules for generated pages.

## Requirements and lifecycle

- Docker must be installed and running. The adapter uses Docker only, with network access
  off, copied datasets mounted read-only, and only the notebooks folder writable.
- The skill declares `hailer==0.2.8`, which pins `marimo==0.24.2`, and Python 3.12 or newer.
  `uv run` installs these separately on first use. Attaché's core dependencies are unchanged.
  The first notebook start may also download Hailer's versioned Docker kernel image.
  If that registry image is unavailable, ask Attaché to **build the analysis kernel**, or
  run the skill with `args: ["sales", "build"]` after initialising the `sales` workspace.
  This uses Hailer's bundled image builder with host package configuration disabled, then
  `start` uses the locally built matching image. Allow several minutes and about 1 GB of disk.
- Notebook execution does not receive host model credentials. Summaries returned to Attaché
  are part of its normal model conversation, so your configured model receives those summaries.
- The skill is owner-only and cannot be invoked by members or from the owner's group turns.
- A started Docker kernel persists across short script calls and Attaché restarts. Ask
  “Stop the sales analysis” when done; saved notebooks and datasets remain. A later `start`
  reuses a matching running kernel or starts one, with no silent fallback to host execution.

The workspace layout is `files/<chat>/analysis/<name>/`: `data/` contains imported copies,
`notebooks/` contains ordinary marimo Python notebooks, and `.hailer/` holds runtime and
active-notebook state outside the container mounts. Browser authentication tokens stay in
that private runtime state and the browser launcher, never in Attaché results pages.

## Adapter contract and verification

The bundled script builds its own fixed Docker configuration rather than loading Hailer
environment overrides or model settings. It rejects mismatched versions, runtime settings
and mounts before executing. Shutdown selects only Docker resources labelled for that
workspace, including resources left by an interrupted start; a failed cleanup retains its
runtime record for retry. Notebook creation and active selection use Hailer's native APIs.

The adapter targets the **published PyPI Hailer 0.2.8 wheel**, not an arbitrary checkout
reporting the same version. That release exposes `HailerTools(config, server=...)`, persistent
kernel state and `DockerRuntime`; newer main-branch code uses a different sandbox contract.
The wheel's SHA-256 is
`5031b1911d9cea7d7b7237d8cad04d7594d20d445d234852ac92c559f289eeac`.
Upgrading this bridge requires checking the new API and lifecycle together. A signature
guard rejects the incompatible same-version checkout rather than attempting a fallback.

The implementation is covered by offline tests with fake Hailer modules and fake runtime
objects, including ownership, containment, resume, shutdown failures and token redaction.
Those tests do not install Hailer, start Docker, call a model, or analyse real user data.
A separate contract smoke check imported the exact published wheel, bound the adapter's
runtime and tool signatures, and created/listed a notebook in a disposable directory using
the actual library, without starting a kernel or importing Hailer's model agent.
A separate Windows Docker/headless-Chrome smoke check built that exact release's image
from its public wheel assets, imported a synthetic three-row CSV and computed its total
of 42 in a saved notebook cell. It verified runtime reuse, stop/restart and notebook reopen,
read-only data, blocked outbound networking and withholding of a synthetic host secret.
The test removed its containers, network and temporary workspace; it made no model calls.

At verification time the public `0.2.8` registry image was unavailable without authentication.
The local `build` fallback succeeded and produced a roughly 914 MB image. Registry access
and available disk space are prerequisites to check on each machine; the local builder
needs access to its public base image and Python package downloads.
