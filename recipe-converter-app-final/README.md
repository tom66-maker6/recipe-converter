# Recipe Converter

Converts pastry recipes (Word, Excel, text-PDF) into your standardized Wynn Excel
template — preserving the template 100% (logo, fonts, borders, formulas) — with
ingredient standardization, kg→g / egg / **Gelatine Mass** conversions, confidence
scoring and warnings. A simple shared **access code** protects the whole app.

Runs on **Render's free tier**. No framework to install, no corporate IT, no Azure.

---

## Deploy on Render (free)

1. Put the **contents** of this folder into a GitHub repository (all sub-folders
   preserved — `backend/`, `frontend/`, `infra/`, `render.yaml`, …).
2. In Render: **New → Blueprint → connect the repo**. Render reads `render.yaml`
   and builds the Docker image.
3. Choose the **Free** instance type.
4. Add one secret environment variable in the Render dashboard:
   `SHARED_ACCESS_CODE = wynn2027`  (change the value any time — no re-upload needed).
5. Deploy. Open the link Render gives you, enter the access code, and use it.

The access code is **never stored in the code** — it lives only in that Render
environment variable, which you can edit whenever you like.

## Access control

- One shared access code gates everything: the home page, upload, preview,
  conversion and download routes are all protected.
- Users stay signed in for the browser session and can **Sign out**.
- Wrong codes are rejected and login attempts are rate-limited.
- To change the code: edit `SHARED_ACCESS_CODE` in Render → the service restarts →
  the new code applies. Nothing in GitHub changes.

## Run locally (optional)

    cd recipe-converter-app
    pip install -r backend/requirements.txt
    AUTH_MODE=code SHARED_ACCESS_CODE=wynn2027 COOKIE_SECURE=false python backend/server.py
    # open http://localhost:8000

## What works on the free version

| Feature | Status |
|---|---|
| Word (`.docx`) and Excel (`.xlsx`) recipes | ✅ works |
| Text-based PDF | ✅ works |
| Multiple files at once (queued, processed one by one) | ✅ works |
| Standardization, conversions, confidence, warnings | ✅ works |
| Fills your exact template + Download / Download-All | ✅ works |
| Scanned PDFs and phone photos (need OCR) | ⚠️ requires a paid OCR service (Azure Document Intelligence) |
| Messy free-form layouts + full sentence translation | ⚠️ better with optional Azure OpenAI; the built-in extractor handles clean table layouts |

## Notes

- **Free tier sleeps** after ~15 min idle; the next visit wakes it in ~30–60 s.
- **Free tier has no permanent disk**, so newly *learned* ingredient corrections and
  the audit log reset on restart (the built-in standardizations always apply). For
  permanent memory, switch the Render plan to Starter and add a disk at `/var/data`.
- Configuration reference: `.env.example`. Deploy blueprint: `render.yaml`.
