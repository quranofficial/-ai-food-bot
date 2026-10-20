# AI Food Telegram Bot (Kurdish Sorani)

## Features
- Receives a Telegram food photo.
- Uses Gemini vision to estimate the food and approximate calorie range.
- Lets the user specify portion size and oil.
- Stores a history of estimates in SQLite.
- Kurdish Sorani menu and replies.

## Required GitHub Secrets
- `BOT_TOKEN`: Telegram bot token from @BotFather.
- `GEMINI_API_KEY`: Gemini API key from Google AI Studio.

Never put tokens directly in `bot.py` or commit them to the repository.

## Setup from a mobile phone
1. Create a repository on GitHub.
2. Upload `bot.py`, `requirements.txt`, and `.github/workflows/bot.yml` preserving the folder path.
3. Open the repository: Settings → Secrets and variables → Actions → New repository secret.
4. Add `BOT_TOKEN` and `GEMINI_API_KEY`.
5. Open Actions, enable workflows if GitHub asks, then select “AI Food Telegram Bot” → Run workflow.
6. Open Telegram and send `/start` to your bot.

## Important hosting limitation
GitHub Actions is a CI runner, not a 24/7 bot host. This workflow stops after about 5 hours 40 minutes and scheduled runs may be delayed. Therefore, the bot will be offline between runs and may miss a prompt response until it starts again. For reliable always-on polling, use an always-on hosting service or webhook host. Do not create multiple simultaneous polling runs with the same bot token.

## Gemini free tier
Availability, rate limits, and model names vary by region/account and can change. Check current Gemini API pricing and limits before relying on the free tier. If the configured model is no longer available to your key, change `GEMINI_MODEL` in the workflow to a currently supported vision model.

## Privacy
Food photos are sent to the configured AI provider for analysis. Do not send photos containing personal or sensitive information. The SQLite database stores estimates and Telegram user IDs in the runner's filesystem; GitHub-hosted runners are temporary, so history may be lost when the job ends. This demo does not upload the database as an artifact.

## Notes
Calorie values are estimates from an image and can be significantly wrong because portion size, oil, ingredients, and cooking method are hard to determine visually. The bot does not provide medical advice or a daily calorie target.
