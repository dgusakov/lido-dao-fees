from web3 import Web3
from typing import List
from tqdm import tqdm
import os
import datetime

# Replace with your Ethereum node provider
WEB3_PROVIDER = os.getenv("RPC_URL")
WEB3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER))

EVENTS_FETCH_LIMIT = 10000  # Number of blocks to fetch events in one batch

# For pre-SRv3 blocks use old version from git history
# The block from witch we fetch events. There should be at least one CSM Performance Oracle report after this block.
FROM_BLOCK = 25656296

ADDITIONAL_BLOCKS = [25796694]

CURATED_MODULE_ADDRESS = "0x55032650b14df07b85bF18A3a3eC8E0Af2e028d5"
CURATED_MODULE_ID = 1

SDVT_MODULE_ADDRESS = "0xaE7B191A31f627b4eB1d4DaC64eaB9976995b433"
SDVT_MODULE_ID = 2

CSM_FEE_DISTRIBUTOR_ADDRESS = "0xD99CC66fEC647E68294C6477B40fC7E0F6F618D0"
CS_MODULE_ID = 3

CM_V2_FEE_DISTRIBUTOR_ADDRESS = "0x367d23c756599c20DCc8D6943F4976E8F88D60d7"
CURATED_MODULE_V2_ID = 4

STAKING_ROUTER_ADDRESS = "0xFdDf38947aFB03C621C71b06C9C70bce73f12999"
STETH_ADDRESS = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"

with open("abi/nor_abi.json", "r") as file:
    NODE_OPERATORS_REGISTRY_ABI = file.read()
with open("abi/fee_distributor_abi.json", "r") as file:
    FEE_DISTRIBUTOR_ABI = file.read()
with open("abi/staking_router_abi.json", "r") as file:
    STAKING_ROUTER_ABI = file.read()

CURATED_SPECIAL = {
    "EE": [0, 2, 4],
    "DO": [7, 22],
    "ClientTeams": [21, 25, 26, 27, 28, 29, 33]
}
EE_AND_DO_FEE_PERCENT = 400
CLIENT_TEAMS_FEE_PERCENT = 450
CLIENT_TEAMS_FEE_PERCENT_UPDATED = 400

CLIENT_TEAMS_FEE_UPDATE_BLOCK = 35656296 # TODO: Update this block number when the fee percent changes for client teams

SDVT_SUPER_CLUSTERS = [38, 39, 40, 41, 42, 43, 44, 45, 46, 47]
SUPER_CLUSTERS_FEE_PERCENT = 600


def get_latest_block() -> int:
    return WEB3.eth.block_number


def get_fees_and_rebates_logs(contract, from_block: int):
    module_fees = []
    rebates = []
    latest_block = get_latest_block()
    for block in range(from_block, latest_block + 1, EVENTS_FETCH_LIMIT):
        to_block = min(block + 9999, latest_block)
        module_fees.extend(contract.events.ModuleFeeDistributed().get_logs(from_block=block, to_block=to_block))
        rebates.extend(contract.events.RebateTransferred().get_logs(from_block=block, to_block=to_block))
    return module_fees, rebates


def get_block_date(block_number: int) -> str:
    block = WEB3.eth.get_block(block_number)
    return datetime.datetime.fromtimestamp(block.timestamp).strftime('%Y-%m-%d')


def get_node_operators_active_keys(contract, block_number: int) -> (int, List[int]):
    count = contract.functions.getNodeOperatorsCount().call(block_identifier=block_number)
    active_keys = []
    total_active = 0
    for no_id in range(count):
        operator = contract.functions.getNodeOperatorSummary(no_id).call(block_identifier=block_number)
        active = operator[6] - operator[5]
        total_active += active
        active_keys.append(active)
    return total_active, active_keys


def get_new_modules_reports_data(fee_distributor_address):
    fee_distributor = WEB3.eth.contract(address=WEB3.to_checksum_address(fee_distributor_address),
                                        abi=FEE_DISTRIBUTOR_ABI)
    module_fees, rebates = get_fees_and_rebates_logs(fee_distributor, FROM_BLOCK)
    data = []
    for i in range(len(module_fees)):
        rebate_found = False
        for j in range(len(rebates)):
            if module_fees[i].blockNumber == rebates[j].blockNumber:
                data.append([module_fees[i].args['shares'], rebates[j].args['shares'], module_fees[i].blockNumber])
                rebate_found = True
                break
        if not rebate_found:
            data.append([module_fees[i].args['shares'], 0, module_fees[i].blockNumber])
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


def get_module_fee_percent(block_number, module_id):
    sr = WEB3.eth.contract(address=WEB3.to_checksum_address(STAKING_ROUTER_ADDRESS), abi=STAKING_ROUTER_ABI)
    module_data = sr.functions.getStakingModule(module_id).call(block_identifier=block_number)
    return module_data[2]


def get_module_active_stake(block_number, module_id):
    sr = WEB3.eth.contract(address=WEB3.to_checksum_address(STAKING_ROUTER_ADDRESS), abi=STAKING_ROUTER_ABI)
    active_stake = sr.functions.getModuleValidatorsBalance(module_id).call(block_identifier=block_number)
    return active_stake


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


