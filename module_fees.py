from concurrent.futures import ThreadPoolExecutor
from web3 import Web3
from typing import List
from tqdm import tqdm
import json
import os
import datetime

# Replace with your Ethereum node provider
WEB3_PROVIDER = os.getenv("RPC_URL")
WEB3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER))

EVENTS_FETCH_LIMIT = 10000  # Number of blocks to fetch events in one batch
MULTICALL_BATCH_SIZE = 300  # Max number of calls packed into one Multicall3 eth_call
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))  # Number of blocks processed in parallel

# For pre-SRv3 blocks use old version from git history
# The block from witch we fetch events. There should be at least one CSM Performance Oracle report after this block.
FROM_BLOCK = 25656296

ADDITIONAL_BLOCKS = [25796694, 25878708, 26093737]

CURATED_MODULE_ADDRESS = "0x55032650b14df07b85bF18A3a3eC8E0Af2e028d5"
CURATED_MODULE_ID = 1

SDVT_MODULE_ADDRESS = "0xaE7B191A31f627b4eB1d4DaC64eaB9976995b433"
SDVT_MODULE_ID = 2

CSM_FEE_DISTRIBUTOR_ADDRESS = "0xD99CC66fEC647E68294C6477B40fC7E0F6F618D0"
CS_MODULE_ID = 3

CM_V2_FEE_DISTRIBUTOR_ADDRESS = "0x367d23c756599c20DCc8D6943F4976E8F88D60d7"
CURATED_MODULE_V2_ID = 4

CSM_0X02_FEE_DISTRIBUTOR_ADDRESS = "0x122CdeDD0da630B555050976052C262EA25f8335"
CS_MODULE_0X02_ID = 5

ALL_MODULE_IDS = [CURATED_MODULE_ID, SDVT_MODULE_ID, CS_MODULE_ID, CURATED_MODULE_V2_ID, CS_MODULE_0X02_ID]

STAKING_ROUTER_ADDRESS = "0xFdDf38947aFB03C621C71b06C9C70bce73f12999"
STETH_ADDRESS = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

with open("abi/nor_abi.json", "r") as file:
    NODE_OPERATORS_REGISTRY_ABI = file.read()
with open("abi/fee_distributor_abi.json", "r") as file:
    FEE_DISTRIBUTOR_ABI = file.read()
with open("abi/staking_router_abi.json", "r") as file:
    STAKING_ROUTER_ABI = file.read()
with open("abi/multicall3_abi.json", "r") as file:
    MULTICALL3_ABI = file.read()

STAKING_ROUTER = WEB3.eth.contract(address=WEB3.to_checksum_address(STAKING_ROUTER_ADDRESS), abi=STAKING_ROUTER_ABI)
CURATED_MODULE = WEB3.eth.contract(address=WEB3.to_checksum_address(CURATED_MODULE_ADDRESS),
                                   abi=NODE_OPERATORS_REGISTRY_ABI)
SDVT_MODULE = WEB3.eth.contract(address=WEB3.to_checksum_address(SDVT_MODULE_ADDRESS), abi=NODE_OPERATORS_REGISTRY_ABI)
MULTICALL3 = WEB3.eth.contract(address=WEB3.to_checksum_address(MULTICALL3_ADDRESS), abi=MULTICALL3_ABI)
# Used only to decode events, the logs are fetched for all fee distributors at once
FEE_DISTRIBUTOR = WEB3.eth.contract(abi=FEE_DISTRIBUTOR_ABI)

CURATED_SPECIAL = {
    "EE": [0, 2, 4],
    "DO": [7, 22],
    "ClientTeams": [21, 25, 26, 27, 28, 29, 33]
}
EE_AND_DO_FEE_PERCENT = 400
CLIENT_TEAMS_FEE_PERCENT = 450
CLIENT_TEAMS_FEE_PERCENT_UPDATED = 400

CLIENT_TEAMS_FEE_UPDATE_BLOCK = 25878707

