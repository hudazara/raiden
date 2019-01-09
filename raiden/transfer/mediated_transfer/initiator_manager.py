import random

from raiden.transfer import channel
from raiden.transfer.architecture import Event, StateChange, TransitionResult
from raiden.transfer.events import EventPaymentSentFailed
from raiden.transfer.mediated_transfer import initiator
from raiden.transfer.mediated_transfer.events import EventUnlockClaimFailed, EventUnlockFailed
from raiden.transfer.mediated_transfer.state import (
    InitiatorPaymentState,
    InitiatorTransferState,
    TransferDescriptionWithSecretState,
)
from raiden.transfer.mediated_transfer.state_change import (
    ActionInitInitiator,
    ReceiveLockExpired,
    ReceiveSecretRequest,
    ReceiveSecretReveal,
    ReceiveTransferRefundCancelRoute,
)
from raiden.transfer.state import RouteState
from raiden.transfer.state_change import ActionCancelPayment, Block, ContractReceiveSecretReveal
from raiden.utils.typing import BlockNumber, ChannelMap, List, cast


def clear_if_finalized(iteration: TransitionResult) -> TransitionResult:
    """ Clear the initiator payment task if all transfers have been finalized
    or expired. """
    state = cast(InitiatorPaymentState, iteration.new_state)

    if state is None:
        return iteration

    if len(state.initiator_transfers) == 0:
        return TransitionResult(None, iteration.events)

    return iteration


def can_cancel(initiator: InitiatorTransferState) -> bool:
    """ A transfer is only cancellable until the secret is revealed. """
    return (
        initiator is None or
        initiator.revealsecret is None
    )


def events_for_cancel_current_route(transfer_description) -> List[Event]:
    unlock_failed = EventUnlockFailed(
        identifier=transfer_description.payment_identifier,
        secrethash=transfer_description.secrethash,
        reason='route was canceled',
    )
    return [unlock_failed]


def cancel_current_route(
        payment_state: InitiatorPaymentState,
        initiator_state: InitiatorPaymentState,
) -> List[Event]:
    """ Cancel current route.

    This allows a new route to be tried.
    """
    assert can_cancel(initiator_state), 'Cannot cancel a route after the secret is revealed'

    transfer_description = initiator_state.transfer_description

    payment_state.cancelled_channels.append(initiator_state.channel_identifier)

    return events_for_cancel_current_route(transfer_description)


