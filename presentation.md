remove old files
rm -rf /tmp/alice.key /tmp/bob.key /tmp/miner.key
rm -rf /tmp/jiji-alice /tmp/jiji-bob /tmp/jiji-miner

make the keys
jiji keygen --keyfile /tmp/alice.key
jiji keygen --keyfile /tmp/bob.key
jiji keygen --keyfile /tmp/miner.key

miner mines
jiji node \
  --mine \
  --keyfile /tmp/miner.key \
  --data-dir /tmp/jiji-miner \
  --lan

bob also mines
jiji node \
  --mine \
  --keyfile /tmp/bob.key \
  --data-dir /tmp/jiji-bob \
  --port 9335 --rpc-port 9334 \
  --peers 127.0.0.1:9333

send money from miner
jiji transfer \
  --keyfile /tmp/bob.key \
  --data-dir /tmp/jiji-miner \
  0deb8c91e4a7c3bb1f044facab09a066e82f79ef27e6d119c806d9ff9f6602bd 300



run server
cd frontend
python3 serve.py

incase port in use
lsof -nP -iTCP:9333 -sTCP:LISTEN                 

Open the page (e.g. http://127.0.0.1:8080), press Cmd+Opt+J to open the console, paste:


localStorage.removeItem("jiji.wallet");
localStorage.removeItem("jiji.node");
indexedDB.deleteDatabase("jiji-cache");
location.reload();