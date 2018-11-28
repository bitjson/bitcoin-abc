#!/usr/bin/env python3
# Copyright (c) 2018 The Bitcoin developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the finalizeblock RPC calls."""
import os

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, assert_raises_rpc_error, connect_nodes_bi, sync_blocks, wait_until

RPC_FINALIZE_INVALID_BLOCK_ERROR = 'finalize-invalid-block'
RPC_FORK_PRIOR_FINALIZED_ERROR = 'bad-fork-prior-finalized'
RPC_BLOCK_NOT_FOUND_ERROR = 'Block not found'
AUTO_FINALIZATION_DEPTH = 10


class FinalizeBlockTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 2
        self.extra_flags = [["-maxreorgdepth={}".format(AUTO_FINALIZATION_DEPTH)], [
            "-maxreorgdepth={}".format(AUTO_FINALIZATION_DEPTH)]]

    # There should only be one chaintip, which is expected_tip
    def only_valid_tip(self, expected_tip, other_tip_status=None):
        node = self.nodes[0]
        assert_equal(node.getbestblockhash(), expected_tip)
        for tip in node.getchaintips():
            if tip["hash"] == expected_tip:
                assert_equal(tip["status"], "active")
            else:
                assert_equal(tip["status"], other_tip_status)

    def run_test(self):
        node = self.nodes[0]

        self.log.info("Test block finalization...")
        node.generate(10)
        tip = node.getbestblockhash()
        node.finalizeblock(tip)
        assert_equal(node.getbestblockhash(), tip)
        assert_equal(node.getfinalizedblockhash(), tip)

        alt_node = self.nodes[1]
        connect_nodes_bi(self.nodes, 0, 1)
        sync_blocks(self.nodes[0:2])

        alt_node.invalidateblock(tip)
        # We will use this later
        fork_block = alt_node.getbestblockhash()

        # Node 0 should not accept the whole alt_node's chain due to tip being finalized,
        # even though it is longer.
        # Headers would not be accepted if previousblock is invalid:
        #    - First block from alt node has same height than node tip, but is on a minority chain. Its
        #    status is "valid-headers"
        #    - Second block from alt node has height > node tip height, will be marked as invalid because
        #    node tip is finalized
        #    - Later blocks from alt node will be rejected because their previous block are invalid
        #
        # Expected state:
        #
        # On alt_node:
        #                           >(210)->(211)-> // ->(218 tip)
        #                          /
        # (200)->(201)-> // ->(209)->(210 invalid)
        #
        # On node:
        #                           >(210 valid-headers)->(211 invalid)->(212 to 218 dropped)
        #                          /
        # (200)->(201)-> // ->(209)->(210 finalized, tip)

        def wait_for_block(node, block, status="invalid"):
            def check_block():
                for tip in node.getchaintips():
                    if tip["hash"] == block:
                        assert(tip["status"] != "active")
                        return tip["status"] == status
                return False
            wait_until(check_block)

        # First block header is accepted as valid-header
        alt_node.generate(1)
        wait_for_block(node, alt_node.getbestblockhash(), "valid-headers")

        # Second block header is accepted but set invalid
        alt_node.generate(1)
        invalid_block = alt_node.getbestblockhash()
        wait_for_block(node, invalid_block)

        # Later block headers are rejected
        for i in range(2, 9):
            alt_node.generate(1)
            assert_raises_rpc_error(-5, RPC_BLOCK_NOT_FOUND_ERROR,
                                    node.getblockheader, alt_node.getbestblockhash())

        assert_equal(node.getbestblockhash(), tip)
        assert_equal(node.getfinalizedblockhash(), tip)

        self.log.info("Test that an invalid block cannot be finalized...")
        assert_raises_rpc_error(-20, RPC_FINALIZE_INVALID_BLOCK_ERROR,
                                node.finalizeblock, invalid_block)

        self.log.info(
            "Test that invalidating a finalized block moves the finalization backward...")

        # Node's finalized block will be invalidated, which causes the finalized block to
        # move to the previous block.
        #
        # Expected state:
        #
        # On alt_node:
        #                                                 >(210)->(211)-> // ->(218 tip)
        #                                                /
        # (200)->(201)-> // ->(208 auto-finalized)->(209)->(210 invalid)
        #
        # On node:
        #                                     >(210 valid-headers)->(211 invalid)->(212 to 218 dropped)
        #                                    /
        # (200)->(201)-> // ->(209 finalized)->(210 tip)
        node.invalidateblock(tip)
        node.reconsiderblock(tip)

        assert_equal(node.getbestblockhash(), tip)
        assert_equal(node.getfinalizedblockhash(), fork_block)

        assert_equal(alt_node.getfinalizedblockhash(), node.getblockheader(
            node.getfinalizedblockhash())['previousblockhash'])

        # The node will now accept that chain as the finalized block moved back.
        # Generate a new block on alt_node to trigger getheader from node
        # Previous 212-218 height blocks have been droped because their previous was invalid
        #
        # Expected state:
        #
        # On alt_node:
        #                                          >(210)->(211)-> // ->(218)->(219 tip)
        #                                         /
        # (200)->(201)-> // ->(209 auto-finalized)->(210 invalid)
        #
        # On node:
        #                                     >(210)->(211)->(212)-> // ->(218)->(219 tip)
        #                                    /
        # (200)->(201)-> // ->(209 finalized)->(210)
        node.reconsiderblock(invalid_block)

        def wait_for_tip(node, tip):
            def check_tip():
                return node.getbestblockhash() == tip
            wait_until(check_tip)

        alt_node_tip = alt_node.generate(1)[-1]
        wait_for_tip(node, alt_node_tip)

        assert_equal(node.getbestblockhash(), alt_node.getbestblockhash())
        assert_equal(node.getfinalizedblockhash(), fork_block)
        assert_equal(alt_node.getfinalizedblockhash(), fork_block)

        self.log.info("Trigger reorg via block finalization...")
        # Finalize node tip to reorg
        #
        # Expected state:
        #
        # On alt_node:
        #                                          >(210)->(211)-> // ->(218)->(219 tip)
        #                                         /
        # (200)->(201)-> // ->(209 auto-finalized)->(210 invalid)
        #
        # On node:
        #                           >(210 invalid)-> // ->(219 invalid)
        #                          /
        # (200)->(201)-> // ->(209)->(210 finalized, tip)
        node.finalizeblock(tip)
        assert_equal(node.getfinalizedblockhash(), tip)

        self.log.info("Try to finalize a block on a competiting fork...")
        assert_raises_rpc_error(-20, RPC_FINALIZE_INVALID_BLOCK_ERROR,
                                node.finalizeblock, alt_node.getbestblockhash())
        assert_equal(node.getfinalizedblockhash(), tip)

        self.log.info(
            "Check auto-finalization occurs as the tip move forward...")
        # Reconsider alt_node tip then generate some more blocks on alt_node.
        # Auto-finalization will occur on both chains.
        #
        # Expected state:
        #
        # On alt_node:
        #                           >(210)->(211)-> // ->(219 auto-finalized)-> // ->(229 tip)
        #                          /
        # (200)->(201)-> // ->(209)->(210 invalid)
        #
        # On node:
        #                           >(210)->(211)-> // ->(219 auto-finalized)-> // ->(229 tip)
        #                          /
        # (200)->(201)-> // ->(209)->(210 invalid)
        node.reconsiderblock(alt_node.getbestblockhash())
        alt_node_new_tip = alt_node.generate(10)[-1]
        wait_for_tip(node, alt_node_new_tip)

        assert_equal(node.getbestblockhash(), alt_node.getbestblockhash())
        assert_equal(node.getfinalizedblockhash(), alt_node_tip)
        assert_equal(alt_node.getfinalizedblockhash(), alt_node_tip)

        self.log.info(
            "Try to finalize a block on an already finalized chain...")
        # Finalizing a block of an already finalized chain should have no effect
        block_218 = node.getblockheader(alt_node_tip)['previousblockhash']
        node.finalizeblock(block_218)
        assert_equal(node.getfinalizedblockhash(), alt_node_tip)

        self.log.info(
            "Make sure reconsidering block move the finalization point...")
        # Reconsidering the tip will move back the finalized block on node
        #
        # Expected state:
        #
        # On alt_node:
        #                           >(210)->(211)-> // ->(219 auto-finalized)-> // ->(229 tip)
        #                          /
        # (200)->(201)-> // ->(209)->(210 invalid)
        #
        # On node:
        #                                     >(210)->(211)-> // ->(219)-> // ->(229 tip)
        #                                    /
        # (200)->(201)-> // ->(209 finalized)->(210)
        node.reconsiderblock(tip)

        assert_equal(node.getbestblockhash(), alt_node_new_tip)
        assert_equal(node.getfinalizedblockhash(), fork_block)


if __name__ == '__main__':
    FinalizeBlockTest().main()