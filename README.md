# drop-manifest-data

Daily scraper feeding the [Drop Manifest](https://claude.ai/code/artifact/07f5661b-175d-4bc8-adb9-15d0e6618418) artifact.

`scraper.py` runs on a GitHub Actions cron (`.github/workflows/daily-refresh.yml`, 12:45 UTC daily) with full internet access, checks each tracked brand's storefront for genuine markdowns, verifies stock, and writes everything — including base64-embedded product photos — to `data.json`.

A separate Claude Code routine (network-restricted) clones this repo and reads `data.json` directly off disk, so it never has to make its own outbound web requests to the retail sites.
