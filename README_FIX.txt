# Render Keep-Alive

Put `render-keepalive.yml` at:
`.github/workflows/render-keepalive.yml`

It pings the Render `/health` endpoint every 10 minutes.

Current blocker:
Render starts the app but Bale rejects the configured bot token with:
`bale.error.APIError: 401: Unauthorized`

Create a NEW Bale bot token and replace the Render environment variable:
`BALE_TOKEN`

Do not publish the old token again; it has been exposed and should be rotated.