SDVT_SUPER_CLUSTERS = [38, 39, 40, 41, 42, 43, 44, 45, 46, 47]
SUPER_CLUSTERS_FEE_PERCENT = 600


def get_latest_block() -> int:
    return WEB3.eth.block_number


def _abi_type(output: dict) -> str:
    if output["type"].startswith("tuple"):
        return "(" + ",".join(_abi_type(c) for c in output["components"]) + ")" + output["type"][len("tuple"):]
    return output["type"]


def multicall(calls, block_number: int, allow_failure: bool = False) -> list:
    """Executes bound contract function calls via Multicall3 in as few eth_calls as possible.
    Results are decoded the same way as `fn.call()` would. Failed calls (allow_failure=True) return None."""
    results = []
    for start in range(0, len(calls), MULTICALL_BATCH_SIZE):
        batch = calls[start:start + MULTICALL_BATCH_SIZE]
        raw = MULTICALL3.functions.aggregate3(
            [(fn.address, allow_failure, fn._encode_transaction_data()) for fn in batch]
        ).call(block_identifier=block_number)
        for fn, (success, data) in zip(batch, raw):
            if not success:
                results.append(None)
                continue
            decoded = WEB3.codec.decode([_abi_type(o) for o in fn.abi["outputs"]], data)
            results.append(decoded[0] if len(decoded) == 1 else decoded)
    return results


def _event_topic(name: str) -> str:
    event_abi = next(e for e in json.loads(FEE_DISTRIBUTOR_ABI) if e["type"] == "event" and e["name"] == name)
    signature = f"{name}({','.join(_abi_type(i) for i in event_abi['inputs'])})"
    return Web3.to_hex(Web3.keccak(text=signature))


def get_fees_and_rebates_logs(fee_distributor_addresses: List[str], from_block: int) -> dict:
    """Fetches ModuleFeeDistributed and RebateTransferred events of all fee distributors in one pass.
    Returns {address: (module_fees, rebates)}."""
    addresses = [WEB3.to_checksum_address(a) for a in fee_distributor_addresses]
    module_fee_topic = _event_topic("ModuleFeeDistributed")
    rebate_topic = _event_topic("RebateTransferred")
    result = {address: ([], []) for address in addresses}
    latest_block = get_latest_block()
    for block in range(from_block, latest_block + 1, EVENTS_FETCH_LIMIT):
        to_block = min(block + EVENTS_FETCH_LIMIT - 1, latest_block)
        logs = WEB3.eth.get_logs({"fromBlock": block, "toBlock": to_block, "address": addresses,
                                  "topics": [[module_fee_topic, rebate_topic]]})
        for log in logs:
            module_fees, rebates = result[WEB3.to_checksum_address(log["address"])]
            if Web3.to_hex(log["topics"][0]) == module_fee_topic:
                module_fees.append(FEE_DISTRIBUTOR.events.ModuleFeeDistributed().process_log(log))
            else:
                rebates.append(FEE_DISTRIBUTOR.events.RebateTransferred().process_log(log))
    return result


def get_new_modules_reports_data(fee_distributor_addresses: List[str]) -> dict:
    """Returns {address: [[module_fee_shares, rebate_shares, block], ...]} sorted by block."""
    logs = get_fees_and_rebates_logs(fee_distributor_addresses, FROM_BLOCK)
    data = {}
    for address, (module_fees, rebates) in logs.items():
        # First rebate in the block is matched with the module fee distribution of the same block
        rebates_by_block = {}
        for rebate in rebates:
            rebates_by_block.setdefault(rebate.blockNumber, rebate.args['shares'])
        data[address] = [[fee.args['shares'], rebates_by_block.get(fee.blockNumber, 0), fee.blockNumber]
                         for fee in module_fees]
    return data


# We assume reports are sorted
def get_new_modules_data_for_block(reports_data, block_number):
    reports_count = len(reports_data)
    # No reports at all, return empty data
    if reports_count == 0:
        return 0, 0

    # Before the first report block, return empty data
    if block_number < reports_data[0][2]:
        return 0, 0
    # After and at the last report block, return last report data
    if block_number >= reports_data[reports_count - 1][2]:
        return reports_data[reports_count - 1][0], reports_data[reports_count - 1][1]

    for i in range(1, len(reports_data)):
        if reports_data[i][2] > block_number:
            return reports_data[i - 1][0], reports_data[i - 1][1]


