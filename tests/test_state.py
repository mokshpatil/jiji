from jiji.core.crypto import generate_keypair
from jiji.core.state import WorldState, Account
from jiji.core.transaction import Post, Endorse, Transfer, Coinbase


def make_keys():
    return generate_keypair()


class TestAccountBasics:
    def test_default_account(self):
        acct = Account()
        assert acct.balance == 0
        assert acct.nonce == 0

    def test_nonexistent_account_returns_none(self):
        state = WorldState()
        assert state.get_account(b"\x00" * 32) is None

    def test_get_or_create(self):
        state = WorldState()
        pub = b"\x01" * 32
        acct = state.get_or_create(pub)
        assert acct.balance == 0
        assert state.get_account(pub) is acct


class TestCoinbaseTransition:
    def test_credits_recipient(self):
        state = WorldState()
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=50, height=0)
        state.apply_transaction(cb, pub)
        assert state.get_account(pub).balance == 50

    def test_does_not_increment_nonce(self):
        state = WorldState()
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=50, height=0)
        state.apply_transaction(cb, pub)
        assert state.get_account(pub).nonce == 0


class TestPostTransition:
    def test_deducts_gas_credits_miner(self):
        priv, pub = make_keys()
        _, miner = make_keys()
        state = WorldState()
        state.get_or_create(pub).balance = 100
        post = Post(author=pub, nonce=0, timestamp=1000, body="hi", reply_to=None, gas_fee=5)
        post.sign_tx(priv)
        state.apply_transaction(post, miner)
        assert state.get_account(pub).balance == 95
        assert state.get_account(pub).nonce == 1
        assert state.get_account(miner).balance == 5

    def test_miner_is_author(self):
        # author mines their own post, gas goes back to self
        priv, pub = make_keys()
        state = WorldState()
        state.get_or_create(pub).balance = 100
        post = Post(author=pub, nonce=0, timestamp=1000, body="hi", reply_to=None, gas_fee=3)
        post.sign_tx(priv)
        state.apply_transaction(post, pub)
        assert state.get_account(pub).balance == 100  # -3 gas + 3 miner reward
        assert state.get_account(pub).nonce == 1


class TestEndorseTransition:
    def test_deducts_gas_and_tip(self):
        priv_a, pub_a = make_keys()
        _, pub_b = make_keys()
        _, miner = make_keys()
        state = WorldState()
        state.get_or_create(pub_a).balance = 50
        endorse = Endorse(author=pub_a, nonce=0, target=b"\x00" * 32, amount=10, message="nice", gas_fee=2)
        endorse.sign_tx(priv_a)
        state.apply_transaction(endorse, miner, target_author=pub_b)
        assert state.get_account(pub_a).balance == 38  # 50 - 2 - 10
        assert state.get_account(pub_a).nonce == 1
        assert state.get_account(miner).balance == 2
        assert state.get_account(pub_b).balance == 10

    def test_zero_tip(self):
        priv, pub = make_keys()
        _, miner = make_keys()
        state = WorldState()
        state.get_or_create(pub).balance = 10
        endorse = Endorse(author=pub, nonce=0, target=b"\x00" * 32, amount=0, message="", gas_fee=1)
        endorse.sign_tx(priv)
        state.apply_transaction(endorse, miner, target_author=None)
        assert state.get_account(pub).balance == 9


class TestTransferTransition:
    def test_moves_tokens(self):
        priv_a, pub_a = make_keys()
        _, pub_b = make_keys()
        _, miner = make_keys()
        state = WorldState()
        state.get_or_create(pub_a).balance = 100
        tx = Transfer(sender=pub_a, recipient=pub_b, amount=30, nonce=0, gas_fee=1)
        tx.sign_tx(priv_a)
        state.apply_transaction(tx, miner)
        assert state.get_account(pub_a).balance == 69
        assert state.get_account(pub_a).nonce == 1
        assert state.get_account(pub_b).balance == 30
        assert state.get_account(miner).balance == 1


class TestStateRoot:
    def test_deterministic(self):
        state = WorldState()
        _, pub = make_keys()
        state.get_or_create(pub).balance = 100
        assert state.state_root() == state.state_root()

    def test_empty_state(self):
        state = WorldState()
        root = state.state_root()
        assert len(root) == 32

    def test_different_states_different_roots(self):
        _, pub = make_keys()
        s1 = WorldState()
        s1.get_or_create(pub).balance = 100
        s2 = WorldState()
        s2.get_or_create(pub).balance = 200
        assert s1.state_root() != s2.state_root()


class TestStateCopy:
    def test_independent(self):
        _, pub = make_keys()
        state = WorldState()
        state.get_or_create(pub).balance = 100
        copy = state.copy()
        copy.get_or_create(pub).balance = 999
        assert state.get_account(pub).balance == 100

    def test_same_root(self):
        _, pub = make_keys()
        state = WorldState()
        state.get_or_create(pub).balance = 50
        copy = state.copy()
        assert state.state_root() == copy.state_root()
