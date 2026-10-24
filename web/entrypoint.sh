#!/bin/sh
# The data folder is a bind mount that Docker may have created as root: hand it to the app user, then drop root.
set -e
chown -R dressing:dressing /data
exec gosu dressing "$@"
