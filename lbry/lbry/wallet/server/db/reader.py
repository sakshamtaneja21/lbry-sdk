import time
import struct
import sqlite3
import logging
from operator import itemgetter
from typing import Tuple, List, Dict, Union, Type, Optional
from binascii import unhexlify
from decimal import Decimal
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass

from torba.client.basedatabase import query, interpolate

from lbry.schema.url import URL, normalize_name
from lbry.schema.tags import clean_tags
from lbry.schema.result import Outputs
from lbry.wallet.ledger import BaseLedger, MainNetLedger, RegTestLedger

from .common import CLAIM_TYPES, STREAM_TYPES, COMMON_TAGS


class SQLiteOperationalError(sqlite3.OperationalError):
    def __init__(self, metrics):
        super().__init__('sqlite query errored')
        self.metrics = metrics


class SQLiteInterruptedError(sqlite3.OperationalError):
    def __init__(self, metrics):
        super().__init__('sqlite query interrupted')
        self.metrics = metrics


ATTRIBUTE_ARRAY_MAX_LENGTH = 100


INTEGER_PARAMS = {
    'height', 'creation_height', 'activation_height', 'expiration_height',
    'timestamp', 'creation_timestamp', 'release_time', 'fee_amount',
    'tx_position', 'channel_join',
    'amount', 'effective_amount', 'support_amount',
    'trending_group', 'trending_mixed',
    'trending_local', 'trending_global',
}

SEARCH_PARAMS = {
    'name', 'claim_id', 'txid', 'nout', 'channel', 'channel_ids', 'not_channel_ids',
    'public_key_id', 'claim_type', 'stream_types', 'media_types', 'fee_currency',
    'has_channel_signature', 'signature_valid',
    'any_tags', 'all_tags', 'not_tags',
    'any_locations', 'all_locations', 'not_locations',
    'any_languages', 'all_languages', 'not_languages',
    'is_controlling', 'limit', 'offset', 'order_by',
    'no_totals',
} | INTEGER_PARAMS


ORDER_FIELDS = {
   'name', 'claim_hash'
} | INTEGER_PARAMS


PRAGMAS = """
    pragma journal_mode=WAL;
"""


@dataclass
class ReaderState:
    db: sqlite3.Connection
    stack: List[List]
    metrics: Dict
    is_tracking_metrics: bool
    ledger: Type[BaseLedger]
    query_timeout: float
    log: logging.Logger

    def close(self):
        self.db.close()

    def reset_metrics(self):
        self.stack = []
        self.metrics = {}

    def set_query_timeout(self):
        stop_at = time.perf_counter() + self.query_timeout

        def interruptor():
            if time.perf_counter() >= stop_at:
                self.db.interrupt()
            return

        self.db.set_progress_handler(interruptor, 100)


ctx: ContextVar[Optional[ReaderState]] = ContextVar('ctx')


def initializer(log, _path, _ledger_name, query_timeout, _measure=False):
    db = sqlite3.connect(_path, isolation_level=None, uri=True)
    db.row_factory = sqlite3.Row
    ctx.set(
        ReaderState(
            db=db, stack=[], metrics={}, is_tracking_metrics=_measure,
            ledger=MainNetLedger if _ledger_name == 'mainnet' else RegTestLedger,
            query_timeout=query_timeout, log=log
        )
    )


def cleanup():
    ctx.get().close()
    ctx.set(None)