def maybe_try_new_route(
        payment_state: InitiatorPaymentState,
        initiator_state: InitiatorTransferState,
        transfer_description: TransferDescriptionWithSecretState,
        available_routes: List[RouteState],
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    events: List[Event] = list()
    if can_cancel(initiator_state):
        cancel_events = cancel_current_route(payment_state, initiator_state)

        sub_iteration = initiator.try_new_route(
            old_initiator_state=initiator_state,
            channelidentifiers_to_channels=channelidentifiers_to_channels,
            available_routes=available_routes,
            transfer_description=transfer_description,
            pseudo_random_generator=pseudo_random_generator,
            block_number=block_number,
        )

        events.extend(cancel_events)
        events.extend(sub_iteration.events)
        assert sub_iteration.new_state

        new_transfer = sub_iteration.new_state.transfer
        payment_state.initiator_transfers[new_transfer.lock.secrethash] = sub_iteration.new_state

    iteration = TransitionResult(payment_state, events)

    return iteration


def subdispatch_to_initiatortransfer(
        initiator_state: InitiatorTransferState,
        state_change: StateChange,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    channel_identifier = initiator_state.channel_identifier
    channel_state = channelidentifiers_to_channels[channel_identifier]
    if not channel_state:
        return TransitionResult(initiator_state, list())

    return initiator.state_transition(
        initiator_state=initiator_state,
        state_change=state_change,
        channel_state=channel_state,
        pseudo_random_generator=pseudo_random_generator,
        block_number=block_number,
    )


def subdispatch_to_all_initiatortransfer(
        payment_state: InitiatorPaymentState,
        state_change: StateChange,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    events = list()
    for secrethash in list(payment_state.initiator_transfers.keys()):
        initiator_state = payment_state.initiator_transfers[secrethash]
        channel_identifier = initiator_state.channel_identifier
        channel_state = channelidentifiers_to_channels.get(channel_identifier)
        if not channel_state:
            continue

        sub_iteration = initiator.state_transition(
            initiator_state=initiator_state,
            state_change=state_change,
            channel_state=channel_state,
            pseudo_random_generator=pseudo_random_generator,
            block_number=block_number,
        )
        if sub_iteration.new_state is None:
            del payment_state.initiator_transfers[secrethash]
        events.extend(sub_iteration.events)
    return TransitionResult(payment_state, events)


def handle_block(
        payment_state: InitiatorPaymentState,
        state_change: Block,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    return subdispatch_to_all_initiatortransfer(
        payment_state=payment_state,
        state_change=state_change,
        channelidentifiers_to_channels=channelidentifiers_to_channels,
        pseudo_random_generator=pseudo_random_generator,
        block_number=block_number,
    )


def handle_init(
        payment_state: InitiatorPaymentState,
        state_change: ActionInitInitiator,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    events: List[Event]
    if payment_state is None:
        sub_iteration = initiator.try_new_route(
            old_initiator_state=None,
            channelidentifiers_to_channels=channelidentifiers_to_channels,
            available_routes=state_change.routes,
            transfer_description=state_change.transfer,
            pseudo_random_generator=pseudo_random_generator,
            block_number=block_number,
        )

        events = sub_iteration.events
        if sub_iteration.new_state:
            payment_state = InitiatorPaymentState(
                initiator_transfers={
                    sub_iteration.new_state.transfer.lock.secrethash: sub_iteration.new_state,
                },
            )
    else:
        events = list()

    iteration = TransitionResult(payment_state, events)
    return iteration


def handle_cancelpayment(
        payment_state: InitiatorPaymentState,
        channelidentifiers_to_channels: ChannelMap,
) -> TransitionResult:
    """ Cancel the payment and all related transfers. """
    # Cannot cancel a transfer after the secret is revealed
    events = list()
    for secrethash in list(payment_state.initiator_transfers.keys()):
        initiator_state = payment_state.initiator_transfers[secrethash]
        channel_identifier = initiator_state.channel_identifier
        channel_state = channelidentifiers_to_channels[channel_identifier]
        if can_cancel(initiator_state):
            transfer_description = initiator_state.transfer_description
            cancel_events = cancel_current_route(payment_state, initiator_state)

            del payment_state.initiator_transfers[secrethash]

            cancel = EventPaymentSentFailed(
                payment_network_identifier=channel_state.payment_network_identifier,
                token_network_identifier=channel_state.token_network_identifier,
                identifier=transfer_description.payment_identifier,
                target=transfer_description.target,
                reason='user canceled payment',
            )
            cancel_events.append(cancel)

            events.extend(cancel_events)

    return TransitionResult(payment_state, events)


def handle_transferrefundcancelroute(
        payment_state: InitiatorPaymentState,
        state_change: ReceiveTransferRefundCancelRoute,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    initiator_state = payment_state.initiator_transfers.get(state_change.transfer.lock.secrethash)
    if not initiator_state:
        return TransitionResult(payment_state, list())

    channel_identifier = initiator_state.channel_identifier
    channel_state = channelidentifiers_to_channels[channel_identifier]
    refund_transfer = state_change.transfer
    original_transfer = initiator_state.transfer

    is_valid_lock = (
        refund_transfer.lock.secrethash == original_transfer.lock.secrethash and
        refund_transfer.lock.amount == original_transfer.lock.amount and
        refund_transfer.lock.expiration == original_transfer.lock.expiration
    )

    is_valid_refund = channel.refund_transfer_matches_received(
        refund_transfer,
        original_transfer,
    )

    events = list()
    if not is_valid_lock or not is_valid_refund:
        return TransitionResult(payment_state, list())

    is_valid, channel_events, _ = channel.handle_receive_refundtransfercancelroute(
        channel_state,
        refund_transfer,
    )

    events.extend(channel_events)

    if not is_valid:
        return TransitionResult(payment_state, list())

    old_description = initiator_state.transfer_description
    transfer_description = TransferDescriptionWithSecretState(
        old_description.payment_network_identifier,
        old_description.payment_identifier,
        old_description.amount,
        old_description.token_network_identifier,
        old_description.initiator,
        old_description.target,
        state_change.secret,
    )

    sub_iteration = maybe_try_new_route(
        payment_state=payment_state,
        initiator_state=initiator_state,
        transfer_description=transfer_description,
        available_routes=state_change.routes,
        channelidentifiers_to_channels=channelidentifiers_to_channels,
        pseudo_random_generator=pseudo_random_generator,
        block_number=block_number,
    )

    events.extend(sub_iteration.events)

    iteration = TransitionResult(payment_state, events)

    return iteration


def handle_lock_expired(
        payment_state: InitiatorPaymentState,
        state_change: ReceiveLockExpired,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    """Initiator also needs to handle LockExpired messages when refund transfers are involved.

    A -> B -> C

    - A sends locked transfer to B
    - B attempted to forward to C but has not enough capacity
    - B sends a refund transfer with the same secrethash back to A
    - When the lock expires B will also send a LockExpired message to A
    - A needs to be able to properly process it

    Related issue: https://github.com/raiden-network/raiden/issues/3183
"""
    initiator_state = payment_state.initiator_transfers.get(state_change.secrethash)
    if not initiator_state:
        return TransitionResult(payment_state, list())

    channel_identifier = initiator_state.channel_identifier
    channel_state = channelidentifiers_to_channels[channel_identifier]
    secrethash = initiator_state.transfer.lock.secrethash
    result = channel.handle_receive_lock_expired(
        channel_state=channel_state,
        state_change=state_change,
        block_number=block_number,
    )

    if not channel.get_lock(result.new_state.partner_state, secrethash):
        transfer = initiator_state.transfer
        unlock_failed = EventUnlockClaimFailed(
            identifier=transfer.payment_identifier,
            secrethash=transfer.lock.secrethash,
            reason='Lock expired',
        )
        result.events.append(unlock_failed)

    return TransitionResult(payment_state, result.events)


def handle_offchain_secretreveal(
        payment_state: InitiatorPaymentState,
        state_change: ReceiveSecretReveal,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    return subdispatch_to_all_initiatortransfer(
        payment_state=payment_state,
        state_change=state_change,
        channelidentifiers_to_channels=channelidentifiers_to_channels,
        pseudo_random_generator=pseudo_random_generator,
        block_number=block_number,
    )


def handle_onchain_secretreveal(
        payment_state: InitiatorPaymentState,
        state_change: ReceiveSecretReveal,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    return subdispatch_to_all_initiatortransfer(
        payment_state=payment_state,
        state_change=state_change,
        channelidentifiers_to_channels=channelidentifiers_to_channels,
        pseudo_random_generator=pseudo_random_generator,
        block_number=block_number,
    )


def handle_secretrequest(
        payment_state: InitiatorPaymentState,
        state_change: ReceiveSecretRequest,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    initiator_state = payment_state.initiator_transfers.get(state_change.secrethash)

    if not initiator_state:
        return TransitionResult(payment_state, list())

    sub_iteration = subdispatch_to_initiatortransfer(
        initiator_state=initiator_state,
        state_change=state_change,
        channelidentifiers_to_channels=channelidentifiers_to_channels,
        pseudo_random_generator=pseudo_random_generator,
        block_number=block_number,
    )

    return TransitionResult(payment_state, sub_iteration.events)


def state_transition(
        payment_state: InitiatorPaymentState,
        state_change: StateChange,
        channelidentifiers_to_channels: ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: BlockNumber,
) -> TransitionResult:
    # pylint: disable=unidiomatic-typecheck
    if type(state_change) == Block:
        iteration = handle_block(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    elif type(state_change) == ActionInitInitiator:
        iteration = handle_init(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    elif type(state_change) == ReceiveSecretRequest:
        iteration = handle_secretrequest(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    elif type(state_change) == ReceiveTransferRefundCancelRoute:
        iteration = handle_transferrefundcancelroute(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    elif type(state_change) == ActionCancelPayment:
        iteration = handle_cancelpayment(
            payment_state,
            channelidentifiers_to_channels,
        )
    elif type(state_change) == ReceiveSecretReveal:
        iteration = handle_offchain_secretreveal(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    elif type(state_change) == ReceiveLockExpired:
        iteration = handle_lock_expired(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    elif type(state_change) == ContractReceiveSecretReveal:
        iteration = handle_onchain_secretreveal(
            payment_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )
    else:
        iteration = TransitionResult(payment_state, list())

    return clear_if_finalized(iteration)
