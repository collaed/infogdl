#!/bin/bash
# Export browser cookies and sync to ecb.pm for scraping
# Cron: daily at 6am

set -e
COOKIE_DB="$HOME/snap/firefox/common/.mozilla/firefox/d4cz308g.default/cookies.sqlite"
EXPORT_DIR="/tmp/infogdl_cookies"
REMOTE="root@ecb.pm"
REMOTE_DATA="/opt/infogdl-data"

mkdir -p "$EXPORT_DIR"
cp "$COOKIE_DB" "$EXPORT_DIR/cookies.sqlite" 2>/dev/null || true

# Export cookies to proper Netscape format
python3 << 'PYEOF'
import sqlite3

db = sqlite3.connect("/tmp/infogdl_cookies/cookies.sqlite")
lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html", ""]

domains = ("%x.com%", "%twitter.com%", "%linkedin.com%", "%instagram.com%")
placeholders = " OR ".join(["host LIKE ?"] * len(domains))

rows = db.execute(
    f"SELECT host, path, isSecure, expiry, name, value FROM moz_cookies WHERE {placeholders}",
    domains
).fetchall()

count = 0
for host, path, secure, expiry, name, value in rows:
    if "\t" in value or "\n" in value:
        continue
    domain_flag = "TRUE" if host.startswith(".") else "FALSE"
    secure_flag = "TRUE" if secure else "FALSE"
    expiry = str(int(expiry)) if expiry else "0"
    lines.append(f"{host}\t{domain_flag}\t{path}\t{secure_flag}\t{expiry}\t{name}\t{value}")
    count += 1

db.close()
with open("/tmp/infogdl_cookies/cookies.txt", "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"Exported {count} cookies")
PYEOF

scp -q "$EXPORT_DIR/cookies.txt" "$REMOTE:$REMOTE_DATA/cookies.txt"
rsync -az "$HOME/ref_data_ecb/profiles/" "$REMOTE:$REMOTE_DATA/profiles/" 2>/dev/null || true
rsync -az "$REMOTE:/root/ref_data_ecb/" "$HOME/ref_data_ecb/" 2>/dev/null || true
rm -rf "$EXPORT_DIR"
echo "$(date): Cookie sync complete" >> "$HOME/infogdl/infogdl.log"
