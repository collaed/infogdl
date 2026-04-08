#!/bin/bash
# Bidirectional merge of reference data between ecb.pm and local
# Profiles are merged (union), downvoted handles stay removed

REF_LOCAL="$HOME/ref_data_ecb"
REF_REMOTE="root@ecb.pm:/root/ref_data_ecb/"

mkdir -p "$REF_LOCAL/profiles"

echo "📥 Pulling reference data from ecb.pm..."
rsync -avz "$REF_REMOTE" "$REF_LOCAL/"

echo "📤 Pushing local profile updates to ecb.pm..."
rsync -avz "$REF_LOCAL/profiles/" "root@ecb.pm:/opt/infogdl-data/profiles/"

echo "✅ Sync complete"
