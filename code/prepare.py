import config
import logging
import bash
from cmd import dockercmd
import os
import utils
import math
from multiprocessing.dummy import Pool as ThreadPool
import itertools
from bitcoin.rpc import DEFAULT_HTTP_TIMEOUT
import node as node_utils
from itertools import islice


class Prepare:
    def __init__(self, context):
        self._context = context
        self._pool = None

    def execute(self):
        self._pool = ThreadPool(5)

        logging.info('Begin of prepare step')

        self.prepare_simulation_dir()

        remove_old_containers_if_exists()
        recreate_network()

        self.give_nodes_spendable_coins()

        self.start_nodes()

        self._pool.close()

        logging.info('End of prepare step')

    def give_nodes_spendable_coins(self):
        nodes = list(self._context.nodes.values())
        cbs = []
        for i, node in enumerate(nodes):
            cbs.append(
                self._pool.apply_async(
                    start_node,
                    args=(node,
                          DEFAULT_HTTP_TIMEOUT,
                          0,
                          (str(node.ip) for node in nodes[max(0, i - 5):i])
                          )
                )
            )
        for cb in cbs:
            cb.get()

        amount_of_tx_chains = calc_number_of_tx_chains(
            self._context.args.txs_per_tick,
            self._context.args.blocks_per_tick,
            len(nodes)
        )
        logging.info('Each node receives {} tx-chains'.format(amount_of_tx_chains))

        for i, node in enumerate(nodes):
            wait_until_height_reached(node, i * amount_of_tx_chains)
            node.execute_rpc('generate', amount_of_tx_chains)
            logging.info('Generated {} blocks for node={} for their tx-chains'.format(amount_of_tx_chains, node.name))

        wait_until_height_reached(nodes[0], amount_of_tx_chains * len(nodes))
        nodes[0].execute_cli('generate', config.blocks_needed_to_make_coinbase_spendable)
        current_height = config.blocks_needed_to_make_coinbase_spendable + amount_of_tx_chains * len(nodes)

        self._pool.starmap(wait_until_height_reached, zip(nodes, itertools.repeat(current_height)))

        self._pool.map(transfer_coinbase_tx_to_normal_tx, nodes)

        for i, node in enumerate(nodes):
            wait_until_height_reached(node, current_height + i)
            node.execute_rpc('generate', 1)

        current_height += len(nodes)
        self._context.first_block_height = current_height

        self._pool.starmap(wait_until_height_reached, zip(
                nodes,
                itertools.repeat(current_height)
        ))

        self._pool.map(node_utils.rm_peers_file, nodes)
        node_utils.graceful_rm(self._pool, nodes)

    def start_nodes(self):
        nodes = self._context.nodes.values()

        self._pool.starmap(start_node, zip(
            nodes,
            itertools.repeat(config.rpc_simulation_timeout),
            itertools.repeat(self._context.first_block_height)
        ))

        self._pool.starmap(add_latency, zip(
            self._context.nodes.values(),
            itertools.repeat(self._context.zone.zones)
        ))

        logging.info('All nodes for the simulation are started')
        utils.sleep(3 + len(self._context.nodes) * 0.2)

    def prepare_simulation_dir(self):
        if not os.path.exists(self._context.run_dir):
            os.makedirs(self._context.run_dir)

        if os.path.islink(config.soft_link_to_run_dir):
            bash.check_output('unlink {}'.format(config.soft_link_to_run_dir))
        bash.check_output('cd {}; ln -s {} {}'.format(config.data_dir, self._context.run_name, config.last_run))
        os.makedirs(config.postprocessing_dir)

        for file in [config.network_csv_file_name, config.ticks_csv_file_name,
                     config.nodes_csv_file_name, config.args_csv_file_name]:
            bash.check_output('cp {}{} {}'.format(config.data_dir, file, self._context.run_dir))
            bash.check_output('cd {}; ln -s ../{} {}'.format(config.postprocessing_dir, file, file))
        logging.info('Simulation directory created')


def start_node(node, timeout=DEFAULT_HTTP_TIMEOUT, height=0, connect_to_ips=None):
    node.run(connect_to_ips)
    node.connect_to_rpc(timeout)
    node.wait_until_rpc_ready()
    wait_until_height_reached(node, height)


def transfer_coinbase_tx_to_normal_tx(node):
    node.generate_spent_to_address()
    node.create_tx_chains()
    node.transfer_coinbases_to_normal_tx()
    logging.info("Transferred all coinbase-tx to normal tx for node={}".format(node.name))


def connect(node):
    node.connect()


def add_latency(node, zones):
    node.add_latency(zones)


def remove_old_containers_if_exists():
    containers = bash.check_output(dockercmd.ps_containers())
    if len(containers) > 0:
        bash.check_output(dockercmd.remove_all_containers(), lvl=logging.DEBUG)
        logging.info('Old containers removed')


def recreate_network():
    exit_code = bash.call_silent(dockercmd.inspect_network())
    if exit_code == 0:
        bash.check_output(dockercmd.rm_network())
    bash.check_output(dockercmd.create_network())
    logging.info('Docker network {} created'.format(config.network_name))
    utils.sleep(1)


def wait_until_height_reached(node, height):
    while int(node.execute_rpc('getblockcount')) < height:
        logging.debug('Waiting until node={} reached height={}...'.format(node.name, str(height)))
        utils.sleep(0.2)


def wait_until_height_reached_cli(node, height):
    msg = bash.check_output(
        "docker exec simcoin-{} bash -c '"
        "  while "
        "    [[ "
        "      $(bitcoin-cli "
        "        -regtest "
        "        --conf=/data/bitcoin.conf "
        "        getblockcount) -lt {} "
        "    ]]; "
        "    do sleep 0.2; "
        "done; "
        "echo Block Height reached'".format(node.name, str(height)))
    logging.debug('Waiting until {}'.format(str(msg)))


def calc_number_of_tx_chains(txs_per_tick, blocks_per_tick, number_of_nodes):
    txs_per_block = txs_per_tick / blocks_per_tick
    txs_per_block_per_node = txs_per_block / number_of_nodes

    # 10 times + 3 chains in reserve
    needed_tx_chains = (txs_per_block_per_node / config.max_in_mempool_ancestors) * 10 + 3

    return math.ceil(needed_tx_chains)
