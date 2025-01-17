#!/usr/bin/env python3
# Copyright (c) 2019 The Bitcoin developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-ilncense.php.

import time

from test_framework.test_framework import BitcoinTestFramework
from test_framework.mininode import network_thread_start, P2PDataStore
from test_framework.util import assert_equal
from test_framework.blocktools import (
    create_block,
    create_coinbase,
)


class InvalidChainsTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.tip = None
        self.blocks = {}
        self.block_heights = {}
        self.extra_args = [["-whitelist=127.0.0.1"]]

    def next_block(self, number):
        if self.tip == None:
            base_block_hash = self.genesis_hash
            block_time = int(time.time()) + 1
        else:
            base_block_hash = self.tip.sha256
            block_time = self.tip.nTime + 1

        height = self.block_heights[base_block_hash] + 1
        coinbase = create_coinbase(height)
        coinbase.rehash()
        block = create_block(base_block_hash, coinbase, block_time)

        block.solve()
        self.tip = block
        self.block_heights[block.sha256] = height
        assert number not in self.blocks
        self.blocks[number] = block
        return block

    def run_test(self):
        node = self.nodes[0]
        node.add_p2p_connection(P2PDataStore())
        network_thread_start()
        node.p2p.wait_for_verack()

        self.genesis_hash = int(node.getbestblockhash(), 16)
        self.block_heights[self.genesis_hash] = 0

        # move the tip back to a previous block
        def tip(number):
            self.tip = self.blocks[number]

        # shorthand for functions
        block = self.next_block

        # Reference for blocks mined in this test:
        #
        #       11  21   -- 221 - 222
        #      /   /    /
        # 0 - 1 - 2  - 22 - 23 - 24 - 25
        #     \
        #      -- 12 - 13 - 14

        # Generate some valid blocks
        node.p2p.send_blocks_and_test([block(0), block(1), block(2)], node)

        # Explicitly invalidate blocks 1 and 2
        # See below for why we do this
        node.invalidateblock(self.blocks[1].hash)
        assert_equal(self.blocks[0].hash, node.getbestblockhash())
        node.invalidateblock(self.blocks[2].hash)
        assert_equal(self.blocks[0].hash, node.getbestblockhash())

        # Mining on top of blocks 1 or 2 is rejected
        tip(1)
        node.p2p.send_blocks_and_test(
            [block(11)], node, success=False, reject_reason='bad-prevblk', request_block=False)

        tip(2)
        node.p2p.send_blocks_and_test(
            [block(21)], node, success=False, reject_reason='bad-prevblk', request_block=False)

        # Reconsider block 2 to remove invalid status from *both* 1 and 2
        # The goal is to test that block 1 is not retaining any internal state
        # that prevents us from accepting blocks building on top of block 1
        node.reconsiderblock(self.blocks[2].hash)
        assert_equal(self.blocks[2].hash, node.getbestblockhash())

        # Mining on the block 1 chain should be accepted
        # (needs to mine two blocks because less-work chains are not processed)
        tip(1)
        node.p2p.send_blocks_and_test([block(12), block(13)], node)

        # Mining on the block 2 chain should still be accepted
        # (needs to mine two blocks because less-work chains are not processed)
        tip(2)
        node.p2p.send_blocks_and_test([block(22), block(221)], node)

        # Mine more blocks from block 22 to be longest chain
        tip(22)
        node.p2p.send_blocks_and_test([block(23), block(24)], node)

        # Sanity checks
        assert_equal(self.blocks[24].hash, node.getbestblockhash())
        assert any(self.blocks[221].hash == chaintip["hash"]
                   for chaintip in node.getchaintips())

        # Invalidating the block 2 chain should reject new blocks on that chain
        node.invalidateblock(self.blocks[2].hash)
        assert_equal(self.blocks[13].hash, node.getbestblockhash())

        # Mining on the block 2 chain should be rejected
        tip(24)
        node.p2p.send_blocks_and_test(
            [block(25)], node, success=False, reject_reason='bad-prevblk', request_block=False)

        # Continued mining on the block 1 chain is still ok
        tip(13)
        node.p2p.send_blocks_and_test([block(14)], node)

        # Mining on a once-valid chain forking from block 2's longest chain,
        # which is now invalid, should also be rejected.
        tip(221)
        node.p2p.send_blocks_and_test(
            [block(222)], node, success=False, reject_reason='bad-prevblk', request_block=False)


if __name__ == '__main__':
    InvalidChainsTest().main()
