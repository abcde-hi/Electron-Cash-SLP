"""
Validate SLP token transactions with declared version 0x65.

This uses the graph searching mechanism from slp_dagging.py
"""

import threading
import queue

from .transaction import Transaction
from . import slp
from .slp import SlpMessage, SlpParsingError, SlpUnsupportedSlpTokenType, SlpInvalidOutputMessage
from .slp_dagging import TokenGraph, ValidationJob, ValidationJobManager
from .bitcoin import TYPE_SCRIPT
from .util import print_error
from .slp_validator_0x01 import Validator_SLP1

from . import slp_proxying # loading this module starts a thread.

### Uncomment one of the following options:

# Have a shared thread for validating all SLP token_ids sequentially
shared_jobmgr = ValidationJobManager(threadname="Validation_NFT1")

## Each token_id gets its own thread (thread spam?)
#shared_jobmgr = None

###


# Global db for shared graphs (each token_id_hex has its own graph).
graph_db_lock = threading.Lock()
graph_db = dict()   # token_id_hex -> (TokenGraph, ValidationJobManager)
def get_graph(token_id_hex, token_type):
    with graph_db_lock:
        try:
            return graph_db[token_id_hex]
        except KeyError:
            if shared_jobmgr:
                jobmgr = shared_jobmgr
            else:
                jobmgr = ValidationJobManager(threadname="Validation_NFT1_token_id_%.10s"%(token_id_hex,))

            if token_type == 129:
                val = Validator_SLP1(token_id_hex, enforced_token_type=129)
            elif token_type == 65:
                val = Validator_NFT1(token_id_hex, jobmgr)

            graph = TokenGraph(val)

            graph_db[token_id_hex] = (graph, jobmgr)

            return graph_db[token_id_hex]
def kill_graph(token_id_hex):
    try:
        graph, jobmgr = graph_db.pop(token_id_hex)
    except KeyError:
        return
    if jobmgr != shared_jobmgr:
        jobmgr.kill()
    graph.reset()

def setup_config(config_set):
    """ Called by main_window.py before wallet even gets loaded.

    - Limits on downloading DAG.
    - Proxy requests.
    """
    global proxy
    global config

    proxy = slp_proxying.tokengraph_proxy
    config = config_set


def setup_job(tx, reset=False):
    """ Perform setup steps before validation for a given transaction. """
    slpMsg = SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])

    if slpMsg.transaction_type == 'GENESIS':
        token_id_hex = tx.txid()
    elif slpMsg.transaction_type == 'SEND' or slpMsg.transaction_type == 'MINT':
        token_id_hex = slpMsg.op_return_fields['token_id_hex']
    else:
        return None

    if reset:
        try:
            kill_graph(token_id_hex)
        except KeyError:
            pass

    graph, jobmgr = get_graph(token_id_hex, slpMsg.token_type)
    
    return graph, jobmgr


def make_job(tx, wallet, network, *, debug=False, reset=False, callback_done=None, **kwargs):
    """
    Basic validation job maker for a single transaction.

    Creates job and starts it running in the background thread.

    Before calling this you have to call setup_config().

    Returns job, or None if it was not a validatable type
    """
    # This should probably be redone into a class, it is getting messy.

    try:
        limit_dls   = config.get('slp_validator_download_limit', None)
        limit_depth = config.get('slp_validator_depth_limit', None)
        proxy_enable = config.get('slp_validator_proxy_enabled', False)
    except NameError: # in daemon mode (no GUI) 'config' is not defined
        limit_dls = None
        limit_depth = None
        proxy_enable = False

    try:
        graph, jobmgr = setup_job(tx, reset=reset)
    except (SlpParsingError, IndexError):
        return

    graph.validator.wallet = wallet
    graph.validator.network = network

    txid = tx.txid()

    num_proxy_requests = 0
    proxyqueue = queue.Queue()

    def proxy_cb(txids, results):
        newres = {}
        # convert from 'true/false' to (True,1) or (False,3)
        for t,v in results.items():
            if v:
                newres[t] = (True, 1)
            else:
                newres[t] = (True, 3)
        proxyqueue.put(newres)

    def fetch_hook(txids):
        l = []
        for txid in txids:
            try:
                l.append(wallet.transactions[txid])
            except KeyError:
                pass
        if proxy_enable:
            proxy.add_job(txids, proxy_cb)
            nonlocal num_proxy_requests
            num_proxy_requests += 1
        return l

    job = ValidationJob(graph, [txid], network,
                        fetch_hook=fetch_hook,
                        validitycache=None, #wallet.slpv1_validity,
                        download_limit=limit_dls,
                        depth_limit=limit_depth,
                        debug=debug,
                        was_reset=reset,
                        **kwargs)
    def done_callback(job):
        # wait for proxy stuff to roll in
        results = {}
        try:
            for _ in range(num_proxy_requests):
                r = proxyqueue.get(timeout=5)
                results.update(r)
        except queue.Empty:
            pass

        if proxy_enable:
            graph.finalize_from_proxy(results)

        # Do consistency check here
        # XXXXXXX
        
        # Save validity
        for t,n in job.nodes.items():
            val = n.validity
            if val != 0:
                wallet.slpv1_validity[t] = val
    job.add_callback(done_callback)

    jobmgr.add_job(job)

    return job


