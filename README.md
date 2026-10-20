# AI Food Super Pro Telegram Bot

A Sorani Kurdish food assistant powered by Gemini. Features:
- Analyze food photos and estimate calories and macros.
- Ask food and nutrition questions in a ChatGPT-like text conversation.
- Get simple meal and recipe ideas.
- View recent meal history and a daily summary.
- Delete your saved food history.
- Refine a photo estimate with portion-size and oil buttons.
- SQLite database with a safe migration from the earlier bot schema.
- Retry temporary Gemini overload/rate-limit errors and compress images before upload.

## GitHub setup (mobile-friendly)
1. Upload the files in this folder to your repository, keeping `.github/workflows/bot.yml` in that exact path.
2. In repository Settings → Secrets and variables → Actions, create:
   - `BOT_TOKEN`: Telegram token from BotFather.
   - `GEMINI_API_KEY`: Gemini API key.
   - `BOT_ACCESS_CODES`: one or more owner-created access codes separated by commas, for example `FOOD-A7K9,FOOD-P4M2`. Use unique, hard-to-guess codes and never publish them. Each code can authorize one Telegram account only.
3. Open Actions → AI Food Super Pro → Run workflow.
4. Open the bot in Telegram and send `/start`. Enter one access code when prompted. The same Telegram account will not be asked again while its saved authorization state remains available.

## Notes
- The default model is `gemini-3.8-flash` in the workflow; change `GEMINI_MODEL` there if Google changes model availability.
- Gemini free quotas and availability can vary. Retries help with temporary errors but cannot guarantee a response.
- GitHub Actions is not permanent 24/7 hosting. A cache is used for the small, hashed one-time authorization state so it can usually carry between runs, but GitHub may evict caches; if that happens, a user may need a new unused code. Food history is still local to each runner and is not guaranteed to persist across runs. For guaranteed permanent login and history, use persistent hosting/storage.
- Calories and macros from a photo are estimates, not medical measurements. The bot does not diagnose conditions or recommend extreme diets.
- Never commit bot tokens or API keys to the repository.
