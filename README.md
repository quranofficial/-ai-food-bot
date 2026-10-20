# AI Food Super Pro — Gemini first, Groq fallback

A Sorani Kurdish Telegram food assistant. For each AI request, it tries Gemini exactly once. If Gemini fails (including quota errors such as 429 or availability errors such as 503), it immediately tries Groq.

## Features
- Analyze food photos and estimate calories/macros.
- Chat in Sorani about food and general nutrition.
- Suggest simple meals and recipes.
- View recent history and daily summary; delete personal history.
- Refine photo estimates with portion/oil buttons.
- Optimize image size and show clearer error messages.

## GitHub setup (phone-friendly)
1. Upload/replace these project files in your GitHub repository. Keep `.github/workflows/bot.yml` at exactly that path.
2. Open repository Settings → Secrets and variables → Actions.
3. Add `BOT_TOKEN` from Telegram BotFather.
4. Add `GEMINI_API_KEY` for the first-choice AI and `GROQ_API_KEY` for automatic fallback. At least one AI key is required; for the requested fallback behavior, add both.
5. Open Actions → AI Food Super Pro → Run workflow.

## AI providers
- Gemini is called once per request. If it errors or returns an empty answer, the bot immediately switches to Groq.
- Groq fallback model defaults to `qwen/qwen3.8-27b`, which supports text and image inputs according to Groq's current vision docs. You can change `GROQ_MODEL` in the workflow if the provider changes availability.
- Both providers have usage limits and can fail; fallback is not a guarantee of unlimited free access.

## Important
GitHub Actions is not 24/7 hosting and its local SQLite database may not persist across fresh runners. Food calories/macros from photos are estimates, not medical measurements. Never put API keys directly in source code or commit them to GitHub.
