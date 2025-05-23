from typing import Optional, TYPE_CHECKING

from bittensor.core.errors import StakeError, NotRegisteredError
from bittensor.core.extrinsics.utils import get_old_stakes
from bittensor.utils import unlock_key
from bittensor.utils.balance import Balance
from bittensor.utils.btlogging import logging

if TYPE_CHECKING:
    from bittensor_wallet import Wallet
    from bittensor.core.subtensor import Subtensor


def unstake_extrinsic(
    subtensor: "Subtensor",
    wallet: "Wallet",
    hotkey_ss58: Optional[str] = None,
    netuid: Optional[int] = None,
    amount: Optional[Balance] = None,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
    safe_staking: bool = False,
    allow_partial_stake: bool = False,
    rate_tolerance: float = 0.005,
) -> bool:
    """Removes stake into the wallet coldkey from the specified hotkey ``uid``.

    Args:
        subtensor (bittensor.core.subtensor.Subtensor): Subtensor instance.
        wallet (bittensor_wallet.Wallet): Bittensor wallet object.
        hotkey_ss58 (Optional[str]): The ``ss58`` address of the hotkey to unstake from. By default, the wallet hotkey
            is used.
        netuid (Optional[int]): Subnet unique id.
        amount (Union[Balance]): Amount to stake as Bittensor balance.
        wait_for_inclusion (bool): If set, waits for the extrinsic to enter a block before returning ``True``, or
            returns ``False`` if the extrinsic fails to enter the block within the timeout.
        wait_for_finalization (bool): If set, waits for the extrinsic to be finalized on the chain before returning
            ``True``, or returns ``False`` if the extrinsic fails to be finalized within the timeout.
        safe_staking: If true, enables price safety checks
        allow_partial_stake: If true, allows partial unstaking if price tolerance exceeded
        rate_tolerance: Maximum allowed price decrease percentage (0.005 = 0.5%)

    Returns:
        success (bool): Flag is ``True`` if extrinsic was finalized or included in the block. If we did not wait for
            finalization / inclusion, the response is ``True``.
    """
    # Decrypt keys,
    if not (unlock := unlock_key(wallet)).success:
        logging.error(unlock.message)
        return False

    if hotkey_ss58 is None:
        hotkey_ss58 = wallet.hotkey.ss58_address  # Default to wallet's own hotkey.

    logging.info(
        f":satellite: [magenta]Syncing with chain:[/magenta] [blue]{subtensor.network}[/blue] [magenta]...[/magenta]"
    )
    block = subtensor.get_current_block()
    old_balance = subtensor.get_balance(wallet.coldkeypub.ss58_address, block=block)
    old_stake = subtensor.get_stake(
        coldkey_ss58=wallet.coldkeypub.ss58_address,
        hotkey_ss58=hotkey_ss58,
        netuid=netuid,
        block=block,
    )

    if amount is None:
        # Unstake it all.
        logging.warning(
            f"Didn't receive any unstaking amount. Unstaking all existing stake: [blue]{old_stake}[/blue] "
            f"from hotkey: [blue]{hotkey_ss58}[/blue]"
        )
        unstaking_balance = old_stake
    else:
        unstaking_balance = amount
    unstaking_balance.set_unit(netuid)

    # Check enough to unstake.
    stake_on_uid = old_stake
    if unstaking_balance > stake_on_uid:
        logging.error(
            f":cross_mark: [red]Not enough stake[/red]: [green]{stake_on_uid}[/green] to unstake: "
            f"[blue]{unstaking_balance}[/blue] from hotkey: [yellow]{wallet.hotkey_str}[/yellow]"
        )
        return False

    try:
        call_params = {
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount_unstaked": unstaking_balance.rao,
        }

        if safe_staking:
            pool = subtensor.subnet(netuid=netuid)
            base_price = pool.price.rao
            price_with_tolerance = base_price * (1 - rate_tolerance)

            # For logging
            base_rate = pool.price.tao
            rate_with_tolerance = base_rate * (1 - rate_tolerance)

            logging.info(
                f":satellite: [magenta]Safe Unstaking from:[/magenta] "
                f"netuid: [green]{netuid}[/green], amount: [green]{unstaking_balance}[/green], "
                f"tolerance percentage: [green]{rate_tolerance * 100}%[/green], "
                f"price limit: [green]{rate_with_tolerance}[/green], "
                f"original price: [green]{base_rate}[/green], "
                f"with partial unstake: [green]{allow_partial_stake}[/green] "
                f"on [blue]{subtensor.network}[/blue][magenta]...[/magenta]"
            )

            call_params.update(
                {
                    "limit_price": price_with_tolerance,
                    "allow_partial": allow_partial_stake,
                }
            )
            call_function = "remove_stake_limit"
        else:
            logging.info(
                f":satellite: [magenta]Unstaking from:[/magenta] "
                f"netuid: [green]{netuid}[/green], amount: [green]{unstaking_balance}[/green] "
                f"on [blue]{subtensor.network}[/blue][magenta]...[/magenta]"
            )
            call_function = "remove_stake"

        call = subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function=call_function,
            call_params=call_params,
        )

        staking_response, err_msg = subtensor.sign_and_send_extrinsic(
            call,
            wallet,
            wait_for_inclusion,
            wait_for_finalization,
            nonce_key="coldkeypub",
            sign_with="coldkey",
            use_nonce=True,
        )

        if staking_response is True:  # If we successfully unstaked.
            # We only wait here if we expect finalization.
            if not wait_for_finalization and not wait_for_inclusion:
                return True

            logging.success(":white_heavy_check_mark: [green]Finalized[/green]")

            logging.info(
                f":satellite: [magenta]Checking Balance on:[/magenta] [blue]{subtensor.network}[/blue] "
                f"[magenta]...[/magenta]"
            )
            block = subtensor.get_current_block()
            new_balance = subtensor.get_balance(
                wallet.coldkeypub.ss58_address, block=block
            )
            new_stake = subtensor.get_stake(
                coldkey_ss58=wallet.coldkeypub.ss58_address,
                hotkey_ss58=hotkey_ss58,
                netuid=netuid,
                block=block,
            )
            logging.info(
                f"Balance: [blue]{old_balance}[/blue] :arrow_right: [green]{new_balance}[/green]"
            )
            logging.info(
                f"Stake: [blue]{old_stake}[/blue] :arrow_right: [green]{new_stake}[/green]"
            )
            return True
        else:
            if safe_staking and "Custom error: 8" in err_msg:
                logging.error(
                    ":cross_mark: [red]Failed[/red]: Price exceeded tolerance limit. Either increase price tolerance or enable partial staking."
                )
            else:
                logging.error(f":cross_mark: [red]Failed: {err_msg}.[/red]")
            return False

    except NotRegisteredError:
        logging.error(
            f":cross_mark: [red]Hotkey: {wallet.hotkey_str} is not registered.[/red]"
        )
        return False
    except StakeError as e:
        logging.error(f":cross_mark: [red]Stake Error: {e}[/red]")
        return False


