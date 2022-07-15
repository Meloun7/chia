import asyncio
from secrets import token_bytes
from typing import List, Set, Optional

import pytest
from chia_rs import Coin

from chia.full_node.coin_store import CoinStore
from chia.full_node.singleton_store import SingletonStore, LAUNCHER_PUZZLE_HASH, MAX_REORG_SIZE
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.util.hash import std_hash
from chia.util.ints import uint64, uint32
from tests.util.db_connection import DBConnection


def test_coin() -> Coin:
    return Coin(token_bytes(32), std_hash(b"456"), uint64(1))


async def add_coins(height: int, coin_store: CoinStore, coins: List[Coin]) -> None:
    reward_coins = {test_coin() for i in range(2)}
    coin_names: Set[bytes32] = {c.name() for c in coins}
    removals: List[bytes32] = []
    for coin in coins:
        if coin.parent_coin_info in coin_names:
            removals.append(coin.parent_coin_info)
        else:
            cr: Optional[CoinRecord] = await coin_store.get_coin_record(coin.parent_coin_info)
            if cr is not None and not cr.spent:
                removals.append(coin.parent_coin_info)

    await coin_store.new_block(uint32(height), uint64(1000000 + height), reward_coins, coins, removals)


@pytest.mark.asyncio
async def test_basic_singleton_store(db_version):
    async with DBConnection(db_version) as db_wrapper:
        coin_store = await CoinStore.create(db_wrapper)
        store = SingletonStore(asyncio.Lock())

        # Create singletons
        launcher_coins, launcher_spends = [], []
        for i in range(10):
            launcher_coins.append(Coin(std_hash(i.to_bytes(4, "big")), LAUNCHER_PUZZLE_HASH, uint64(1)))
            launcher_spends.append(Coin(launcher_coins[-1].name(), std_hash(b"2"), uint64(1)))

        await add_coins(1, coin_store, launcher_coins)
        await add_coins(2, coin_store, launcher_spends)

        launcher_coins_2, launcher_spends_2 = [], []
        for i in range(10, 20):
            launcher_coins_2.append(Coin(std_hash(i.to_bytes(4, "big")), LAUNCHER_PUZZLE_HASH, uint64(1)))
            launcher_spends_2.append(Coin(launcher_coins_2[-1].name(), std_hash(b"2"), uint64(1)))

        await add_coins(3, coin_store, launcher_coins_2)
        await add_coins(4, coin_store, launcher_spends_2)
        await store.set_peak_height(uint32(4), set())

        for coin in launcher_spends + launcher_spends_2:
            cr = await coin_store.get_coin_record(coin.name())
            await store.add_state(coin.parent_coin_info, cr)
            # Already exists
            with pytest.raises(ValueError):
                await store.add_state(coin.parent_coin_info, cr)

        assert (await store.get_peak_height()) == 4

        # Get the latest state
        for lc, ls in zip(launcher_coins + launcher_coins_2, launcher_spends + launcher_spends_2):
            cr = await store.get_latest_coin_record_by_launcher_id(lc.name())
            assert cr.coin == ls

        state_updates = []
        for coin in launcher_spends + launcher_spends_2:
            state_updates.append(Coin(coin.name(), std_hash(b"2"), uint64(1)))

        await add_coins(uint32(6), coin_store, state_updates)

        # State not yet updated
        for n, lc in enumerate(launcher_coins + launcher_coins_2):
            cr = await store.get_latest_coin_record_by_launcher_id(lc.name())
            assert cr.name != state_updates[n].name()

        # Update store state
        await store.set_peak_height(uint32(6), set())
        for coin in state_updates:
            cr = await coin_store.get_coin_record(coin.name())
            launcher_id = (await coin_store.get_coin_record(cr.coin.parent_coin_info)).coin.parent_coin_info
            await store.add_state(launcher_id, cr)

        # Now it's updated
        for n, lc in enumerate(launcher_coins + launcher_coins_2):
            cr = await store.get_latest_coin_record_by_launcher_id(lc.name())
            assert cr.name == state_updates[n].name()

        # Remove a singleton
        await store.remove_singleton(launcher_coins[0].name())
        assert (await store.get_latest_coin_record_by_launcher_id(launcher_coins[0].name())) is None

        launcher_id = launcher_coins[1].name()
        assert store._singleton_history[launcher_id].last_non_recent_state is None
        assert len(store._singleton_history[launcher_id].recent_history) == 1

        latest_state_coin = state_updates[1]
        for height in range(7, 200):
            cr = await coin_store.get_coin_record(latest_state_coin.name())
            if height == 7:
                with pytest.raises(ValueError):
                    await store.add_state(launcher_id, cr)
            else:
                await store.add_state(launcher_id, cr)
            latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
            await add_coins(height, coin_store, [latest_state_coin])
            await store.set_peak_height(uint32(height), set())

        assert (await store.get_latest_coin_record_by_launcher_id(launcher_id)).confirmed_block_index == 198
        assert store.is_recent(uint32(200))
        assert not store.is_recent(uint32(60))

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is not None
        assert 110 >= len(info.recent_history) >= 99
        recent_cr = await coin_store.get_coin_record(info.recent_history[0][1])
        assert recent_cr.coin.parent_coin_info == info.last_non_recent_state[1]
        last_recent_cr = await coin_store.get_coin_record(info.recent_history[-1][1])
        assert info.latest_state.coin.parent_coin_info == last_recent_cr.name

        await store.set_peak_height(uint32(300), set(), False)
        new_info = store.get_all_singletons()[launcher_id]
        assert new_info.recent_history == info.recent_history

        for height in range(300, 350):
            await store.set_peak_height(uint32(height), set())
        new_new_info = store.get_all_singletons()[launcher_id]
        assert new_new_info.recent_history != info.recent_history

        assert len(store._singleton_history[launcher_id].recent_history) == 0

        await store.rollback(uint32(200), coin_store)
        assert len(store.get_all_singletons()[launcher_id].recent_history) == (198 - MAX_REORG_SIZE)

        await store.rollback(uint32(30), coin_store)
        assert len(store.get_all_singletons()[launcher_id].recent_history) == 25
        info = store.get_all_singletons()[launcher_id]
        last_recent_cr = await coin_store.get_coin_record(info.recent_history[-1][1])
        assert info.latest_state.coin.parent_coin_info == last_recent_cr.name

        await store.rollback(uint32(28), coin_store)
        assert len(store.get_all_singletons()[launcher_id].recent_history) == 23
        info = store.get_all_singletons()[launcher_id]
        last_recent_cr = await coin_store.get_coin_record(info.recent_history[-1][1])
        assert info.latest_state.coin.parent_coin_info == last_recent_cr.name

        await store.rollback(uint32(0), coin_store)
        assert len(store.get_all_singletons()) == 0

        # Test no update of recent
        # TODO: test a few more sub cases within rollback