def get_latest_fees_for_modules():
    print("Fetching CSM Oracle reports data...", end="", flush=True)
    csm_data = get_new_modules_reports_data(CSM_FEE_DISTRIBUTOR_ADDRESS)
    print("DONE")
    print(
        f"Fetched {len(csm_data)} CSM reports since block {FROM_BLOCK}, report blocks: {[data[2] for data in csm_data]}")

    print("Fetching CMv2 Oracle reports data...", end="", flush=True)
    cm_v2_data = get_new_modules_reports_data(CM_V2_FEE_DISTRIBUTOR_ADDRESS)
    print("DONE")
    print(
        f"Fetched {len(cm_v2_data)} CMv2 reports since block {FROM_BLOCK}, report blocks: {[data[2] for data in cm_v2_data]}")

    print("Inserting additional blocks data...", end="", flush=True)
    total_data = []
    total_blocks = sorted(set([data[2] for data in csm_data] + [data[2] for data in cm_v2_data] + ADDITIONAL_BLOCKS))
    for block in total_blocks:
        total_data.append({"csm": get_new_modules_data_for_block(csm_data, block),
                           "cmv2": get_new_modules_data_for_block(cm_v2_data, block), "block": block})
    print("DONE")

    print("Calculating CSM DAO fee shares...")
    csm_fee_percents = []
    csm_dao_fee_shares = []
    for item in tqdm(total_data):
        data = item["csm"]
        csm_fee_percent = get_module_fee_percent(item["block"], CS_MODULE_ID)
        csm_fee_percents.append(csm_fee_percent)
        csm_dao_fee_share = calc_new_module_dao_fee(data[0], data[1], csm_fee_percent)
        csm_dao_fee_shares.append(csm_dao_fee_share)

    print("Calculating CMv2 DAO fee shares...")
    cmv2_fee_percents = []
    cmv2_dao_fee_shares = []
    for item in tqdm(total_data):
        data = item["cmv2"]
        cmv2_fee_percent = get_module_fee_percent(item["block"], CURATED_MODULE_V2_ID)
        cmv2_fee_percents.append(cmv2_fee_percent)
        cmv2_dao_fee_share = calc_new_module_dao_fee(data[0], data[1], cmv2_fee_percent)
        cmv2_dao_fee_shares.append(cmv2_dao_fee_share)

    print("Calculating Curated DAO fee shares...")
    curated_contract = WEB3.eth.contract(address=WEB3.to_checksum_address(CURATED_MODULE_ADDRESS),
                                         abi=NODE_OPERATORS_REGISTRY_ABI)
    curated_dao_fee_shares = []
    for item in tqdm(total_data):
        curated_fee_percent = get_module_fee_percent(item["block"], CURATED_MODULE_ID)
        total_curated_active_keys, curated_active_keys = get_node_operators_active_keys(curated_contract, item["block"])
        curated_dao_fee_share = calc_curated_dao_fee(total_curated_active_keys, curated_active_keys,
                                                     curated_fee_percent, item["block"])
        curated_dao_fee_shares.append(curated_dao_fee_share)

    print("Calculating SDVT DAO fee shares...")
    sdvt_contract = WEB3.eth.contract(address=WEB3.to_checksum_address(SDVT_MODULE_ADDRESS),
                                      abi=NODE_OPERATORS_REGISTRY_ABI)
    sdvt_dao_fee_shares = []
    for item in tqdm(total_data):
        sdvt_fee_percent = get_module_fee_percent(item["block"], SDVT_MODULE_ID)
        total_sdvt_active_keys, sdvt_active_keys = get_node_operators_active_keys(sdvt_contract, item["block"])
        sdvt_dao_fee_share = calc_sdvt_dao_fee(total_sdvt_active_keys, sdvt_active_keys, sdvt_fee_percent)
        sdvt_dao_fee_shares.append(sdvt_dao_fee_share)

    print("Calculating total DAO fee shares...")
    total_dao_fee_shares = []
    for i in tqdm(range(len(total_data))):
        total_csm_active_stake = get_module_active_stake(total_data[i]["block"], CS_MODULE_ID)
        total_curated_active_stake = get_module_active_stake(total_data[i]["block"], CURATED_MODULE_ID)
        total_sdvt_active_stake = get_module_active_stake(total_data[i]["block"], SDVT_MODULE_ID)
        total_cmv2_active_stake = get_module_active_stake(total_data[i]["block"], CURATED_MODULE_V2_ID)
        total_dao_fee_share = (total_csm_active_stake * csm_dao_fee_shares[i] +
                               total_curated_active_stake * curated_dao_fee_shares[i] +
                               total_sdvt_active_stake * sdvt_dao_fee_shares[i] +
                               total_cmv2_active_stake * cmv2_dao_fee_shares[i]) / (total_csm_active_stake +
                                                                                    total_curated_active_stake +
                                                                                    total_sdvt_active_stake +
                                                                                    total_cmv2_active_stake)
        total_dao_fee_shares.append(total_dao_fee_share)

    print("\n========================= DAO Fee Report =========================")
    print("|Block   |Date      |CSM     |CMv1    |CMv2    |SDVT    |Overall |")
    print("|--------|----------|--------|--------|--------|--------|--------|")
    for i in range(len(total_data)):
        if cmv2_dao_fee_shares[i] == 10:
            print(
                f"|{total_data[i]['block']}|{get_block_date(total_data[i]['block'])}|{csm_dao_fee_shares[i]:7.4f}%|{curated_dao_fee_shares[i]:7.4f}%|     N/A|{sdvt_dao_fee_shares[i]:7.4f}%|{total_dao_fee_shares[i]:7.4f}%|")
        else:
            print(
                f"|{total_data[i]['block']}|{get_block_date(total_data[i]['block'])}|{csm_dao_fee_shares[i]:7.4f}%|{curated_dao_fee_shares[i]:7.4f}%|{cmv2_dao_fee_shares[i]:7.4f}%|{sdvt_dao_fee_shares[i]:7.4f}%|{total_dao_fee_shares[i]:7.4f}%|")

if __name__ == "__main__":
    get_latest_fees_for_modules()
