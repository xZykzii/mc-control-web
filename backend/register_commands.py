import json
import os

import requests


APP_ID = os.environ["DISCORD_APPLICATION_ID"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

with open("commands.json", encoding="utf-8") as f:
    commands = json.load(f)

if GUILD_ID:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
else:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"

resp = requests.put(
    url,
    headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"},
    json=commands,
    timeout=30,
)

print(resp.status_code)
print(resp.text)
resp.raise_for_status()
print("registered guild commands" if GUILD_ID else "registered global commands")
