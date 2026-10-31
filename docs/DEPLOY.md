# Deploying the dashboard (Streamlit Community Cloud, free)

Result: a public `https://<name>.streamlit.app` link that works from any device, no laptop needed.
Everything runs on synthetic data; remediation is simulated only; no API key is required (or wanted).

## 1. Put the code on GitHub

`netcoop-ai/` must be the **root** of the repository (so `requirements.txt` and `pyproject.toml` sit at the top level).

Easiest on Windows, with **GitHub Desktop** (https://desktop.github.com, includes Git):

1. File → **Add local repository** → choose `C:\Desktop\Agentic_AI_TechZephyr\netcoop-ai` → when asked,
   **create a repository** here.
2. Check the file list on the left: there must be **no `.venv/`, `state/` or `reports/INC-...`** entries
   (`.gitignore` excludes them). You should see `data/`, `schemas/`, `reports/eval/`, `reports/examples/`.
3. Commit to `main`, then **Publish repository**. Untick *Keep this code private* if friends should read the code
   (Streamlit Cloud can also deploy private repos from your own account).

Do not upload through the GitHub website: it is limited to about 100 files per upload and easily picks up `.venv`.

## 2. Create the app

1. Go to https://share.streamlit.io and sign in with GitHub (allow access to the repository).
2. **Create app** → *Deploy a public app from GitHub*:
   * Repository: `<you>/netcoop-ai`
   * Branch: `main`
   * Main file path: `dashboard/app.py`
   * App URL: pick a name, e.g. `netcoop-ai`
3. **Advanced settings** → Python version **3.12**. Leave *Secrets* empty.
4. **Deploy.** The first build installs the dependencies from `requirements.txt` (a few minutes).

Share `https://<name>.streamlit.app` with your friends.

## Notes

* **Do not add `ANTHROPIC_API_KEY` as a secret.** The optional LLM narrative is off unless `NETCOOP_LLM=1`, and on a
  public app anyone could spend your credits.
* The cloud disk is temporary: incidents, approvals and reports reset when the app restarts. That is fine for a demo;
  the committed evaluation results (`reports/eval/`) are always shown.
* An app with no visitors goes to sleep after a while; the first visitor clicks *Wake up* and waits about a minute.
* Every push to `main` redeploys automatically.
* If the build fails, open *Manage app* → logs; the usual causes are a wrong main file path or `netcoop-ai/` not being
  the repository root.