@pytest.mark.asyncio
async def test_add_state(db_version):
    async with DBConnection(db_version) as db_wrapper:
        coin_store = await CoinStore.create(db_wrapper)
        store = SingletonStore(asyncio.Lock())

        launcher_coin = Coin(std_hash((123).to_bytes(4, "big")), LAUNCHER_PUZZLE_HASH, uint64(1))
        launcher_spend = Coin(launcher_coin.name(), std_hash(b"2"), uint64(1))
        launcher_id = launcher_coin.name()

        await add_coins(1, coin_store, [launcher_coin, launcher_spend])
        await store.set_peak_height(uint32(1), set())

        cr = await coin_store.get_coin_record(launcher_spend.name())
        await store.add_state(launcher_spend.parent_coin_info, cr)

        await store.set_peak_height(uint32(201), set())
        for h in range(10, 200, 10):
            latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
            await add_coins(h, coin_store, [latest_state_coin])
            cr = await coin_store.get_coin_record(latest_state_coin.name())
            await store.add_state(launcher_id, cr)

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is not None
        assert len(info.recent_history) == 8

        # Case 1: there is recent history, there is last non-recent state
        prev_cr = cr
        latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
        await add_coins(h, coin_store, [latest_state_coin])
        cr = await coin_store.get_coin_record(latest_state_coin.name())
        await store.add_state(launcher_id, cr)
        info = store.get_all_singletons()[launcher_id]
        assert (prev_cr.confirmed_block_index, prev_cr.name) in info.recent_history

        # Case 2: no recent history, there is last non-recent state, new recent
        await store.set_peak_height(uint32(301), set())
        for i in range(2):
            prev_cr = cr
            latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
            await add_coins(290 + i, coin_store, [latest_state_coin])
            cr = await coin_store.get_coin_record(latest_state_coin.name())
            await store.add_state(launcher_id, cr)
        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is not None
        assert [(prev_cr.confirmed_block_index, prev_cr.name)] == info.recent_history

        # Case 3: no recent history, there is last non-recent state, new is not recent
        await store.set_peak_height(uint32(501), set())
        for i in range(2):
            prev_cr = cr
            latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
            await add_coins(390 + i, coin_store, [latest_state_coin])
            cr = await coin_store.get_coin_record(latest_state_coin.name())
            await store.add_state(launcher_id, cr)
        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state == (prev_cr.confirmed_block_index, prev_cr.name)
        assert info.recent_history == []


