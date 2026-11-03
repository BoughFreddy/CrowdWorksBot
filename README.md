# CrowdWorks → Telegram New Job Alert Bot

Watches 5 CrowdWorks category search pages (newest first), and sends a Telegram
message for each **new** job whose category, title or description matches one of
the keywords in `keywords.txt`.

Watched by default (category_id 226, 311, 230, 235, 242, `order=new`).

## Setup

1. **Install Python 3.9+**, then in this folder:
   ```
   pip install -r requirements.txt
   ```
2. **Create a Telegram bot**: in Telegram open **@BotFather**, send `/newbot`,
   follow the prompts, and copy the token.
3. **Configure**
   - Copy `env.example` to `.env` and paste the token into `TELEGRAM_BOT_TOKEN`.
   - Open your new bot in Telegram and send it any message (e.g. "hi").
   - Run `python crowdworks_bot.py --get-chat-id` and put the printed number into
     `TELEGRAM_CHAT_ID`.
4. **Keywords**: edit `keywords.txt` (one per line). Already filled with your list.
5. **Test and run**
   ```
   python crowdworks_bot.py --dry-run   # prints which current jobs match (sends nothing)
   python crowdworks_bot.py --test      # sends a test Telegram message
   python crowdworks_bot.py             # start monitoring (checks every 5 min)
   ```

On the first run the bot remembers the jobs already listed (no flood of alerts) and
then alerts only for new ones. Seen job IDs are stored in `seen_jobs.json`; delete it
to start fresh.

## How matching works

A keyword matches a job if any of these is true (full/half-width and case are ignored):

- the job's CrowdWorks category contains it. The category is the text after the title in
  "<title> Webデザインの仕事の依頼" (here: Webデザイン). So `LP（ランディングページ）制作`
  also matches the category `LP（ランディングページ）制作・デザイン`.
- the whole phrase appears in the title or description,
- every part of it appears in the title (`iPhone・iPadアプリ開発` → "iPhone" + "iPadアプリ開発").

If the search-page data doesn't settle it, the bot opens the job page once to read
its category, client and full description (`FETCH_DETAILS=true`).

`EXCLUDE_KEYWORDS` blocks a job when an entry is the client's username (or part of it),
or appears in the title/description. Each Telegram alert shows
the title, budget, category, client, matched keywords, a short summary and the link.

## Keeping it running 24/7

- **Linux / VPS**: `nohup python crowdworks_bot.py &`, a systemd service, or cron:
  ```
  */5 * * * * cd /path/to/CrowdWorksBot && /usr/bin/python3 crowdworks_bot.py --once
  ```
- **Windows**: Task Scheduler running `python crowdworks_bot.py --once` every 1 minute.

## Notes

- The bot waits `REQUEST_DELAY` (3 s) between requests and checks every 1 minute.
  Please keep it gentle; CrowdWorks' terms may restrict automated access, so use it
  for personal alerts only.
- If the log says "No jobs parsed", CrowdWorks changed their page layout and
  `parse_jobs()` needs updating.
