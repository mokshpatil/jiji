Generate key
jiji keygen --keyfile <loc>

Start a node
jiji node --mine --keyfile <loc> --data-dir <loc> --log-level INFO

New node
jiji node --mine --keyfile <loc> --data-dir <loc> --port 9335 --rpc-port 9334 --peers 127.0.0.1:9333

Make post 
jiji post "Hello, 1!" --keyfile /tmp/alice.key
jiji post "Hello, 2!" --keyfile /tmp/alice.key