@pytest.mark.asyncio
async def test_add_state_no_recent_no_lnrs(db_version):
    # Case 4: no recent or LNRS, new is recent
    async with DBConnection(db_version) as db_wrapper:
        coin_store = await CoinStore.create(db_wrapper)
        store = SingletonStore(asyncio.Lock())

        launcher_coin = Coin(std_hash((123).to_bytes(4, "big")), LAUNCHER_PUZZLE_HASH, uint64(1))
        launcher_spend = Coin(launcher_coin.name(), std_hash(b"2"), uint64(1))
        launcher_id = launcher_coin.name()

        await add_coins(1, coin_store, [launcher_coin, launcher_spend])
        await store.set_peak_height(uint32(1), set())

        cr = await coin_store.get_coin_record(launcher_spend.name())
        await store.add_state(launcher_spend.parent_coin_info, cr)

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is None
        assert len(info.recent_history) == 0

        await store.set_peak_height(uint32(MAX_REORG_SIZE), set())
        prev_cr = cr
        latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
        await add_coins(MAX_REORG_SIZE - 10, coin_store, [latest_state_coin])
        cr = await coin_store.get_coin_record(latest_state_coin.name())
        await store.add_state(launcher_id, cr)

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is None
        assert info.recent_history == [(prev_cr.confirmed_block_index, prev_cr.name)]


@pytest.mark.asyncio
async def test_add_state_no_recent_no_lrns_non_recent(db_version):
    # Case 5: no recent or LNRS, new is not recent
    async with DBConnection(db_version) as db_wrapper:
        coin_store = await CoinStore.create(db_wrapper)
        store = SingletonStore(asyncio.Lock())

        launcher_coin = Coin(std_hash((123).to_bytes(4, "big")), LAUNCHER_PUZZLE_HASH, uint64(1))
        launcher_spend = Coin(launcher_coin.name(), std_hash(b"2"), uint64(1))
        launcher_id = launcher_coin.name()

        await add_coins(1, coin_store, [launcher_coin, launcher_spend])
        await store.set_peak_height(uint32(1), set())

        cr = await coin_store.get_coin_record(launcher_spend.name())
        await store.add_state(launcher_spend.parent_coin_info, cr)

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is None
        assert len(info.recent_history) == 0

        await store.set_peak_height(uint32(MAX_REORG_SIZE + 50), set())
        prev_cr = cr
        latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
        await add_coins(MAX_REORG_SIZE + 40, coin_store, [latest_state_coin])
        cr = await coin_store.get_coin_record(latest_state_coin.name())
        await store.add_state(launcher_id, cr)

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state == (prev_cr.confirmed_block_index, prev_cr.name)
        assert info.recent_history == []


@pytest.mark.asyncio
async def test_add_state_recent_no_lnrs(db_version):
    # Case 4: no recent or LNRS, new is recent
    async with DBConnection(db_version) as db_wrapper:
        coin_store = await CoinStore.create(db_wrapper)
        store = SingletonStore(asyncio.Lock())

        launcher_coin = Coin(std_hash((123).to_bytes(4, "big")), LAUNCHER_PUZZLE_HASH, uint64(1))
        launcher_spend = Coin(launcher_coin.name(), std_hash(b"2"), uint64(1))
        launcher_id = launcher_coin.name()

        await store.set_peak_height(uint32(1), set())
        await add_coins(1, coin_store, [launcher_coin, launcher_spend])

        cr = await coin_store.get_coin_record(launcher_spend.name())
        await store.add_state(launcher_spend.parent_coin_info, cr)

        await store.set_peak_height(uint32(81), set())
        for h in range(10, 80, 10):
            latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
            await add_coins(h, coin_store, [latest_state_coin])
            cr = await coin_store.get_coin_record(latest_state_coin.name())
            await store.add_state(launcher_id, cr)

        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is None
        assert len(info.recent_history) == 7

        prev_cr = cr
        latest_state_coin = Coin(cr.name, std_hash(b"2"), uint64(1))
        await add_coins(81, coin_store, [latest_state_coin])
        cr = await coin_store.get_coin_record(latest_state_coin.name())
        await store.add_state(launcher_id, cr)
        info = store.get_all_singletons()[launcher_id]
        assert info.last_non_recent_state is None
        assert len(info.recent_history) == 8
