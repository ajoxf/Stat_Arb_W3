"""Start one leg runner — one process, one MT5 account, one terminal.

    python run_leg.py --config config.json --account leg_a

Run one of these per account, on the Windows box with the terminals.
They stay up across coordinator restarts, and they must be reachable
with the coordinator DOWN: symbol search, test and diagnose all talk
straight to them.
"""

from okxtrader.leg_runner import main

if __name__ == '__main__':
    main()
