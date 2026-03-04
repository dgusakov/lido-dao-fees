# Lido DAO fees from staking modules calculator

## Requirements
- Python 3.8+

## Usage

- Install dependencies
```bash
pip install web3==6.0.0
```
- Prepare ENV
```bash
export RPC_URL="<your_rpc_url>"
```
- Add additional blocks to the `ADDITIONAL_BLOCKS` variable in the `lido_dao_fees.py` script if you want to calculate fees for more blocks than just CSM report blocks. Note that CSM fees will be used as of latest CSM report block.
```bash
python lido_dao_fees.py
```