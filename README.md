# Hoshigami bot + temporary video website

The bot and `index.html` are served by the same Python process and the same Render Web Service. Run one service instance for this in-memory vault version; multiple instances would not share the temporary files or access state.
The website uses the existing Hoshigami dark theme and provides a private one-hour video vault.

## Render settings

- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Health Check Path: `/health` (recommended)
- `BOT_TOKEN`: required Telegram bot token
- `PYTHON_VERSION`: `3.12.8` (or another supported Python 3.12 version)
- `AGNES_API_KEY`: optional AI key
- `EPISODE_API_URL`: optional API endpoint you own or are licensed to use
- `EPISODE_API_KEY`: optional bearer key for that endpoint

Render automatically provides `PORT` and, for a Web Service, `RENDER_EXTERNAL_URL`.
If `RENDER_EXTERNAL_URL` is not provided, set `WEBHOOK_URL` to the public HTTPS Render URL.

## Temporary video vault

After a user is verified as a channel member, the bot creates a random 6–10 digit access ID and a separate high-entropy access key. The Telegram message contains a secure website link. The numeric ID alone is not enough to access the vault.

Videos larger than `TELEGRAM_UPLOAD_LIMIT` are streamed to temporary disk storage and are linked to that user's active ID. The website supports in-browser playback and downloads, including HTTP byte-range requests for seeking. The ID, key, and its videos expire after one hour. Cleanup runs every five seconds.

Optional size settings:

- `TELEGRAM_UPLOAD_LIMIT`: defaults to `50000000` bytes
- `MAX_VIDEO_BYTES`: defaults to `524288000` bytes (500 MiB per source video)
- `MAX_VAULT_BYTES_PER_USER`: defaults to 750 MiB
- `MAX_VAULT_BYTES_GLOBAL`: defaults to 1 GiB
- `VAULT_DIR`: defaults to `/tmp/hoshigami-vault`

The vault is intentionally temporary and in-memory metadata is not persistent. Render restarts, redeployments, or instance replacement clear active vaults; users will need to verify again. Keep files within a storage size supported by the selected Render instance.

## Media rights

Configure `EPISODE_API_URL` only for content you own or are authorized to distribute. This update adds temporary delivery/storage only; it does not add a media-source downloader or API.