class Validator_NFT1:
    prevalidation = True # indicate we want to check validation when some inputs still active.

    validity_states = {
        0: 'Unknown',
        1: 'Valid',
        2: 'Invalid: not SLP / malformed SLP',
        3: 'Invalid: insufficient valid inputs',
        4: 'Invalid: bad parent for child NFT'
        }

    def __init__(self, token_id_hex, jobmgr):
        self.token_id_hex = token_id_hex
        self.validation_jobmgr = jobmgr
        self.wallet = None
        self.network = None
        self.genesis_tx = None
        self.nft_parent_tx = None
        self.nft_child_job = None
        self.nft_parent_validity = 0

    def get_info(self,tx):
        """
        Enforce internal consensus rules (check all rules that don't involve
        information from inputs).

        Prune if mismatched token_id_hex from this validator or SLP version other than 65.
        """
        txouts = tx.outputs()
        if len(txouts) < 1:
            return ('prune', 2) # not SLP -- no outputs!

        # We take for granted that parseSlpOutputScript here will catch all
        # consensus-invalid op_return messages. In this procedure we check the
        # remaining internal rules, having to do with the overall transaction.
        try:
            slpMsg = SlpMessage.parseSlpOutputScript(txouts[0][1])
        except SlpUnsupportedSlpTokenType as e:
            # for unknown types: pruning as unknown has similar effect as pruning
            # invalid except it tells the validity cacher to not remember this
            # tx as 'bad'
            return ('prune', 0)
        except SlpInvalidOutputMessage as e:
            return ('prune', 2)

        # Parse the SLP
        if slpMsg.token_type not in [65]:
            return ('prune', 0)

        if slpMsg.transaction_type == 'SEND':
            token_id_hex = slpMsg.op_return_fields['token_id_hex']

            # need to examine all inputs
            vin_mask = (True,)*len(tx.inputs())

            # myinfo is the output sum
            # Note: according to consensus rules, we compute sum before truncating extra outputs.
        #    print("DEBUG SLP:getinfo %.10s outputs: %r"%(tx.txid(), slpMsg.op_return_fields['token_output']))
            myinfo = sum(slpMsg.op_return_fields['token_output'])

            # Cannot have more than 1 SLP output w/ child NFT (vout 0 op_return msg & vout 1 qty)
            if len(slpMsg.op_return_fields['token_output']) != 2:
                return ('prune', 2)

            # Cannot have quantity other than 1 as output at vout 1
            if slpMsg.op_return_fields['token_output'][1] != 1:
                return ('prune', 2)

            # outputs straight from the token amounts
            outputs = slpMsg.op_return_fields['token_output']
        elif slpMsg.transaction_type == 'GENESIS':
            token_id_hex = tx.txid()

            vin_mask = (False,)*len(tx.inputs()) # don't need to examine any inputs.

            myinfo = 'GENESIS'

            mintvout = slpMsg.op_return_fields['mint_baton_vout']
            if mintvout is not None:
                return ('prune', 2)
            decimals = slpMsg.op_return_fields['decimals']
            if decimals != 0:
                return ('prune', 2)
            outputs = [None,None]
            outputs[1] = slpMsg.op_return_fields['initial_token_mint_quantity']
            if outputs[1] > 1:
                return ('prune', 2)
        elif slpMsg.transaction_type == 'MINT':
            return ('prune', 2)
        elif slpMsg.transaction_type == 'COMMIT':
            return ('prune', 0)

        if token_id_hex != self.token_id_hex:
            return ('prune', 0)  # mismatched token_id_hex

        # truncate / expand outputs list to match tx outputs length
        outputs = tuple(outputs[:len(txouts)])
        outputs = outputs + (None,)*(len(txouts) - len(outputs))

        return vin_mask, myinfo, outputs


    def check_needed(self, myinfo, out_n):
        if myinfo == 'GENESIS':
            # genesis shouldn't have any parents, so this should not happen.
            raise RuntimeError('Unexpected', out_n)

        # TRAN txes are only interested in integer, non-zero input contributions.
        if out_n is None or out_n == 'MINT':
            return False
        else:
            return (out_n > 0)

    def download_nft_genesis(self, done_callback):
        def dl_cb(resp):
            if resp.get('error'):
                raise Exception(resp['error'].get('message'))
            raw = resp.get('result')
            tx = Transaction(raw)
            assert tx.txid() == self.token_id_hex
            wallet = self.wallet
            with wallet.lock, wallet.transaction_lock:
                if not wallet.transactions.get(tx.txid(), None):
                    wallet.transactions[tx.txid()] = tx
                if not wallet.tx_tokinfo.get(tx.txid(), None):
                    from .slp import SlpMessage
                    slpMsg = SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])
                    tti = { 'type':'SLP%d'%(slpMsg.token_type,),
                        'transaction_type':slpMsg.transaction_type,
                        'token_id': tx.txid(),
                        'validity': 0,
                    }
                    wallet.tx_tokinfo[tx.txid()] = tti
            wallet.save_transactions(True)
            self.genesis_tx = tx
            if done_callback:
                done_callback()
        requests = [('blockchain.transaction.get', [self.token_id_hex]), ]
        self.network.send(requests, dl_cb)

    def download_nft_parent_tx(self, done_callback):
        def dl_cb(resp):
            if resp.get('error'):
                raise Exception(resp['error'].get('message'))
            raw = resp.get('result')
            tx = Transaction(raw)
            wallet = self.wallet
            with wallet.lock, wallet.transaction_lock:
                if not wallet.transactions.get(tx.txid(), None):
                    wallet.transactions[tx.txid()] = tx
                if not wallet.tx_tokinfo.get(tx.txid(), None):
                    slpMsg = SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])
                    tti = { 'type':'SLP%d'%(slpMsg.token_type,),
                        'transaction_type':slpMsg.transaction_type,
                        'validity': 0,
                    }
                    if slpMsg.transaction_type == 'GENESIS':
                        tti['token_id'] = tx.txid()
                    else:
                        tti['token_id'] = slpMsg.op_return_fields['token_id_hex']
                    wallet.tx_tokinfo[tx.txid()] = tti
            wallet.save_transactions(True)
            self.nft_parent_tx = tx
            if done_callback:
                done_callback()
        nft_parent_txid = self.genesis_tx.inputs()[0]['prevout_hash']
        requests = [('blockchain.transaction.get', [nft_parent_txid]), ]
        self.network.send(requests, dl_cb)

    def start_NFT_parent_job(self, done_callback):
        wallet = self.wallet
        network = self.network
        def callback(job):
            (txid,node), = job.nodes.items()
            val = node.validity
            group_id = wallet.tx_tokinfo[self.nft_parent_tx.txid()]['token_id']                            
            if not wallet.token_types.get(group_id, None):
                name = wallet.token_types[self.genesis_tx.txid()]['name'] + '-parent'
                #decimals = SlpMessage.parseSlpOutputScript(wallet.transactions[group_id].outputs()[0][1]).op_return_fields['decimals']
                parent_entry = dict({'class':'SLP129','name':name,'decimals':0}) # TODO: handle case where decimals is not 0
                wallet.add_token_type(group_id, parent_entry)
            with wallet.lock, wallet.transaction_lock:
                wallet.token_types[self.genesis_tx.txid()]['group_id'] = group_id
                wallet.tx_tokinfo[self.nft_parent_tx.txid()]['validity'] = val
                wallet.tx_tokinfo[self.genesis_tx.txid()]['validity'] = val
            wallet.save_transactions(True)
            ui_cb = getattr(wallet, 'ui_emit_validity_updated', None)
            if ui_cb:
                ui_cb(txid, val)
                ui_cb(self.genesis_tx.txid(), val)
            if done_callback:
                done_callback(val)

        tx = self.nft_parent_tx
        from . import slp_validator_0x01_nft1
        job = slp_validator_0x01_nft1.make_job(tx, wallet, network, debug=self.nft_child_job.debug, reset=self.nft_child_job.was_reset)
        if job is not None:
            job.add_callback(callback)    

    def validate_NFT_parent(self, myinfo):
        self.nft_child_job = self.validation_jobmgr.job_current
        self.validation_jobmgr.pause_job(self.nft_child_job)

        def restart_nft_job(val):
            self.nft_parent_validity = val
            shared_jobmgr.unpause_job(self.nft_child_job)

        def start_nft_parent_validation():
            self.start_NFT_parent_job(restart_nft_job)

        def start_dl_nft_parent():
            self.download_nft_parent_tx(start_nft_parent_validation)

        self.download_nft_genesis(start_dl_nft_parent)

    def validate(self, myinfo, inputs_info):

        # NFT requires parent validation pre-valid phase
        if self.nft_parent_tx == None:
            self.validate_NFT_parent(myinfo)
            return None

        if myinfo == 'GENESIS':
            if len(inputs_info) != 0:
                raise RuntimeError('Unexpected', inputs_info)
            if self.nft_parent_validity == 1:
                return (True, 1)
            elif self.nft_parent_validity > 1:
                return (False, self.nft_parent_validity)
            return None
        else:
            # TRAN --- myinfo is an integer sum(outs)

            # Check whether from the unknown + valid inputs there could be enough to satisfy outputs.
            insum_all = sum(inp[2] for inp in inputs_info if inp[1] <= 1)
            if insum_all < myinfo:
                return (False, 3)

            # Check whether the known valid inputs provide enough tokens to satisfy outputs:
            insum_valid = sum(inp[2] for inp in inputs_info if inp[1] == 1)
            if insum_valid >= myinfo:
                return (True, 1)
            return None