def get_block_state(block_number: int) -> dict:
    """Fetches all on-chain data needed for one report row in 2 multicalls.
    Modules not registered at the block get 0 fee and 0 active stake."""
    module_calls = ([STAKING_ROUTER.functions.getStakingModule(i) for i in ALL_MODULE_IDS] +
                    [STAKING_ROUTER.functions.getModuleValidatorsBalance(i) for i in ALL_MODULE_IDS])
    module_results = multicall(module_calls, block_number, allow_failure=True)
    n = len(ALL_MODULE_IDS)
    fees = {i: (m[2] if m is not None else 0) for i, m in zip(ALL_MODULE_IDS, module_results[:n])}
    stakes = {i: (s if s is not None else 0) for i, s in zip(ALL_MODULE_IDS, module_results[n:])}

    curated_count, sdvt_count, timestamp = multicall([CURATED_MODULE.functions.getNodeOperatorsCount(),
                                                      SDVT_MODULE.functions.getNodeOperatorsCount(),
                                                      MULTICALL3.functions.getCurrentBlockTimestamp()], block_number)

    summaries = multicall([CURATED_MODULE.functions.getNodeOperatorSummary(i) for i in range(curated_count)] +
                          [SDVT_MODULE.functions.getNodeOperatorSummary(i) for i in range(sdvt_count)], block_number)
    active_keys = [s[6] - s[5] for s in summaries]
    curated_keys, sdvt_keys = active_keys[:curated_count], active_keys[curated_count:]

    return {
        "fees": fees,
        "stakes": stakes,
        "curated_keys": (sum(curated_keys), curated_keys),
        "sdvt_keys": (sum(sdvt_keys), sdvt_keys),
        "date": datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d'),
    }


def calc_new_module_dao_fee(module_fee_shares: int, rebate_shares: int, module_fee_on_sr: int) -> float:
    if module_fee_shares == 0 and rebate_shares == 0:
        return 10
    return (1000 - module_fee_shares / ((module_fee_shares + rebate_shares) / module_fee_on_sr)) / 100


def calc_sdvt_dao_fee(total_active: int, active_keys: List[int], module_fee_on_sr: int) -> float:
    fee_accumulator = 0
    for no_id, keys in enumerate(active_keys):
        if no_id in SDVT_SUPER_CLUSTERS:
            fee_accumulator += keys * SUPER_CLUSTERS_FEE_PERCENT
        else:
            fee_accumulator += keys * module_fee_on_sr
    dao_fee_share = (1000 - (fee_accumulator / total_active)) / 100
    return dao_fee_share


def calc_curated_dao_fee(total_active: int, active_keys: List[int], module_fee_on_sr: int, block: int) -> float:
    if module_fee_on_sr == 500:
        return 5
    fee_accumulator = 0
    for no_id, keys in enumerate(active_keys):
        if no_id in CURATED_SPECIAL["EE"] or no_id in CURATED_SPECIAL["DO"]:
            fee_accumulator += keys * EE_AND_DO_FEE_PERCENT
        elif no_id in CURATED_SPECIAL["ClientTeams"]:
            if block > CLIENT_TEAMS_FEE_UPDATE_BLOCK:
                fee_accumulator += keys * CLIENT_TEAMS_FEE_PERCENT_UPDATED
            else:
                fee_accumulator += keys * CLIENT_TEAMS_FEE_PERCENT
        else:
            fee_accumulator += keys * module_fee_on_sr
    dao_fee_share = (1000 - (fee_accumulator / total_active)) / 100
    return dao_fee_share


