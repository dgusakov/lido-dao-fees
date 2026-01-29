from web3 import Web3
from typing import List
from tqdm import tqdm
import os

# Replace with your Ethereum node provider
WEB3_PROVIDER = os.getenv("RPC_URL")
WEB3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER))

# The block from witch we fetch events. There should be at least one CSM Performance Oracle report after this block.
FROM_BLOCK = 23649467

CURATED_MODULE_ADDRESS = "0x55032650b14df07b85bF18A3a3eC8E0Af2e028d5"
CURATED_MODULE_ID = 1
SDVT_MODULE_ADDRESS = "0xaE7B191A31f627b4eB1d4DaC64eaB9976995b433"
SDVT_MODULE_ID = 2

CSM_FEE_DISTRIBUTOR_ADDRESS = "0xD99CC66fEC647E68294C6477B40fC7E0F6F618D0"
CS_MODULE_ID = 3

STAKING_ROUTER_ADDRESS = "0xFdDf38947aFB03C621C71b06C9C70bce73f12999"
STETH_ADDRESS = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"

with open("abi/nor_abi.json", "r") as file:
    NODE_OPERATORS_REGISTRY_ABI = file.read()
with open("abi/fee_distributor_abi.json", "r") as file:
    CSM_FEE_DISTRIBUTOR_ABI = file.read()
with open("abi/staking_router_abi.json", "r") as file:
    STAKING_ROUTER_ABI = file.read()

CURATED_SPECIAL = {
    "EE": [2, 3, 18, 30, 31],
    "ClientTeams": [21, 25, 26, 27, 28, 29, 33]
}
EE_FEE_PERCENT = 400
CLIENT_TEAMS_FEE_PERCENT = 450

SDVT_SUPER_CLUSTERS = [38, 39, 40, 41, 42, 43, 44, 45, 46, 47]
SUPER_CLUSTERS_FEE_PERCENT = 600


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


def get_csm_reports_data():
    fee_distributor = WEB3.eth.contract(address=WEB3.to_checksum_address(CSM_FEE_DISTRIBUTOR_ADDRESS), abi=CSM_FEE_DISTRIBUTOR_ABI)
    module_fees = fee_distributor.events.ModuleFeeDistributed().get_logs(from_block=FROM_BLOCK)
    rebates = fee_distributor.events.RebateTransferred().get_logs(from_block=FROM_BLOCK)
    data = []
    for i in range(len(module_fees)):
        assert module_fees[i].blockNumber == rebates[i].blockNumber, "Latest CSM report data mismatch"
        data.append([module_fees[i].args['shares'], rebates[i].args['shares'], module_fees[i].blockNumber])
    return data


def get_module_fee_percent(block_number, module_id):
    sr = WEB3.eth.contract(address=WEB3.to_checksum_address(STAKING_ROUTER_ADDRESS), abi=STAKING_ROUTER_ABI)
    module_data = sr.functions.getStakingModule(module_id).call(block_identifier=block_number)
    return module_data[2]


def get_module_active_keys(block_number, module_id):
    sr = WEB3.eth.contract(address=WEB3.to_checksum_address(STAKING_ROUTER_ADDRESS), abi=STAKING_ROUTER_ABI)
    active_keys = sr.functions.getStakingModuleActiveValidatorsCount(module_id).call(block_identifier=block_number)
    return active_keys


def calc_csm_dao_fee(module_fee_shares: int, rebate_shares: int, module_fee_on_sr: int) -> float:
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


def calc_curated_dao_fee(total_active: int, active_keys: List[int], module_fee_on_sr: int) -> float:
    if module_fee_on_sr == 500:
        return 5
    fee_accumulator = 0
    for no_id, keys in enumerate(active_keys):
        if no_id in CURATED_SPECIAL["EE"]:
            fee_accumulator += keys * EE_FEE_PERCENT
        elif no_id in CURATED_SPECIAL["ClientTeams"]:
            fee_accumulator += keys * CLIENT_TEAMS_FEE_PERCENT
        else:
            fee_accumulator += keys * module_fee_on_sr
    dao_fee_share = (1000 - (fee_accumulator / total_active)) / 100
    return dao_fee_share


