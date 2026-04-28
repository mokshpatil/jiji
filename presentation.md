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
  --log-level INFO
  --lan

bob also mines
jiji node \
  --keyfile /tmp/bob.key \
  --data-dir /tmp/jiji-bob \
  --port 9335 --rpc-port 9334 \
  --peers 127.0.0.1:9333

run server
cd frontend
python3 serve.py