def unstake_multiple_extrinsic(
    subtensor: "Subtensor",
    wallet: "Wallet",
    hotkey_ss58s: list[str],
    netuids: list[int],
    amounts: Optional[list[Balance]] = None,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
) -> bool:
    """Removes stake from each ``hotkey_ss58`` in the list, using each amount, to a common coldkey.

    Args:
        subtensor (bittensor.core.subtensor.Subtensor): Subtensor instance.
        wallet (bittensor_wallet.Wallet): The wallet with the coldkey to unstake to.
        hotkey_ss58s (List[str]): List of hotkeys to unstake from.
        netuids (List[int]): List of subnets unique IDs to unstake from.
        amounts (List[Balance]): List of amounts to unstake. If ``None``, unstake all.
        wait_for_inclusion (bool): If set, waits for the extrinsic to enter a block before returning ``True``, or
            returns ``False`` if the extrinsic fails to enter the block within the timeout.
        wait_for_finalization (bool): If set, waits for the extrinsic to be finalized on the chain before returning
            ``True``, or returns ``False`` if the extrinsic fails to be finalized within the timeout.

    Returns:
        success (bool): Flag is ``True`` if extrinsic was finalized or included in the block. Flag is ``True`` if any
            wallet was unstaked. If we did not wait for finalization / inclusion, the response is ``True``.
    """

    if not isinstance(hotkey_ss58s, list) or not all(
        isinstance(hotkey_ss58, str) for hotkey_ss58 in hotkey_ss58s
    ):
        raise TypeError("hotkey_ss58s must be a list of str")

    if len(hotkey_ss58s) == 0:
        return True

    if netuids is not None and len(netuids) != len(hotkey_ss58s):
        raise ValueError("netuids must be a list of the same length as hotkey_ss58s")

    if amounts is not None and len(amounts) != len(hotkey_ss58s):
        raise ValueError("amounts must be a list of the same length as hotkey_ss58s")

    if amounts is not None and not all(
        isinstance(amount, Balance) for amount in amounts
    ):
        raise TypeError("amounts must be a [list of bittensor.Balance] or None")

    if amounts is None:
        amounts = [None] * len(hotkey_ss58s)
    else:
        # Convert to Balance
        amounts = [amount.set_unit(netuid) for amount, netuid in zip(amounts, netuids)]
        if sum(amount.tao for amount in amounts) == 0:
            # Staking 0 tao
            return True

    # Unlock coldkey.
    if not (unlock := unlock_key(wallet)).success:
        logging.error(unlock.message)
        return False

    logging.info(
        f":satellite: [magenta]Syncing with chain:[/magenta] [blue]{subtensor.network}[/blue] [magenta]...[/magenta]"
    )
    block = subtensor.get_current_block()
    old_balance = subtensor.get_balance(wallet.coldkeypub.ss58_address, block=block)
    all_stakes = subtensor.get_stake_for_coldkey(
        coldkey_ss58=wallet.coldkeypub.ss58_address
    )
    old_stakes = get_old_stakes(
        wallet=wallet, hotkey_ss58s=hotkey_ss58s, netuids=netuids, all_stakes=all_stakes
    )

    successful_unstakes = 0
    for idx, (hotkey_ss58, amount, old_stake, netuid) in enumerate(
        zip(hotkey_ss58s, amounts, old_stakes, netuids)
    ):
        # Convert to bittensor.Balance
        if amount is None:
            # Unstake it all.
            unstaking_balance = old_stake
            logging.warning(
                f"Didn't receive any unstaking amount. Unstaking all existing stake: [blue]{old_stake}[/blue] "
                f"from hotkey: [blue]{hotkey_ss58}[/blue]"
            )
        else:
            unstaking_balance = amount
            unstaking_balance.set_unit(netuid)

        # Check enough to unstake.
        if unstaking_balance > old_stake:
            logging.error(
                f":cross_mark: [red]Not enough stake[/red]: [green]{old_stake}[/green] to unstake: "
                f"[blue]{unstaking_balance}[/blue] from hotkey: [blue]{wallet.hotkey_str}[/blue]."
            )
            continue

        try:
            logging.info(
                f"Unstaking [blue]{unstaking_balance}[/blue] from [magenta]{hotkey_ss58}[/magenta] on [blue]{netuid}[/blue]"
            )
            call = subtensor.substrate.compose_call(
                call_module="SubtensorModule",
                call_function="remove_stake",
                call_params={
                    "hotkey": hotkey_ss58,
                    "amount_unstaked": unstaking_balance.rao,
                    "netuid": netuid,
                },
            )
            staking_response, err_msg = subtensor.sign_and_send_extrinsic(
                call,
                wallet,
                wait_for_inclusion,
                wait_for_finalization,
                nonce_key="coldkeypub",
                sign_with="coldkey",
                use_nonce=True,
            )

            if staking_response is True:  # If we successfully unstaked.
                # We only wait here if we expect finalization.

                if not wait_for_finalization and not wait_for_inclusion:
                    successful_unstakes += 1
                    continue

                logging.info(":white_heavy_check_mark: [green]Finalized[/green]")

                logging.info(
                    f":satellite: [magenta]Checking Balance on:[/magenta] [blue]{subtensor.network}[/blue] "
                    f"[magenta]...[/magenta]..."
                )
                new_stake = subtensor.get_stake(
                    coldkey_ss58=wallet.coldkeypub.ss58_address,
                    hotkey_ss58=hotkey_ss58,
                    netuid=netuid,
                    block=block,
                )
                logging.info(
                    f"Stake ({hotkey_ss58}) on netuid {netuid}: [blue]{old_stake}[/blue] :arrow_right: [green]{new_stake}[/green]"
                )
                successful_unstakes += 1
            else:
                logging.error(f":cross_mark: [red]Failed: {err_msg}.[/red]")
                continue

        except NotRegisteredError:
            logging.error(
                f":cross_mark: [red]Hotkey[/red] [blue]{hotkey_ss58}[/blue] [red]is not registered.[/red]"
            )
            continue
        except StakeError as e:
            logging.error(":cross_mark: [red]Stake Error: {}[/red]".format(e))
            continue

    if successful_unstakes != 0:
        logging.info(
            f":satellite: [magenta]Checking Balance on:[/magenta] ([blue]{subtensor.network}[/blue] "
            f"[magenta]...[/magenta]"
        )
        new_balance = subtensor.get_balance(wallet.coldkeypub.ss58_address)
        logging.info(
            f"Balance: [blue]{old_balance}[/blue] :arrow_right: [green]{new_balance}[/green]"
        )
        return True

    return False