def get_latest_fees_for_modules():
    print("Fetching CSM Oracle reports data...", end="", flush=True)
    csm_data = get_csm_reports_data()
    print("DONE")
    print(
        f"Fetched {len(csm_data)} CSM reports since block {FROM_BLOCK}, report blocks: {[data[2] for data in csm_data]}")

    print("Calculating CSM DAO fee shares...")
    csm_fee_percents = []
    csm_dao_fee_shares = []
    for item in tqdm(csm_data):
        csm_fee_percent = get_module_fee_percent(item[2], CS_MODULE_ID)
        csm_fee_percents.append(csm_fee_percent)
        csm_dao_fee_share = calc_csm_dao_fee(item[0], item[1], csm_fee_percent)
        csm_dao_fee_shares.append(csm_dao_fee_share)

    print("Calculating Curated DAO fee shares...")
    curated_contract = WEB3.eth.contract(address=WEB3.to_checksum_address(CURATED_MODULE_ADDRESS), abi=NODE_OPERATORS_REGISTRY_ABI)
    curated_dao_fee_shares = []
    for item in tqdm(csm_data):
        curated_fee_percent = get_module_fee_percent(item[2], CURATED_MODULE_ID)
        total_curated_active_keys, curated_active_keys = get_node_operators_active_keys(curated_contract, item[2])
        curated_dao_fee_share = calc_curated_dao_fee(total_curated_active_keys, curated_active_keys,
                                                     curated_fee_percent)
        curated_dao_fee_shares.append(curated_dao_fee_share)

    print("Calculating SDVT DAO fee shares...")
    sdvt_contract = WEB3.eth.contract(address=WEB3.to_checksum_address(SDVT_MODULE_ADDRESS), abi=NODE_OPERATORS_REGISTRY_ABI)
    sdvt_dao_fee_shares = []
    for item in tqdm(csm_data):
        sdvt_fee_percent = get_module_fee_percent(item[2], SDVT_MODULE_ID)
        total_sdvt_active_keys, sdvt_active_keys = get_node_operators_active_keys(sdvt_contract, item[2])
        sdvt_dao_fee_share = calc_curated_dao_fee(total_sdvt_active_keys, sdvt_active_keys, sdvt_fee_percent)
        sdvt_dao_fee_shares.append(sdvt_dao_fee_share)

    print("Calculating total DAO fee shares...")
    total_dao_fee_shares = []
    for i in tqdm(range(len(csm_data))):
        total_csm_active_keys = get_module_active_keys(csm_data[i][2], CS_MODULE_ID)
        total_curated_active_keys = get_module_active_keys(csm_data[i][2], CURATED_MODULE_ID)
        total_sdvt_active_keys = get_module_active_keys(csm_data[i][2], SDVT_MODULE_ID)
        total_dao_fee_share = (total_csm_active_keys * csm_dao_fee_shares[i] +
                               total_curated_active_keys * curated_dao_fee_shares[i] +
                               total_sdvt_active_keys * sdvt_dao_fee_shares[i]) / (total_csm_active_keys +
                                                                                   total_curated_active_keys +
                                                                                   total_sdvt_active_keys)
        total_dao_fee_shares.append(total_dao_fee_share)

    print("\n=============== DAO Fee Report ===============")
    print("|Block   |CSM     |Curated |SDVT    |Overall |")
    print("|--------|--------|--------|--------|--------|")
    for i in range(len(csm_data)):
        print(
            f"|{csm_data[i][2]}| {csm_dao_fee_shares[i]:.4f}%| {curated_dao_fee_shares[i]:.4f}%| {sdvt_dao_fee_shares[i]:.4f}%| {total_dao_fee_shares[i]:.4f}%|")


if __name__ == "__main__":
    get_latest_fees_for_modules()
