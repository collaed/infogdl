#!/bin/bash
# Export browser cookies and sync to ecb.pm for scraping
# Cron: daily at 6am

set -e
COOKIE_DB="$HOME/snap/firefox/common/.mozilla/firefox/d4cz308g.default/cookies.sqlite"
EXPORT_DIR="/tmp/infogdl_cookies"
REMOTE="root@ecb.pm"
REMOTE_DATA="/opt/infogdl-data"

mkdir -p "$EXPORT_DIR"

# Copy cookie DB (Firefox locks it)
cp "$COOKIE_DB" "$EXPORT_DIR/cookies.sqlite" 2>/dev/null || true

# Export X.com cookies to Netscape format
python3 -c "
import sqlite3, sys
db = sqlite3.connect('$EXPORT_DIR/cookies.sqlite')
lines = ['# Netscape HTTP Cookie File', '']
for name, value, host, path, secure, expiry in db.execute(
    'SELECT name, value, host, path, isSecure, expiry FROM moz_cookies WHERE host LIKE \"%x.com%\" OR host LIKE \"%linkedin.com%\" OR host LIKE \"%instagram.com%\"'):
    secure_str = 'TRUE' if secure else 'FALSE'
    lines.append(f'{host}\tTRUE\t{path}\t{secure_str}\t{expiry}\t{name}\t{value}')
db.close()
open('$EXPORT_DIR/cookies.txt', 'w').write('\n'.join(lines) + '\n')
print(f'Exported {len(lines)-2} cookies')
"

# Upload to ecb.pm
scp -q "$EXPORT_DIR/cookies.txt" "$REMOTE:$REMOTE_DATA/cookies.txt"

# Also sync profile lists and ref data
rsync -az "$HOME/ref_data_ecb/profiles/" "$REMOTE:$REMOTE_DATA/profiles/" 2>/dev/null || true
rsync -az "$REMOTE:/root/ref_data_ecb/" "$HOME/ref_data_ecb/" 2>/dev/null || true

# Cleanup
rm -rf "$EXPORT_DIR"

echo "$(date): Cookie sync complete" >> "$HOME/infogdl/infogdl.log"