def calc_block_row(item: dict) -> dict:
    block = item["block"]
    state = get_block_state(block)
    fees, stakes = state["fees"], state["stakes"]

    shares = {
        CS_MODULE_ID: calc_new_module_dao_fee(*item["csm"], fees[CS_MODULE_ID]),
        CURATED_MODULE_V2_ID: calc_new_module_dao_fee(*item["cmv2"], fees[CURATED_MODULE_V2_ID]),
        CS_MODULE_0X02_ID: calc_new_module_dao_fee(*item["csm_0x02"], fees[CS_MODULE_0X02_ID]),
        CURATED_MODULE_ID: calc_curated_dao_fee(*state["curated_keys"], fees[CURATED_MODULE_ID], block),
        SDVT_MODULE_ID: calc_sdvt_dao_fee(*state["sdvt_keys"], fees[SDVT_MODULE_ID]),
    }
    total = sum(stakes[i] * shares[i] for i in ALL_MODULE_IDS) / sum(stakes[i] for i in ALL_MODULE_IDS)
    return {"block": block, "date": state["date"], "shares": shares, "total": total}


def get_latest_fees_for_modules():
    print("Fetching CSM, CMv2 and 0x02 CSM Oracle reports data...", end="", flush=True)
    reports = get_new_modules_reports_data([CSM_FEE_DISTRIBUTOR_ADDRESS, CM_V2_FEE_DISTRIBUTOR_ADDRESS,
                                            CSM_0X02_FEE_DISTRIBUTOR_ADDRESS])
    csm_data = reports[WEB3.to_checksum_address(CSM_FEE_DISTRIBUTOR_ADDRESS)]
    cm_v2_data = reports[WEB3.to_checksum_address(CM_V2_FEE_DISTRIBUTOR_ADDRESS)]
    csm_0x02_data = reports[WEB3.to_checksum_address(CSM_0X02_FEE_DISTRIBUTOR_ADDRESS)]
    print("DONE")
    print(
        f"Fetched {len(csm_data)} CSM reports since block {FROM_BLOCK}, report blocks: {[data[2] for data in csm_data]}")
    print(
        f"Fetched {len(cm_v2_data)} CMv2 reports since block {FROM_BLOCK}, report blocks: {[data[2] for data in cm_v2_data]}")
    print(
        f"Fetched {len(csm_0x02_data)} 0x02 CSM reports since block {FROM_BLOCK}, report blocks: {[data[2] for data in csm_0x02_data]}")

    print("Inserting additional blocks data...", end="", flush=True)
    total_data = []
    total_blocks = sorted(set([data[2] for data in csm_data] + [data[2] for data in cm_v2_data] +
                              [data[2] for data in csm_0x02_data] + ADDITIONAL_BLOCKS))
    for block in total_blocks:
        total_data.append({"csm": get_new_modules_data_for_block(csm_data, block),
                           "cmv2": get_new_modules_data_for_block(cm_v2_data, block),
                           "csm_0x02": get_new_modules_data_for_block(csm_0x02_data, block), "block": block})
    print("DONE")

    print("Calculating DAO fee shares...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        rows = list(tqdm(executor.map(calc_block_row, total_data), total=len(total_data)))

    # New modules without any oracle report yet have the default 10% DAO fee and are shown as N/A
    def fmt_new_module(share: float) -> str:
        return "     N/A" if share == 10 else f"{share:7.4f}%"

    print("\n============================== DAO Fee Report ==============================")
    print("|Block   |Date      |CSM     |CMv1    |CMv2    |0x02 CSM|SDVT    |Overall |")
    print("|--------|----------|--------|--------|--------|--------|--------|--------|")
    for row in rows:
        s = row["shares"]
        print(
            f"|{row['block']}|{row['date']}|{s[CS_MODULE_ID]:7.4f}%|{s[CURATED_MODULE_ID]:7.4f}%|{fmt_new_module(s[CURATED_MODULE_V2_ID])}|{fmt_new_module(s[CS_MODULE_0X02_ID])}|{s[SDVT_MODULE_ID]:7.4f}%|{row['total']:7.4f}%|")


if __name__ == "__main__":
    get_latest_fees_for_modules()
