import os

for i in range(1, 80):
    os.system(f'jiji post "Hello, {i}! qwertyuiopasdfghjklzxcvbnmqwertyuiopasdfghjklzxcvbnmqwertyuiopasdfghjklzxcvbnmqwertyuiopasdfghjklzxcvbnmqwertyuiopasdfg" --keyfile /tmp/alice.key')
    print(f'Posted message {i}')

os.system('jiji status')

os.system('jiji post "VERY LARGE MESSAGE qwertyuiopasdfghjklzxcvbnmqwertyuiopasdfghjklzxcvbnmqwertyuiopasdfghjklzxcvbnmqwertyuiopasdfghjklzxcvbnmqwertyuiopasdfg" --keyfile /tmp/alice.key')
os.system('jiji status')
