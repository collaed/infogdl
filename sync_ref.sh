#!/bin/bash
# Sync reference data from ecb.pm to local machine
# Run: ./sync_ref.sh

REF_LOCAL="$HOME/ref_data_ecb"
REF_REMOTE="root@ecb.pm:/root/ref_data_ecb/"

echo "📥 Pulling reference data from ecb.pm..."
rsync -avz "$REF_REMOTE" "$REF_LOCAL/"
echo "✅ Synced to $REF_LOCAL"

# Also push local profile updates back
if [ -d "$REF_LOCAL/profiles" ]; then
    echo "📤 Pushing profile lists back to ecb.pm..."
    rsync -avz "$REF_LOCAL/profiles/" "root@ecb.pm:/opt/infogdl-data/profiles/"
    echo "✅ Profiles synced"
fi