def measure(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        state = ctx.get()
        if not state.is_tracking_metrics:
            return func(*args, **kwargs)
        metric = {}
        state.metrics.setdefault(func.__name__, []).append(metric)
        state.stack.append([])
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = int((time.perf_counter()-start)*1000)
            metric['total'] = elapsed
            metric['isolated'] = (elapsed-sum(state.stack.pop()))
            if state.stack:
                state.stack[-1].append(elapsed)
    return wrapper


def reports_metrics(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        state = ctx.get()
        if not state.is_tracking_metrics:
            return func(*args, **kwargs)
        state.reset_metrics()
        r = func(*args, **kwargs)
        return r, state.metrics
    return wrapper


@reports_metrics
def search_to_bytes(constraints) -> Union[bytes, Tuple[bytes, Dict]]:
    return encode_result(search(constraints))


@reports_metrics
def resolve_to_bytes(urls) -> Union[bytes, Tuple[bytes, Dict]]:
    return encode_result(resolve(urls))


def encode_result(result):
    return Outputs.to_bytes(*result)


@measure
def execute_query(sql, values) -> List:
    context = ctx.get()
    context.set_query_timeout()
    try:
        return context.db.execute(sql, values).fetchall()
    except sqlite3.OperationalError as err:
        plain_sql = interpolate(sql, values)
        if context.is_tracking_metrics:
            context.metrics['execute_query'][-1]['sql'] = plain_sql
        if str(err) == "interrupted":
            context.log.warning("interrupted slow sqlite query:\n%s", plain_sql)
            raise SQLiteInterruptedError(context.metrics)
        context.log.exception('failed running query', exc_info=err)
        raise SQLiteOperationalError(context.metrics)


def _get_claims(cols, for_count=False, **constraints) -> Tuple[str, Dict]:
    if 'order_by' in constraints:
        sql_order_by = []
        for order_by in constraints['order_by']:
            is_asc = order_by.startswith('^')
            column = order_by[1:] if is_asc else order_by
            if column not in ORDER_FIELDS:
                raise NameError(f'{column} is not a valid order_by field')
            if column == 'name':
                column = 'normalized'
            sql_order_by.append(
                f"claim.{column} ASC" if is_asc else f"claim.{column} DESC"
            )
        constraints['order_by'] = sql_order_by

    ops = {'<=': '__lte', '>=': '__gte', '<': '__lt', '>': '__gt'}
    for constraint in INTEGER_PARAMS:
        if constraint in constraints:
            value = constraints.pop(constraint)
            postfix = ''
            if isinstance(value, str):
                if len(value) >= 2 and value[:2] in ops:
                    postfix, value = ops[value[:2]], value[2:]
                elif len(value) >= 1 and value[0] in ops:
                    postfix, value = ops[value[0]], value[1:]
            if constraint == 'fee_amount':
                value = Decimal(value)*1000
            constraints[f'claim.{constraint}{postfix}'] = int(value)

    if constraints.pop('is_controlling', False):
        if {'sequence', 'amount_order'}.isdisjoint(constraints):
            for_count = False
            constraints['claimtrie.claim_hash__is_not_null'] = ''
    if 'sequence' in constraints:
        constraints['order_by'] = 'claim.activation_height ASC'
        constraints['offset'] = int(constraints.pop('sequence')) - 1
        constraints['limit'] = 1
    if 'amount_order' in constraints:
        constraints['order_by'] = 'claim.effective_amount DESC'
        constraints['offset'] = int(constraints.pop('amount_order')) - 1
        constraints['limit'] = 1

    if 'claim_id' in constraints:
        claim_id = constraints.pop('claim_id')
        if len(claim_id) == 40:
            constraints['claim.claim_id'] = claim_id
        else:
            constraints['claim.claim_id__like'] = f'{claim_id[:40]}%'

    if 'name' in constraints:
        constraints['claim.normalized'] = normalize_name(constraints.pop('name'))

    if 'public_key_id' in constraints:
        constraints['claim.public_key_hash'] = sqlite3.Binary(
            ctx.get().ledger.address_to_hash160(constraints.pop('public_key_id')))
    if 'channel_hash' in constraints:
        constraints['claim.channel_hash'] = sqlite3.Binary(constraints.pop('channel_hash'))
    if 'channel_ids' in constraints:
        channel_ids = constraints.pop('channel_ids')
        if channel_ids:
            constraints['claim.channel_hash__in'] = [
                sqlite3.Binary(unhexlify(cid)[::-1]) for cid in channel_ids
            ]
    if 'not_channel_ids' in constraints:
        not_channel_ids = constraints.pop('not_channel_ids')
        if not_channel_ids:
            not_channel_ids_binary = [
                sqlite3.Binary(unhexlify(ncid)[::-1]) for ncid in not_channel_ids
            ]
            if constraints.get('has_channel_signature', False):
                constraints['claim.channel_hash__not_in'] = not_channel_ids_binary
            else:
                constraints['null_or_not_channel__or'] = {
                    'claim.signature_valid__is_null': True,
                    'claim.channel_hash__not_in': not_channel_ids_binary
                }
    if 'signature_valid' in constraints:
        has_channel_signature = constraints.pop('has_channel_signature', False)
        if has_channel_signature:
            constraints['claim.signature_valid'] = constraints.pop('signature_valid')
        else:
            constraints['null_or_signature__or'] = {
                'claim.signature_valid__is_null': True,
                'claim.signature_valid': constraints.pop('signature_valid')
            }
    elif constraints.pop('has_channel_signature', False):
        constraints['claim.signature_valid__is_not_null'] = True

    if 'txid' in constraints:
        tx_hash = unhexlify(constraints.pop('txid'))[::-1]
        nout = constraints.pop('nout', 0)
        constraints['claim.txo_hash'] = sqlite3.Binary(
            tx_hash + struct.pack('<I', nout)
        )

    if 'claim_type' in constraints:
        constraints['claim.claim_type'] = CLAIM_TYPES[constraints.pop('claim_type')]
    if 'stream_types' in constraints:
        stream_types = constraints.pop('stream_types')
        if stream_types:
            constraints['claim.stream_type__in'] = [
                STREAM_TYPES[stream_type] for stream_type in stream_types
            ]
    if 'media_types' in constraints:
        media_types = constraints.pop('media_types')
        if media_types:
            constraints['claim.media_type__in'] = media_types

    if 'fee_currency' in constraints:
        constraints['claim.fee_currency'] = constraints.pop('fee_currency').lower()

    _apply_constraints_for_array_attributes(constraints, 'tag', clean_tags, for_count)
    _apply_constraints_for_array_attributes(constraints, 'language', lambda _: _, for_count)
    _apply_constraints_for_array_attributes(constraints, 'location', lambda _: _, for_count)

    select = f"SELECT {cols} FROM claim"

    sql, values = query(
        select if for_count else select+"""
        LEFT JOIN claimtrie USING (claim_hash)
        LEFT JOIN claim as channel ON (claim.channel_hash=channel.claim_hash)
        """, **constraints
    )
    return sql, values


def get_claims(cols, for_count=False, **constraints) -> List:
    if 'channel' in constraints:
        channel_url = constraints.pop('channel')
        match = resolve_url(channel_url)
        if isinstance(match, sqlite3.Row):
            constraints['channel_hash'] = match['claim_hash']
        else:
            return [[0]] if cols == 'count(*)' else []
    sql, values = _get_claims(cols, for_count, **constraints)
    return execute_query(sql, values)


@measure
def get_claims_count(**constraints) -> int:
    constraints.pop('offset', None)
    constraints.pop('limit', None)
    constraints.pop('order_by', None)
    count = get_claims('count(*)', for_count=True, **constraints)
    return count[0][0]


@measure
def search(constraints) -> Tuple[List, List, int, int]:
    assert set(constraints).issubset(SEARCH_PARAMS), \
        f"Search query contains invalid arguments: {set(constraints).difference(SEARCH_PARAMS)}"
    total = None
    if not constraints.pop('no_totals', False):
        total = get_claims_count(**constraints)
    constraints['offset'] = abs(constraints.get('offset', 0))
    constraints['limit'] = min(abs(constraints.get('limit', 10)), 50)
    if 'order_by' not in constraints:
        constraints['order_by'] = ["claim_hash"]
    txo_rows = _search(**constraints)
    channel_hashes = set(filter(None, map(itemgetter('channel_hash'), txo_rows)))
    extra_txo_rows = []
    if channel_hashes:
        extra_txo_rows = _search(
            **{'claim.claim_hash__in': [sqlite3.Binary(h) for h in channel_hashes]}
        )
    return txo_rows, extra_txo_rows, constraints['offset'], total


def _search(**constraints):
    return get_claims(
        """
        claimtrie.claim_hash as is_controlling,
        claimtrie.last_take_over_height,
        claim.claim_hash, claim.txo_hash,
        claim.claims_in_channel,
        claim.height, claim.creation_height,
        claim.activation_height, claim.expiration_height,
        claim.effective_amount, claim.support_amount,
        claim.trending_group, claim.trending_mixed,
        claim.trending_local, claim.trending_global,
        claim.short_url, claim.canonical_url,
        claim.channel_hash, channel.txo_hash AS channel_txo_hash,
        channel.height AS channel_height, claim.signature_valid
        """, **constraints
    )


@measure
def resolve(urls) -> Tuple[List, List]:
    result = []
    channel_hashes = set()
    for raw_url in urls:
        match = resolve_url(raw_url)
        result.append(match)
        if isinstance(match, sqlite3.Row) and match['channel_hash']:
            channel_hashes.add(match['channel_hash'])
    extra_txo_rows = []
    if channel_hashes:
        extra_txo_rows = _search(
            **{'claim.claim_hash__in': [sqlite3.Binary(h) for h in channel_hashes]}
        )
    return result, extra_txo_rows


@measure
def resolve_url(raw_url):

    try:
        url = URL.parse(raw_url)
    except ValueError as e:
        return e

    channel = None

    if url.has_channel:
        query = url.channel.to_dict()
        if set(query) == {'name'}:
            query['is_controlling'] = True
        else:
            query['order_by'] = ['^height']
        matches = _search(**query, limit=1)
        if matches:
            channel = matches[0]
        else:
            return LookupError(f'Could not find channel in "{raw_url}".')

    if url.has_stream:
        query = url.stream.to_dict()
        if channel is not None:
            if set(query) == {'name'}:
                # temporarily emulate is_controlling for claims in channel
                query['order_by'] = ['effective_amount', '^height']
            else:
                query['order_by'] = ['^channel_join']
            query['channel_hash'] = channel['claim_hash']
            query['signature_valid'] = 1
        elif set(query) == {'name'}:
            query['is_controlling'] = 1
        matches = _search(**query, limit=1)
        if matches:
            return matches[0]
        else:
            return LookupError(f'Could not find stream in "{raw_url}".')

    return channel


def _apply_constraints_for_array_attributes(constraints, attr, cleaner, for_count=False):
    any_items = set(cleaner(constraints.pop(f'any_{attr}s', []))[:ATTRIBUTE_ARRAY_MAX_LENGTH])
    all_items = set(cleaner(constraints.pop(f'all_{attr}s', []))[:ATTRIBUTE_ARRAY_MAX_LENGTH])
    not_items = set(cleaner(constraints.pop(f'not_{attr}s', []))[:ATTRIBUTE_ARRAY_MAX_LENGTH])

    all_items = {item for item in all_items if item not in not_items}
    any_items = {item for item in any_items if item not in not_items}

    any_queries = {}

    if attr == 'tag':
        common_tags = any_items & COMMON_TAGS.keys()
        if common_tags:
            any_items -= common_tags
        if len(common_tags) < 5:
            for item in common_tags:
                index_name = COMMON_TAGS[item]
                any_queries[f'#_common_tag_{index_name}'] = f"""
                EXISTS(
                    SELECT 1 FROM tag INDEXED BY tag_{index_name}_idx WHERE claim.claim_hash=tag.claim_hash
                    AND tag = '{item}'
                )
                """
        elif len(common_tags) >= 5:
            constraints.update({
                f'$any_common_tag{i}': item for i, item in enumerate(common_tags)
            })
            values = ', '.join(
                f':$any_common_tag{i}' for i in range(len(common_tags))
            )
            any_queries[f'#_any_common_tags'] = f"""
            EXISTS(
                SELECT 1 FROM tag WHERE claim.claim_hash=tag.claim_hash
                AND tag IN ({values})
            )
            """

    if any_items:

        constraints.update({
            f'$any_{attr}{i}': item for i, item in enumerate(any_items)
        })
        values = ', '.join(
            f':$any_{attr}{i}' for i in range(len(any_items))
        )
        if for_count or attr == 'tag':
            any_queries[f'claim.claim_hash__in#_any_{attr}'] = f"""
            SELECT claim_hash FROM {attr} WHERE {attr} IN ({values})
            """
        else:
            any_queries[f'#_any_{attr}'] = f"""
            EXISTS(
                SELECT 1 FROM {attr} WHERE
                    claim.claim_hash={attr}.claim_hash
                AND {attr} IN ({values})
            )
            """

    if len(any_queries) == 1:
        constraints.update(any_queries)
    elif len(any_queries) > 1:
        constraints[f'ORed_{attr}_queries__any'] = any_queries

    if all_items:
        constraints[f'$all_{attr}_count'] = len(all_items)
        constraints.update({
            f'$all_{attr}{i}': item for i, item in enumerate(all_items)
        })
        values = ', '.join(
            f':$all_{attr}{i}' for i in range(len(all_items))
        )
        if for_count:
            constraints[f'claim.claim_hash__in#_all_{attr}'] = f"""
                SELECT claim_hash FROM {attr} WHERE {attr} IN ({values})
                GROUP BY claim_hash HAVING COUNT({attr}) = :$all_{attr}_count
            """
        else:
            constraints[f'#_all_{attr}'] = f"""
                {len(all_items)}=(
                    SELECT count(*) FROM {attr} WHERE
                        claim.claim_hash={attr}.claim_hash
                    AND {attr} IN ({values})
                )
            """

    if not_items:
        constraints.update({
            f'$not_{attr}{i}': item for i, item in enumerate(not_items)
        })
        values = ', '.join(
            f':$not_{attr}{i}' for i in range(len(not_items))
        )
        if for_count:
            constraints[f'claim.claim_hash__not_in#_not_{attr}'] = f"""
                SELECT claim_hash FROM {attr} WHERE {attr} IN ({values})
            """
        else:
            constraints[f'#_not_{attr}'] = f"""
                NOT EXISTS(
                    SELECT 1 FROM {attr} WHERE
                        claim.claim_hash={attr}.claim_hash
                    AND {attr} IN ({values})
                )
            """
